"""Transactional, group-aware scenario progression (#126)."""

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseMember, ExerciseProgress
from app.models.inject import Inject, InjectProgress, InjectState
from app.models.user import UserRole
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


def participant_contexts(members: Iterable[ExerciseMember]) -> set[str | None]:
    """Contexts that can resolve a shared inject, based on enrolment-time roles."""
    return {
        member.group_id
        for member in members
        if member.role_at_enrolment == UserRole.participant
    }


def inject_audience_contexts(
    inject: Inject, members: Iterable[ExerciseMember]
) -> set[str | None]:
    """Participant contexts eligible to resolve this inject at release time."""
    contexts = participant_contexts(members)
    if inject.group_id is not None:
        return {inject.group_id} if inject.group_id in contexts else set()
    if inject.target_teams:
        return contexts & set(inject.target_teams)
    return contexts


async def seed_inject_resolution_contexts(
    session: AsyncSession, inject: Inject
) -> set[str | None]:
    """Persist the immutable resolution audience for one released inject."""
    assert inject.id is not None
    members = (
        await session.exec(
            select(ExerciseMember).where(
                ExerciseMember.exercise_id == inject.exercise_id
            )
        )
    ).all()
    contexts = inject_audience_contexts(inject, members)
    for context in contexts:
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
    return contexts


async def roster_changes_allowed(session: AsyncSession, exercise_id: int) -> bool:
    """Lock the exercise and reject roster changes after its first release."""
    await session.exec(
        select(Exercise.id).where(Exercise.id == exercise_id).with_for_update()
    )
    released = (
        await session.exec(
            select(Inject.id)
            .where(
                Inject.exercise_id == exercise_id,
                Inject.state != InjectState.pending,
            )
            .limit(1)
        )
    ).first()
    return released is None


async def lock_exercise_for_audience_snapshot(
    session: AsyncSession, exercise_id: int
) -> None:
    """Serialize inject release with roster mutation on the exercise row."""
    await session.exec(
        select(Exercise.id).where(Exercise.id == exercise_id).with_for_update()
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
    deliveries — and responses from contexts outside the release audience —
    observe the existing state without changing any cursor.
    """
    # Serialize shared-inject resolution. Without the row lock, two teams could each
    # observe only their own uncommitted progress and neither would close the scalar.
    locked_inject = (
        await session.exec(
            select(Inject).where(Inject.id == inject.id).with_for_update()
        )
    ).one()
    inject = locked_inject
    context = group_id or inject.group_id
    await seed_inject_resolution_contexts(session, inject)
    progress = (
        await session.exec(
            select(InjectProgress)
            .where(InjectProgress.inject_id == inject.id)
            .where(InjectProgress.group_id == context)
            .with_for_update()
        )
    ).one_or_none()
    if progress is None:
        # No seeded row means this context was never in the release audience —
        # e.g. an unassigned member (group_id NULL) who can see a team-targeted
        # inject via the User.team visibility fallback (#256). Inventing a row
        # here would advance a cursor for a path the release never opened; the
        # response itself is still recorded by the caller.
        return False
    if progress.state == InjectState.resolved:
        return False

    now = datetime.now(UTC)
    progress.state = InjectState.resolved
    progress.resolved_at = now
    progress.resolved_by = actor_id
    progress.resolution_reason = "participant_response"
    session.add(progress)

    # A team-specific physical inject has one progression context. A shared inject
    # closes only after every enrolled participant context has resolved; observers
    # and facilitators can never submit responses and therefore do not count.
    resolutions = (
        await session.exec(
            select(InjectProgress).where(InjectProgress.inject_id == inject.id)
        )
    ).all()
    complete = bool(resolutions) and all(
        row.state == InjectState.resolved for row in resolutions
    )
    if complete:
        inject.state = InjectState.resolved
        inject.resolved_at = now
        inject.resolved_by = actor_id
        inject.resolution_reason = "participant_response"
        session.add(inject)

    # Ad-hoc and approved-suggested injects have no scenario node: they resolve
    # like any inject, but they are interruptions, not steps on a path (#256).
    # Writing the cursor here would set current_node_id=None and current_inject_id
    # to this inject, which release_is_allowed reads as "advanced to a dead end" —
    # permanently refusing the branch the team was actually on.
    if inject.scenario_node_id is None:
        return True

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


async def release_is_allowed(
    session: AsyncSession, inject: Inject, *, scheduled: bool = False
) -> bool:
    """Release a scenario node only for a context currently pointing at it.

    Legacy exercises without progression rows remain operable. Independent root
    nodes are also valid opening choices before a context has advanced.

    ``scheduled`` marks a release fired by an inject's own ``release_at_minutes``
    rather than picked by a facilitator (#218). The cursor lock below exists to stop
    a facilitator hand-picking a node off the branch the participants chose; a
    schedule is declared in the scenario before the exercise ran and picks nothing,
    so it is exempt — but only as far as the *unreferenced*-node check below, which
    still refuses a node that some branch does link to.
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
    if not scheduled and any(cursor.current_inject_id is not None for cursor in cursors):
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
