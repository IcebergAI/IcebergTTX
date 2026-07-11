"""Transactional, group-aware scenario progression (#126)."""

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import ExerciseProgress
from app.models.inject import Inject, InjectProgress, InjectState


async def seed_progression(
    session: AsyncSession, *, exercise_id: int, start_node_id: str, group_ids: list[str]
) -> None:
    """Create the initial cursor for every scenario team and shared path."""
    for group_id in [None, *group_ids]:
        await session.exec(
            insert(ExerciseProgress)
            .values(
                exercise_id=exercise_id,
                group_id=group_id,
                current_node_id=start_node_id,
                advanced_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(constraint="uq_exercise_progress_group")
        )


async def resolve_response_progression(
    session: AsyncSession,
    *,
    inject: Inject,
    group_id: str | None,
    actor_id: int,
    next_node_id: str | None,
) -> bool:
    """Resolve one group path and atomically advance its cursor.

    Returns ``True`` only when this call performed the transition; replayed
    deliveries observe the existing resolution without changing the cursor.
    """
    context = group_id or inject.group_id
    await session.exec(
        insert(InjectProgress)
        .values(
            exercise_id=inject.exercise_id,
            inject_id=inject.id,
            group_id=context,
            state=InjectState.released,
        )
        .on_conflict_do_nothing(constraint="uq_inject_progress_group")
    )
    progress = (
        await session.exec(
            select(InjectProgress)
            .where(InjectProgress.inject_id == inject.id)
            .where(InjectProgress.group_id == context)
            .with_for_update()
        )
    ).one()
    if progress.state == InjectState.resolved:
        return False

    now = datetime.now(UTC)
    progress.state = InjectState.resolved
    progress.resolved_at = now
    progress.resolved_by = actor_id
    progress.resolution_reason = "participant_response"
    session.add(progress)

    cursor = (
        await session.exec(
            select(ExerciseProgress)
            .where(ExerciseProgress.exercise_id == inject.exercise_id)
            .where(ExerciseProgress.group_id == context)
            .with_for_update()
        )
    ).one_or_none()
    if cursor is None:
        cursor = ExerciseProgress(exercise_id=inject.exercise_id, group_id=context)
    cursor.current_inject_id = inject.id
    cursor.current_node_id = next_node_id
    cursor.advanced_at = now
    cursor.advanced_by = actor_id
    session.add(cursor)
    return True


async def release_is_allowed(session: AsyncSession, inject: Inject) -> bool:
    """A group-specific branch may release only when it is that group's cursor."""
    if not inject.group_id or not inject.scenario_node_id:
        return True
    cursor = (
        await session.exec(
            select(ExerciseProgress).where(
                ExerciseProgress.exercise_id == inject.exercise_id,
                ExerciseProgress.group_id == inject.group_id,
            )
        )
    ).one_or_none()
    # A scenario may deliberately have independent group-specific opening injects;
    # before a group has advanced, the facilitator retains that manual choice.
    # Once a response advances the cursor, only its selected successor may release.
    return (
        cursor is None
        or cursor.current_inject_id is None
        or cursor.current_node_id == inject.scenario_node_id
    )
