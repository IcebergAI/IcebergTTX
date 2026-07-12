import asyncio
import logging
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
from app.services.background import spawn
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
    await session.commit()
    await session.refresh(comm)
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

    visible = []
    for c in comms:
        teams = c.visible_to_teams
        if c.direction == CommDirection.outbound:
            sender_team = await sender_team_for_comm(session, c)
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
) -> dict:
    assert c.id is not None
    return CommunicationPublic(
        id=c.id,
        exercise_id=c.exercise_id,
        sender_id=c.sender_id,
        sender_team=await sender_team_for_comm(session, c),
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


async def broadcast_communication(comm: Communication) -> None:
    from app.services.ws_manager import manager

    teams = comm.visible_to_teams
    message = {
        "type": "communication_received",
        "exercise_id": comm.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": await comm_payload(comm),
    }
    if comm.direction == CommDirection.outbound:
        if teams:
            await manager.send_to_facilitators_user_and_groups(
                comm.exercise_id, comm.sender_id, teams, message
            )
        else:
            await manager.send_to_facilitators_and_user(comm.exercise_id, comm.sender_id, message)
    elif teams:
        await manager.broadcast_to_groups(comm.exercise_id, teams, message)
    else:
        await manager.broadcast_to_exercise(comm.exercise_id, message)


def schedule_triggered_comms(
    inject: Inject,
    trigger_comms: list,  # list[TriggerComm] from scenario definition
    logical_node_id: str,
) -> None:
    """Schedule node-level all-team communications once, across group-specific injects."""
    assert inject.id is not None
    for index, tc in enumerate(trigger_comms):
        spawn(
            _delayed_comm(
                exercise_id=inject.exercise_id,
                inject_id=inject.id,
                direction=tc.direction,
                external_entity=tc.external_entity,
                subject=tc.subject,
                body=tc.body,
                delay=tc.delay_after_release_seconds,
                trigger_key=f"{logical_node_id}:{index}",
            )
        )


async def _delayed_comm(
    *,
    exercise_id: int,
    inject_id: int,
    direction: str,
    external_entity: str,
    subject: str,
    body: str,
    delay: int,
    trigger_key: str,
) -> None:
    try:
        if delay > 0:
            await asyncio.sleep(delay)

        from app.database import engine

        async with AsyncSession(engine, expire_on_commit=False) as session:
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
            await session.commit()
            if comm_id is not None:
                comm = await session.get(Communication, comm_id)
                assert comm is not None
                await broadcast_communication(comm)
    except Exception:
        logger.exception("Delayed comm failed for inject %d", inject_id)
