from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import (
    VALID_TRANSITIONS,
    Exercise,
    ExerciseMember,
    ExerciseState,
    ExerciseStateTransition,
    transition_action,
)
from app.models.scenario import Scenario
from app.models.user import User
from app.services.inject_service import seed_injects_from_scenario
from app.services.progression_service import seed_progression
from app.services.scenario_service import export_definition


@dataclass(frozen=True)
class ExerciseTransitionResult:
    exercise: Exercise
    transition: ExerciseStateTransition
    action: str


async def create_exercise(
    session: AsyncSession,
    *,
    scenario_id: int,
    title: str,
    created_by: int,
    llm_enabled: bool = False,
) -> Exercise:
    scenario = await session.get(Scenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")

    definition = export_definition(scenario)
    exercise = Exercise(
        scenario_id=scenario_id,
        title=title,
        created_by=created_by,
        llm_enabled=llm_enabled,
        current_node_id=definition.start_inject_id,
    )
    session.add(exercise)
    await session.flush()
    assert exercise.id is not None

    try:
        await seed_injects_from_scenario(session, exercise.id, scenario)
        await seed_progression(
            session,
            exercise_id=exercise.id,
            start_node_id=definition.start_inject_id,
            group_ids=[team.id for team in definition.participant_teams],
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(exercise)
    return exercise


async def transition_state(
    session: AsyncSession,
    exercise: Exercise,
    new_state: ExerciseState,
    *,
    actor_id: int | None = None,
) -> Exercise:
    """Compatibility wrapper returning the updated Exercise.

    API callers that need the durable event for a post-commit projection should use
    ``transition_state_with_history`` directly.
    """
    result = await transition_state_with_history(session, exercise, new_state, actor_id=actor_id)
    return result.exercise


async def transition_state_with_history(
    session: AsyncSession,
    exercise: Exercise,
    new_state: ExerciseState,
    *,
    actor_id: int | None = None,
) -> ExerciseTransitionResult:
    """Atomically change state and append its authoritative lifecycle event.

    The conditional UPDATE is a compare-and-swap on the state observed by the
    caller. A request that waited behind or raced another transition therefore
    receives 409 instead of overwriting the newer state. The history row and
    Exercise update share one transaction; callers may safely emit external
    projections only after this function returns.
    """
    previous_state = exercise.state
    if new_state not in VALID_TRANSITIONS[previous_state]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition from '{previous_state}' to '{new_state}'",
        )

    now = datetime.now(UTC)
    values: dict = {"state": new_state}
    if new_state == ExerciseState.active and exercise.started_at is None:
        values["started_at"] = now
    # Include pacing fields in the same compare-and-swap update as state so a
    # stale lifecycle request cannot overwrite a completed pause calculation.
    if new_state == ExerciseState.paused:
        values["paused_at"] = now
    if new_state == ExerciseState.active and exercise.paused_at is not None:
        values["accumulated_pause_seconds"] = (
            exercise.accumulated_pause_seconds + (now - exercise.paused_at).total_seconds()
        )
        values["paused_at"] = None
    if new_state == ExerciseState.completed:
        values["ended_at"] = now

    assert exercise.id is not None
    statement = (
        update(Exercise)
        .where(col(Exercise.id) == exercise.id, col(Exercise.state) == previous_state)
        .values(**values)
        .returning(col(Exercise.id))
        .execution_options(synchronize_session=False)
    )
    result = await session.exec(statement)
    if result.scalar_one_or_none() is None:
        current = (
            await session.exec(select(Exercise.state).where(Exercise.id == exercise.id))
        ).one_or_none()
        await session.rollback()
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Exercise state changed from '{previous_state}' to '{current}' "
                "while this request was in flight; reload and try again"
            ),
        )

    transition = ExerciseStateTransition(
        exercise_id=exercise.id,
        from_state=previous_state,
        to_state=new_state,
        actor_id=actor_id if actor_id is not None else exercise.created_by,
        transitioned_at=now,
    )
    session.add(transition)
    try:
        await session.flush()
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    updated = await session.get(Exercise, exercise.id, populate_existing=True)
    assert updated is not None
    action = transition_action(previous_state, new_state)
    return ExerciseTransitionResult(exercise=updated, transition=transition, action=action)


async def broadcast_exercise_state(exercise: Exercise) -> None:
    """Push a state/timing change to every connected client (#116).

    Carries the full serialised exercise so each client's pause-aware clock stays
    correct (pause/resume must freeze/continue on participant views too). Sent on every
    lifecycle transition; there is no per-second traffic — the clock ticks client-side.
    """
    from app.schemas.api import ExercisePublic
    from app.services.ws_manager import manager

    assert exercise.id is not None

    await manager.broadcast_to_exercise(
        exercise.id,
        {
            "type": "exercise_state_change",
            "exercise_id": exercise.id,
            "timestamp": datetime.now(UTC).isoformat(),
            "payload": ExercisePublic.from_model(exercise).model_dump(mode="json"),
        },
    )


async def scenario_group_ids(session: AsyncSession, exercise: Exercise) -> set[str]:
    scenario = await session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return set()
    definition = export_definition(scenario)
    return {team.id for team in definition.participant_teams}


async def validate_group_id(
    session: AsyncSession, exercise: Exercise, group_id: str | None
) -> str | None:
    if group_id is None:
        return None
    normalized = group_id.strip()
    if not normalized:
        return None
    if normalized not in await scenario_group_ids(session, exercise):
        raise HTTPException(
            status_code=422,
            detail="group_id is not defined in this scenario",
        )
    return normalized


async def default_group_for_user(
    session: AsyncSession, exercise: Exercise, user: User
) -> str | None:
    if not user.team:
        return None
    return user.team if user.team in await scenario_group_ids(session, exercise) else None


async def enrol_member(
    session: AsyncSession,
    *,
    exercise: Exercise,
    user_id: int,
    group_id: str | None = None,
) -> ExerciseMember:
    assert exercise.id is not None
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    normalized_group_id = await validate_group_id(session, exercise, group_id)
    existing = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if existing:
        return existing  # idempotent

    resolved_group_id = (
        normalized_group_id
        if normalized_group_id is not None
        else await default_group_for_user(session, exercise, user)
    )
    member = ExerciseMember(
        exercise_id=exercise.id,
        user_id=user_id,
        group_id=resolved_group_id,
        role_at_enrolment=user.role,
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def update_member_group(
    session: AsyncSession,
    *,
    exercise: Exercise,
    user_id: int,
    group_id: str | None,
) -> ExerciseMember:
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in exercise"
        )
    member.group_id = await validate_group_id(session, exercise, group_id)
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def remove_member(session: AsyncSession, *, exercise: Exercise, user_id: int) -> None:
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in exercise"
        )
    await session.delete(member)
    await session.commit()
