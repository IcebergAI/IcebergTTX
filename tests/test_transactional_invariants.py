"""PostgreSQL concurrency and rollback coverage for #125."""

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.exercise import Exercise, ExerciseState
from app.models.inject import Inject, InjectState
from app.models.response import Response
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.schemas.scenario_json import InjectNode, ScenarioDefinition
from app.services.exercise_service import create_exercise, transition_state
from app.services.inject_service import release_inject
from app.services.response_service import submit_response
from app.services.scenario_service import create_scenario


async def _persisted_active_exercise() -> tuple[int, int, int, int, int]:
    """Create data committed outside the per-test rollback transaction."""
    unique = uuid4().hex
    async with AsyncSession(engine, expire_on_commit=False) as setup:
        facilitator = User(
            email=f"transaction-{unique}@example.test",
            display_name="Transaction Facilitator",
            role=UserRole.facilitator,
        )
        participant = User(
            email=f"participant-{unique}@example.test",
            display_name="Transaction Participant",
            role=UserRole.participant,
        )
        setup.add_all([facilitator, participant])
        await setup.commit()
        scenario = await create_scenario(
            setup,
            definition=ScenarioDefinition(
                title=f"Transaction scenario {unique}",
                injects=[InjectNode(id="start", title="Start", content="Respond")],
                start_inject_id="start",
            ),
            created_by=facilitator.id,
        )
        exercise = await create_exercise(
            setup,
            scenario_id=scenario.id,
            title=f"Transaction exercise {unique}",
            created_by=facilitator.id,
        )
        exercise = await transition_state(
            setup, exercise, ExerciseState.active, actor_id=facilitator.id
        )
        inject = (
            await setup.exec(select(Inject).where(Inject.exercise_id == exercise.id))
        ).one()
        return facilitator.id, participant.id, scenario.id, exercise.id, inject.id


async def _cleanup(
    exercise_id: int,
    scenario_id: int,
    facilitator_id: int,
    participant_id: int,
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as cleanup:
        exercise = await cleanup.get(Exercise, exercise_id)
        if exercise is not None:
            await cleanup.delete(exercise)
            await cleanup.commit()
        scenario = await cleanup.get(Scenario, scenario_id)
        if scenario is not None:
            await cleanup.delete(scenario)
            await cleanup.commit()

        for user_id in (facilitator_id, participant_id):
            user = await cleanup.get(User, user_id)
            if user is not None:
                await cleanup.delete(user)
        await cleanup.commit()


async def test_stale_inject_release_emits_one_committed_state_change(monkeypatch):
    """Two independent sessions observing pending cannot both release an inject."""
    facilitator_id, participant_id, scenario_id, exercise_id, inject_id = (
        await _persisted_active_exercise()
    )

    async def no_side_effect(*_):
        return None

    monkeypatch.setattr("app.services.inject_service._broadcast_inject_released", no_side_effect)
    monkeypatch.setattr("app.services.inject_service._trigger_communications", no_side_effect)
    try:
        async with (
            AsyncSession(engine, expire_on_commit=False) as winner_session,
            AsyncSession(engine, expire_on_commit=False) as loser_session,
        ):
            winner_view = await winner_session.get(Inject, inject_id)
            loser_view = await loser_session.get(Inject, inject_id)
            assert winner_view is not None and loser_view is not None
            winner = await release_inject(winner_session, winner_view, facilitator_id)
            assert winner.state == InjectState.released
            with pytest.raises(HTTPException) as exc_info:
                await release_inject(loser_session, loser_view, facilitator_id)
            assert exc_info.value.status_code == 409
        async with AsyncSession(engine, expire_on_commit=False) as verify:
            stored = await verify.get(Inject, inject_id)
            assert stored is not None and stored.state == InjectState.released
    finally:
        await _cleanup(exercise_id, scenario_id, facilitator_id, participant_id)


async def test_concurrent_response_submissions_keep_one_response():
    """The unique identity constraint converts a duplicate write into HTTP 409."""
    facilitator_id, participant_id, scenario_id, exercise_id, inject_id = (
        await _persisted_active_exercise()
    )
    try:
        async with AsyncSession(engine, expire_on_commit=False) as release_session:
            inject = await release_session.get(Inject, inject_id)
            assert inject is not None
            await release_inject(release_session, inject, facilitator_id)

        async def submit_once():
            async with AsyncSession(engine, expire_on_commit=False) as concurrent_session:
                return await submit_response(
                    concurrent_session,
                    inject_id=inject_id,
                    exercise_id=exercise_id,
                    user_id=participant_id,
                    content="Contain the incident",
                )

        outcomes = await asyncio.gather(submit_once(), submit_once(), return_exceptions=True)
        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
        assert len(conflicts) == 1 and conflicts[0].status_code == 409
        async with AsyncSession(engine, expire_on_commit=False) as verify:
            responses = (
                await verify.exec(select(Response).where(Response.inject_id == inject_id))
            ).all()
            assert len(responses) == 1
    finally:
        await _cleanup(exercise_id, scenario_id, facilitator_id, participant_id)


async def test_exercise_seeding_failure_rolls_back_parent_and_children(monkeypatch):
    """The exercise does not survive when scenario seeding fails before commit."""
    unique = uuid4().hex
    facilitator_id = scenario_id = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            facilitator = User(
                email=f"rollback-{unique}@example.test",
                display_name="Rollback Facilitator",
                role=UserRole.facilitator,
            )
            setup.add(facilitator)
            await setup.commit()
            facilitator_id = facilitator.id
            scenario = await create_scenario(
                setup,
                definition=ScenarioDefinition(
                    title=f"Rollback scenario {unique}",
                    injects=[InjectNode(id="start", title="Start", content="Respond")],
                    start_inject_id="start",
                ),
                created_by=facilitator.id,
            )
            scenario_id = scenario.id

            async def fail_seed(*_args, **_kwargs):
                raise RuntimeError("simulated seed failure")

            monkeypatch.setattr(
                "app.services.exercise_service.seed_injects_from_scenario", fail_seed
            )
            with pytest.raises(RuntimeError, match="simulated seed failure"):
                await create_exercise(
                    setup,
                    scenario_id=scenario.id,
                    title="Must roll back",
                    created_by=facilitator.id,
                )

        async with AsyncSession(engine, expire_on_commit=False) as verify:
            exercises = (
                await verify.exec(select(Exercise).where(Exercise.scenario_id == scenario_id))
            ).all()
            assert exercises == []
    finally:
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            if scenario_id is not None:
                scenario = await cleanup.get(Scenario, scenario_id)
                if scenario is not None:
                    await cleanup.delete(scenario)
            if facilitator_id is not None:
                facilitator = await cleanup.get(User, facilitator_id)
                if facilitator is not None:
                    await cleanup.delete(facilitator)
            await cleanup.commit()
