"""Transactional, group-aware scenario progression (#126)."""

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import ExerciseProgress
from app.models.inject import Inject, InjectProgress, InjectState
from app.services.scenario_service import definition_for_exercise


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

    # A team-specific physical inject has one progression context, so its legacy
    # top-level state can safely mirror the authoritative per-context resolution.
    # Shared injects remain released while other teams may still respond.
    if inject.group_id is not None or context is None:
        inject.state = InjectState.resolved
        inject.resolved_at = now
        inject.resolved_by = actor_id
        inject.resolution_reason = "participant_response"
        session.add(inject)

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
    """Release a scenario node only for a context currently pointing at it.

    Legacy exercises without progression rows remain operable. Independent root
    nodes are also valid opening choices before a context has advanced.
    """
    if not inject.scenario_node_id:
        return True

    query = select(ExerciseProgress).where(ExerciseProgress.exercise_id == inject.exercise_id)
    cursors = (await session.exec(query)).all()
    if not cursors:
        return True
    # A branch may intentionally hand off from one team to another. In that case
    # the originating team's cursor authorizes release of the target team's
    # physical inject; visibility remains constrained by the inject group.
    if any(cursor.current_node_id == inject.scenario_node_id for cursor in cursors):
        return True
    if any(cursor.current_inject_id is not None for cursor in cursors):
        return False

    definition = await definition_for_exercise(session, inject.exercise_id)
    if definition is None:
        return True
    referenced = {
        successor
        for node in definition.injects
        for successor in [
            node.next_inject_id,
            *(option.next_inject_id for option in node.options),
        ]
        if successor is not None
    }
    return inject.scenario_node_id not in referenced


async def progression_snapshot(
    session: AsyncSession,
    exercise_id: int,
    *,
    group_id: str | None = None,
    include_all_groups: bool = False,
) -> dict:
    """Serialize the authoritative cursor and resolution state for API/WS consumers."""
    cursor_query = select(ExerciseProgress).where(ExerciseProgress.exercise_id == exercise_id)
    inject_query = select(InjectProgress).where(InjectProgress.exercise_id == exercise_id)
    if not include_all_groups:
        cursor_query = cursor_query.where(ExerciseProgress.group_id == group_id)
        inject_query = inject_query.where(InjectProgress.group_id == group_id)

    cursors = (await session.exec(cursor_query)).all()
    resolutions = (await session.exec(inject_query)).all()
    return {
        "exercise_id": exercise_id,
        "cursors": [
            {
                "group_id": cursor.group_id,
                "current_node_id": cursor.current_node_id,
                "current_inject_id": cursor.current_inject_id,
                "advanced_at": cursor.advanced_at.isoformat(),
                "advanced_by": cursor.advanced_by,
            }
            for cursor in cursors
        ],
        "resolutions": [
            {
                "inject_id": resolution.inject_id,
                "group_id": resolution.group_id,
                "state": resolution.state,
                "resolved_at": (
                    resolution.resolved_at.isoformat() if resolution.resolved_at else None
                ),
                "resolved_by": resolution.resolved_by,
                "resolution_reason": resolution.resolution_reason,
            }
            for resolution in resolutions
        ],
    }
