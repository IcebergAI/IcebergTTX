"""Invariant tests for the bounded immutable-launch-snapshot foundation."""

import asyncio
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.exercise import (
    Exercise,
    ExerciseState,
    ExerciseStateTransition,
    SnapshotProvenance,
)
from app.models.inject import Inject
from app.models.launch_snapshot import ExerciseLaunchSnapshot
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.schemas.scenario_json import InjectNode, ScenarioDefinition
from app.services.exercise_service import create_exercise, enrol_member, transition_state
from app.services.launch_snapshot_service import (
    lock_configuration_owner,
    require_inject_configuration_mutable,
    snapshot_digest,
)
from app.services.scenario_service import create_scenario, definition_for_exercise


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_snapshot_digest_is_canonical_for_equivalent_json_objects():
    left = {
        "schema_version": "1.0",
        "exercise": {"title": "Same", "flags": [True, False]},
        "injects": [{"target_teams": ["legal", "ops"], "content": "x"}],
    }
    right = {
        "injects": [{"content": "x", "target_teams": ["legal", "ops"]}],
        "exercise": {"flags": [True, False], "title": "Same"},
        "schema_version": "1.0",
    }

    assert snapshot_digest(left) == snapshot_digest(right)
    assert snapshot_digest(left) != snapshot_digest(
        {**right, "exercise": {"flags": [True, False], "title": "Changed"}}
    )


async def test_first_launch_creates_one_exact_snapshot_and_repeated_start_is_rejected(
    client: AsyncClient,
    facilitator_token: str,
    draft_exercise: Exercise,
    session: AsyncSession,
):
    first = await client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers=_bearer(facilitator_token),
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["launch_provenance"] == SnapshotProvenance.exact
    assert len(payload["launch_snapshot_digest"]) == 64

    snapshots = (
        await session.exec(
            select(ExerciseLaunchSnapshot).where(
                ExerciseLaunchSnapshot.digest == payload["launch_snapshot_digest"]
            )
        )
    ).all()
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot_digest(snapshot.content) == snapshot.digest
    assert snapshot.content["scenario"]["definition"]["title"] == "Test Scenario"
    assert snapshot.content["exercise"]["title"] == draft_exercise.title
    assert snapshot.content["injects"]

    readback = await client.get(
        f"/api/exercises/{draft_exercise.id}/launch-snapshot",
        headers=_bearer(facilitator_token),
    )
    assert readback.status_code == 200
    assert readback.json()["digest"] == snapshot.digest
    assert readback.json()["content"] == snapshot.content

    repeated = await client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers=_bearer(facilitator_token),
    )
    assert repeated.status_code == 409
    assert len(
        (
            await session.exec(
                select(ExerciseLaunchSnapshot).where(
                    ExerciseLaunchSnapshot.digest == snapshot.digest
                )
            )
        ).all()
    ) == 1


async def test_failed_attachment_capture_leaves_launch_unstarted_and_unsnapshotted(
    client: AsyncClient,
    facilitator_token: str,
    draft_exercise: Exercise,
    session: AsyncSession,
):
    exercise_id = draft_exercise.id
    assert exercise_id is not None
    inject = (
        await session.exec(select(Inject).where(Inject.exercise_id == exercise_id))
    ).first()
    assert inject is not None
    inject.attachment_path = f"/tmp/icebergttx-missing-{uuid4().hex}"
    inject.attachment_filename = "missing.txt"
    inject.attachment_size = 10
    session.add(inject)
    await session.commit()

    response = await client.post(
        f"/api/exercises/{exercise_id}/start",
        headers=_bearer(facilitator_token),
    )
    assert response.status_code == 409

    stored = await session.get(Exercise, exercise_id, populate_existing=True)
    assert stored is not None
    assert stored.state == ExerciseState.draft
    assert stored.launch_provenance == SnapshotProvenance.pending
    assert stored.launch_snapshot_digest is None
    transitions = (
        await session.exec(
            select(ExerciseStateTransition).where(
                ExerciseStateTransition.exercise_id == exercise_id
            )
        )
    ).all()
    assert transitions == []


async def test_transition_event_failure_rolls_back_snapshot_and_launch(
    monkeypatch: pytest.MonkeyPatch,
    draft_exercise: Exercise,
    session: AsyncSession,
):
    exercise_id = draft_exercise.id
    assert exercise_id is not None
    event_record = Mock(side_effect=RuntimeError("event recording unavailable"))
    monkeypatch.setattr("app.services.exercise_service.record", event_record)

    with pytest.raises(RuntimeError, match="event recording unavailable"):
        await transition_state(session, draft_exercise, ExerciseState.active)

    stored = await session.get(Exercise, exercise_id, populate_existing=True)
    assert stored is not None
    assert stored.state == ExerciseState.draft
    assert stored.launch_provenance == SnapshotProvenance.pending
    assert stored.launch_snapshot_digest is None
    assert (
        await session.exec(
            select(ExerciseStateTransition).where(
                ExerciseStateTransition.exercise_id == exercise_id
            )
        )
    ).all() == []
    event_record.assert_called_once()


async def test_launched_runtime_definition_comes_from_snapshot_not_mutable_scenario_row(
    session: AsyncSession,
    draft_exercise: Exercise,
    sample_scenario: Scenario,
):
    launched = await transition_state(session, draft_exercise, ExerciseState.active)
    original = await definition_for_exercise(session, launched.id)
    assert original is not None

    changed = original.model_copy(update={"title": "Database row changed after launch"})
    sample_scenario.definition = changed.model_dump_json()
    sample_scenario.title = changed.title
    session.add(sample_scenario)
    await session.commit()

    runtime = await definition_for_exercise(session, launched.id)
    assert runtime is not None
    assert runtime.title == original.title
    assert runtime.title != sample_scenario.title


async def test_material_launch_configuration_changes_the_digest(
    session: AsyncSession,
    facilitator: User,
    sample_scenario: Scenario,
):
    first = await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Canonical run",
        created_by=facilitator.id,
    )
    second = await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Canonical run",
        created_by=facilitator.id,
    )
    changed = await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Materially changed run",
        created_by=facilitator.id,
    )
    first = await transition_state(session, first, ExerciseState.active)
    second = await transition_state(session, second, ExerciseState.active)
    changed = await transition_state(session, changed, ExerciseState.active)

    assert first.launch_snapshot_digest is not None
    assert second.launch_snapshot_digest is not None
    assert changed.launch_snapshot_digest is not None
    assert first.launch_snapshot_digest == second.launch_snapshot_digest
    assert first.launch_snapshot_digest != changed.launch_snapshot_digest


async def _persisted_draft() -> tuple[int, int, int, int]:
    unique = uuid4().hex
    async with AsyncSession(engine, expire_on_commit=False) as setup:
        facilitator = User(
            email=f"launch-owner-{unique}@example.test",
            display_name="Launch Owner",
            role=UserRole.facilitator,
        )
        participant = User(
            email=f"launch-participant-{unique}@example.test",
            display_name="Launch Participant",
            role=UserRole.participant,
            team="ops",
        )
        setup.add_all([facilitator, participant])
        await setup.commit()
        scenario = await create_scenario(
            setup,
            definition=ScenarioDefinition(
                title=f"Concurrent launch {unique}",
                participant_teams=[{"id": "ops", "label": "Operations"}],
                injects=[
                    InjectNode(
                        id="start",
                        title="Start",
                        content="Respond",
                        release_at_minutes=5,
                    )
                ],
                start_inject_id="start",
            ),
            created_by=facilitator.id,
        )
        exercise = await create_exercise(
            setup,
            scenario_id=scenario.id,
            title=f"Concurrent exercise {unique}",
            created_by=facilitator.id,
        )
        await enrol_member(
            setup,
            exercise=exercise,
            user_id=participant.id,
            group_id="ops",
        )
        assert facilitator.id and participant.id and scenario.id and exercise.id
        return facilitator.id, participant.id, scenario.id, exercise.id


async def _cleanup_persisted_draft(
    facilitator_id: int,
    participant_id: int,
    scenario_id: int,
    exercise_id: int,
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


async def test_concurrent_launch_requests_create_one_snapshot_and_one_transition():
    identifiers = await _persisted_draft()
    facilitator_id, participant_id, scenario_id, exercise_id = identifiers
    try:
        async def launch_once():
            async with AsyncSession(engine, expire_on_commit=False) as concurrent:
                exercise = await concurrent.get(Exercise, exercise_id)
                assert exercise is not None
                return await transition_state(
                    concurrent,
                    exercise,
                    ExerciseState.active,
                    actor_id=facilitator_id,
                )

        outcomes = await asyncio.gather(launch_once(), launch_once(), return_exceptions=True)
        assert sum(isinstance(outcome, Exercise) for outcome in outcomes) == 1
        conflicts = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
        assert len(conflicts) == 1
        assert conflicts[0].status_code == 409

        async with AsyncSession(engine, expire_on_commit=False) as verify:
            stored = await verify.get(Exercise, exercise_id)
            assert stored is not None
            assert stored.state == ExerciseState.active
            assert stored.launch_provenance == SnapshotProvenance.exact
            snapshots = (
                await verify.exec(
                    select(ExerciseLaunchSnapshot).where(
                        ExerciseLaunchSnapshot.digest == stored.launch_snapshot_digest
                    )
                )
            ).all()
            transitions = (
                await verify.exec(
                    select(ExerciseStateTransition).where(
                        ExerciseStateTransition.exercise_id == exercise_id
                    )
                )
            ).all()
            assert len(snapshots) == 1
            assert len(transitions) == 1
    finally:
        await _cleanup_persisted_draft(
            facilitator_id,
            participant_id,
            scenario_id,
            exercise_id,
        )


@pytest.mark.parametrize("edit_before_launch", [True, False])
async def test_launch_and_inject_edit_have_one_serialized_boundary(edit_before_launch: bool):
    identifiers = await _persisted_draft()
    facilitator_id, participant_id, scenario_id, exercise_id = identifiers
    try:
        async with (
            AsyncSession(engine, expire_on_commit=False) as launch_session,
            AsyncSession(engine, expire_on_commit=False) as edit_session,
        ):
            launch_view = await launch_session.get(Exercise, exercise_id)
            edit_view = await edit_session.get(Exercise, exercise_id)
            assert launch_view is not None and edit_view is not None
            first = edit_session if edit_before_launch else launch_session
            first_view = edit_view if edit_before_launch else launch_view
            await lock_configuration_owner(first, first_view)

            started = asyncio.Event()

            async def edit() -> None:
                started.set()
                _, locked_exercise = await lock_configuration_owner(edit_session, edit_view)
                inject = (
                    await edit_session.exec(
                        select(Inject)
                        .where(Inject.exercise_id == exercise_id)
                        .order_by(col(Inject.id))
                        .with_for_update()
                    )
                ).one()
                require_inject_configuration_mutable(locked_exercise, inject)
                inject.release_offset_minutes = 45
                edit_session.add(inject)
                await edit_session.commit()

            async def launch() -> Exercise:
                started.set()
                return await transition_state(
                    launch_session,
                    launch_view,
                    ExerciseState.active,
                    actor_id=facilitator_id,
                )

            if edit_before_launch:
                launch_task = asyncio.create_task(launch())
                await started.wait()
                inject = (
                    await edit_session.exec(
                        select(Inject)
                        .where(Inject.exercise_id == exercise_id)
                        .order_by(col(Inject.id))
                        .with_for_update()
                    )
                ).one()
                require_inject_configuration_mutable(edit_view, inject)
                inject.release_offset_minutes = 45
                edit_session.add(inject)
                await edit_session.commit()
                await launch_task
            else:
                edit_task = asyncio.create_task(edit())
                await started.wait()
                await transition_state(
                    launch_session,
                    launch_view,
                    ExerciseState.active,
                    actor_id=facilitator_id,
                )
                with pytest.raises(HTTPException) as exc_info:
                    await edit_task
                assert exc_info.value.status_code == 409

        async with AsyncSession(engine, expire_on_commit=False) as verify:
            stored = await verify.get(Exercise, exercise_id)
            assert stored is not None and stored.launch_snapshot_digest is not None
            snapshot = await verify.get(
                ExerciseLaunchSnapshot,
                stored.launch_snapshot_digest,
            )
            inject = (
                await verify.exec(select(Inject).where(Inject.exercise_id == exercise_id))
            ).one()
            assert snapshot is not None
            frozen_offset = snapshot.content["injects"][0]["release_offset_minutes"]
            assert frozen_offset == (45 if edit_before_launch else 5)
            assert inject.release_offset_minutes == (45 if edit_before_launch else 5)
    finally:
        await _cleanup_persisted_draft(
            facilitator_id,
            participant_id,
            scenario_id,
            exercise_id,
        )


@pytest.mark.parametrize("edit_before_launch", [True, False])
async def test_launch_and_exercise_config_edit_have_one_serialized_boundary(
    edit_before_launch: bool,
):
    identifiers = await _persisted_draft()
    facilitator_id, participant_id, scenario_id, exercise_id = identifiers
    edited_title = f"Edited before launch {uuid4().hex}"
    try:
        async with (
            AsyncSession(engine, expire_on_commit=False) as launch_session,
            AsyncSession(engine, expire_on_commit=False) as edit_session,
        ):
            launch_view = await launch_session.get(Exercise, exercise_id)
            edit_view = await edit_session.get(Exercise, exercise_id)
            assert launch_view is not None and edit_view is not None
            original_title = launch_view.title
            first = edit_session if edit_before_launch else launch_session
            first_view = edit_view if edit_before_launch else launch_view
            await lock_configuration_owner(first, first_view)

            started = asyncio.Event()

            async def edit() -> None:
                started.set()
                _, locked = await lock_configuration_owner(edit_session, edit_view)
                if locked.launch_provenance == SnapshotProvenance.exact:
                    raise HTTPException(
                        status_code=409,
                        detail="Exercise launch configuration is frozen",
                    )
                locked.title = edited_title
                edit_session.add(locked)
                await edit_session.commit()

            async def launch() -> Exercise:
                started.set()
                return await transition_state(
                    launch_session,
                    launch_view,
                    ExerciseState.active,
                    actor_id=facilitator_id,
                )

            if edit_before_launch:
                launch_task = asyncio.create_task(launch())
                await started.wait()
                edit_view.title = edited_title
                edit_session.add(edit_view)
                await edit_session.commit()
                await launch_task
            else:
                edit_task = asyncio.create_task(edit())
                await started.wait()
                await transition_state(
                    launch_session,
                    launch_view,
                    ExerciseState.active,
                    actor_id=facilitator_id,
                )
                with pytest.raises(HTTPException) as exc_info:
                    await edit_task
                assert exc_info.value.status_code == 409

        async with AsyncSession(engine, expire_on_commit=False) as verify:
            stored = await verify.get(Exercise, exercise_id)
            assert stored is not None and stored.launch_snapshot_digest is not None
            snapshot = await verify.get(
                ExerciseLaunchSnapshot,
                stored.launch_snapshot_digest,
            )
            assert snapshot is not None
            frozen_title = snapshot.content["exercise"]["title"]
            expected_title = edited_title if edit_before_launch else original_title
            assert frozen_title == expected_title
            assert stored.title == expected_title
    finally:
        await _cleanup_persisted_draft(
            facilitator_id,
            participant_id,
            scenario_id,
            exercise_id,
        )
