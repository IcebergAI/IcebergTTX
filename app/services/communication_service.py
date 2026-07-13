import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import desc
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import CommDirection, Communication, CommunicationRead
from app.models.exercise import ExerciseMember
from app.models.inject import Inject
from app.models.user import User
from app.schemas.api import CommunicationPublic
from app.services.domain_events import CommunicationCreated, dispatch, record
from app.services.scenario_service import definition_for_exercise

logger = logging.getLogger(__name__)


async def create_communication(
    session: AsyncSession,
    *,
    exercise_id: int,
    direction: CommDirection,
    subject: str,
    body: str,
    sender_id: int | None = None,
    sender_team: str | None = None,
    external_entity: str | None = None,
    triggered_by_inject_id: int | None = None,
    visible_to_teams: list[str] | None = None,
) -> Communication:
    comm = Communication(
        exercise_id=exercise_id,
        sender_id=sender_id,
        sender_team=sender_team,
        direction=direction,
        external_entity=external_entity,
        subject=subject,
        body=body,
        triggered_by_inject_id=triggered_by_inject_id,
        visible_to_teams=visible_to_teams or None,
    )
    session.add(comm)
    # Flush for the id: the event names the row rather than carrying it, because the
    # delayed-trigger path below learns its id from INSERT ... RETURNING and never
    # loads the object.
    await session.flush()
    assert comm.id is not None
    record(session, CommunicationCreated(exercise_id=exercise_id, communication_id=comm.id))
    await session.commit()
    await session.refresh(comm)
    await dispatch(session)
    return comm


async def mark_read(
    session: AsyncSession, communication_id: int, user_id: int
) -> CommunicationRead:
    """Record the user's first read atomically and idempotently.

    ``ON CONFLICT DO NOTHING`` prevents both duplicate retries and concurrent
    inserts from changing the original ``read_at`` timestamp.
    """
    statement = (
        pg_insert(CommunicationRead)
        .values(
            communication_id=communication_id,
            user_id=user_id,
            read_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["communication_id", "user_id"])
    )
    await session.exec(statement)
    await session.commit()
    receipt = await session.get(CommunicationRead, (communication_id, user_id))
    if receipt is None:  # defensive: both referenced rows were already validated
        raise RuntimeError("Communication read receipt was not persisted")
    return receipt


async def communication_read_at(
    session: AsyncSession, communication_id: int, user_id: int
) -> datetime | None:
    receipt = await session.get(CommunicationRead, (communication_id, user_id))
    return receipt.read_at if receipt else None


async def communication_read_times(
    session: AsyncSession, communication_ids: list[int], user_id: int
) -> dict[int, datetime]:
    """Batch the inbox's viewer-specific read state into one targeted query."""
    if not communication_ids:
        return {}
    receipts = (
        await session.exec(
            select(CommunicationRead).where(
                CommunicationRead.user_id == user_id,
                col(CommunicationRead.communication_id).in_(communication_ids),
            )
        )
    ).all()
    return {receipt.communication_id: receipt.read_at for receipt in receipts}


async def sender_teams_for_comms(
    session: AsyncSession, comms: Sequence[Communication]
) -> dict[int, str | None]:
    """Batch the inbox's sender-team resolution into two queries instead of two per row.

    Same precedence as sender_team_for_comm(), which this replaces for list-shaped
    callers: the denormalised column, then the sender's ExerciseMember.group_id, then
    their global User.team. Note that a sender with neither resolves to None *and stores
    None*, so the per-row version re-queried those rows on every single load, forever —
    a backfill of Communication.sender_team would not have fixed that.
    """
    resolved: dict[int, str | None] = {}
    unresolved: list[Communication] = []
    for c in comms:
        if c.id is None:
            continue
        if c.sender_team:
            resolved[c.id] = c.sender_team
        elif c.sender_id is None:
            resolved[c.id] = None
        else:
            unresolved.append(c)
    if not unresolved:
        return resolved

    exercise_ids = {c.exercise_id for c in unresolved}
    sender_ids = {c.sender_id for c in unresolved if c.sender_id is not None}
    members = (
        await session.exec(
            select(ExerciseMember).where(
                col(ExerciseMember.exercise_id).in_(exercise_ids),
                col(ExerciseMember.user_id).in_(sender_ids),
            )
        )
    ).all()
    group_ids = {(m.exercise_id, m.user_id): m.group_id for m in members}

    # Only senders with no exercise-scoped group need their global team looked up.
    fallback_ids = {
        c.sender_id
        for c in unresolved
        if c.sender_id is not None and not group_ids.get((c.exercise_id, c.sender_id))
    }
    user_teams: dict[int, str | None] = {}
    if fallback_ids:
        users = (
            await session.exec(select(User).where(col(User.id).in_(fallback_ids)))
        ).all()
        user_teams = {u.id: u.team for u in users if u.id is not None}

    for c in unresolved:
        assert c.id is not None and c.sender_id is not None
        resolved[c.id] = group_ids.get((c.exercise_id, c.sender_id)) or user_teams.get(
            c.sender_id
        )
    return resolved


async def list_communications(
    session: AsyncSession,
    exercise_id: int,
    user_id: int | None = None,
    user_team: str | None = None,
    participant_view: bool = False,
) -> list[Communication]:
    """Return comms visible to the given participant team (or all for facilitators)."""
    comms = (
        await session.exec(
            select(Communication)
            .where(Communication.exercise_id == exercise_id)
            .order_by(desc(cast(Any, Communication.sent_at)))
        )
    ).all()

    if not participant_view and user_team is None:
        return list(comms)

    sender_teams = await sender_teams_for_comms(session, comms)
    visible = []
    for c in comms:
        teams = c.visible_to_teams
        if c.direction == CommDirection.outbound:
            sender_team = sender_teams.get(c.id) if c.id is not None else None
            sent_by_user = c.sender_id == user_id and (
                sender_team is None or sender_team == user_team
            )
            received_by_team = teams is not None and user_team is not None and user_team in teams
            if sent_by_user or received_by_team:
                visible.append(c)
            continue
        if teams is None or (user_team is not None and user_team in teams):
            visible.append(c)
    return visible


async def unread_count(
    session: AsyncSession,
    exercise_id: int,
    user_id: int,
    user_team: str | None = None,
    participant_view: bool = False,
) -> int:
    """Count the comms this viewer can see but has not yet opened.

    Deliberately layered on list_communications() rather than a bespoke COUNT, so
    the badge can never disagree with the inbox it summarises — a count that
    applied looser visibility rules would leak the existence of messages the
    viewer is not allowed to read.
    """
    comms = await list_communications(
        session,
        exercise_id,
        user_id=user_id,
        user_team=user_team,
        participant_view=participant_view,
    )
    comm_ids = [c.id for c in comms if c.id is not None]
    read_times = await communication_read_times(session, comm_ids, user_id)
    return sum(1 for comm_id in comm_ids if comm_id not in read_times)


async def sender_team_for_comm(session: AsyncSession | None, comm: Communication) -> str | None:
    if comm.sender_team:
        return comm.sender_team
    if session is None or comm.sender_id is None:
        return None
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == comm.exercise_id)
            .where(ExerciseMember.user_id == comm.sender_id)
        )
    ).first()
    if member and member.group_id:
        return member.group_id
    user = await session.get(User, comm.sender_id)
    return user.team if user else None


async def all_team_ids_for_exercise(session: AsyncSession, exercise_id: int) -> list[str]:
    definition = await definition_for_exercise(session, exercise_id)
    if not definition:
        return []
    return [team.id for team in definition.participant_teams]


async def visible_to_teams_for_payload(
    c: Communication, session: AsyncSession | None = None
) -> list[str] | None:
    if c.visible_to_teams:
        return c.visible_to_teams
    if c.direction == CommDirection.inbound and session is not None:
        return await all_team_ids_for_exercise(session, c.exercise_id) or None
    return None


async def comm_payload(
    c: Communication,
    session: AsyncSession | None = None,
    *,
    read_at: datetime | None = None,
    sender_teams: dict[int, str | None] | None = None,
) -> dict:
    """Build one comm's API payload.

    List callers pass a pre-batched ``sender_teams`` map (see sender_teams_for_comms)
    the same way they pass ``read_at`` from communication_read_times(); single-comm
    callers omit it and take the per-row lookup. Membership, not truthiness, decides
    whether the map answers: None is a legitimate resolved team.
    """
    assert c.id is not None
    sender_team = (
        sender_teams[c.id]
        if sender_teams is not None and c.id in sender_teams
        else await sender_team_for_comm(session, c)
    )
    return CommunicationPublic(
        id=c.id,
        exercise_id=c.exercise_id,
        sender_id=c.sender_id,
        sender_team=sender_team,
        direction=c.direction,
        external_entity=c.external_entity,
        subject=c.subject,
        body=c.body,
        triggered_by_inject_id=c.triggered_by_inject_id,
        visible_to_teams=await visible_to_teams_for_payload(c, session),
        sent_at=c.sent_at.isoformat(),
        is_read=read_at is not None,
        read_at=read_at.isoformat() if read_at else None,
    ).model_dump(mode="json")




def schedule_triggered_comms(
    inject: Inject,
    trigger_comms: list,  # list[TriggerComm] from scenario definition
    logical_node_id: str,
) -> None:
    """Schedule node-level all-team communications once, across group-specific injects."""
    assert inject.id is not None
    from app.services.schedule_service import arm_triggered_communication

    for index, tc in enumerate(trigger_comms):
        arm_triggered_communication(
            exercise_id=inject.exercise_id,
            inject_id=inject.id,
            direction=tc.direction,
            external_entity=tc.external_entity,
            subject=tc.subject,
            body=tc.body,
            delay=tc.delay_after_release_seconds,
            trigger_key=f"{logical_node_id}:{index}",
        )


async def deliver_triggered_communication(
    session: AsyncSession,
    *,
    exercise_id: int,
    inject_id: int,
    direction: str,
    external_entity: str,
    subject: str,
    body: str,
    trigger_key: str,
) -> Communication | None:
    """Persist one logical trigger exactly once; its frame follows from the commit (#212).

    The restart-safe timer and session are owned by schedule_service (#240) — this only
    persists and announces. ``on_conflict_do_nothing`` means a replayed trigger returns no
    id and records nothing, so the de-duplication now suppresses the frame *by
    construction* rather than by an ``if`` placed after the commit.
    """
    statement = (
        pg_insert(Communication)
        .values(
            exercise_id=exercise_id,
            direction=CommDirection(direction),
            subject=subject,
            body=body,
            external_entity=external_entity,
            triggered_by_inject_id=inject_id,
            trigger_key=trigger_key,
            sent_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(constraint="uq_communication_exercise_trigger_key")
        .returning(col(Communication.id))
    )
    comm_id = (await session.exec(statement)).scalar_one_or_none()
    if comm_id is not None:
        record(session, CommunicationCreated(exercise_id=exercise_id, communication_id=comm_id))
    await session.commit()
    await dispatch(session)
    if comm_id is None:
        return None
    comm = await session.get(Communication, comm_id)
    if comm is None:
        raise RuntimeError("Triggered communication was not persisted")
    return comm
