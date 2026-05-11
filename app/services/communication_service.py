import asyncio
import json
import logging
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models.communication import CommDirection, Communication
from app.models.inject import Inject

logger = logging.getLogger(__name__)


def create_communication(
    session: Session,
    *,
    exercise_id: int,
    direction: CommDirection,
    subject: str,
    body: str,
    sender_id: int | None = None,
    external_entity: str | None = None,
    triggered_by_inject_id: int | None = None,
    visible_to_teams: list[str] | None = None,
) -> Communication:
    comm = Communication(
        exercise_id=exercise_id,
        sender_id=sender_id,
        direction=direction,
        external_entity=external_entity,
        subject=subject,
        body=body,
        triggered_by_inject_id=triggered_by_inject_id,
        visible_to_teams=json.dumps(visible_to_teams) if visible_to_teams else None,
    )
    session.add(comm)
    session.commit()
    session.refresh(comm)
    return comm


def mark_read(session: Session, comm: Communication, user_id: int) -> Communication:
    readers: list[int] = json.loads(comm.read_by) if comm.read_by else []
    if user_id not in readers:
        readers.append(user_id)
        comm.read_by = json.dumps(readers)
        session.add(comm)
        session.commit()
        session.refresh(comm)
    return comm


def list_communications(
    session: Session,
    exercise_id: int,
    user_team: str | None = None,
) -> list[Communication]:
    """Return comms visible to the given team (or all if team is None / facilitator)."""
    comms = session.exec(
        select(Communication)
        .where(Communication.exercise_id == exercise_id)
        .order_by(Communication.sent_at)
    ).all()

    if user_team is None:
        return list(comms)

    visible = []
    for c in comms:
        teams = json.loads(c.visible_to_teams) if c.visible_to_teams else None
        if teams is None or user_team in teams:
            visible.append(c)
    return visible


def comm_payload(c: Communication) -> dict:
    return {
        "id": c.id,
        "exercise_id": c.exercise_id,
        "sender_id": c.sender_id,
        "direction": c.direction,
        "external_entity": c.external_entity,
        "subject": c.subject,
        "body": c.body,
        "triggered_by_inject_id": c.triggered_by_inject_id,
        "visible_to_teams": json.loads(c.visible_to_teams) if c.visible_to_teams else None,
        "sent_at": c.sent_at.isoformat(),
        "read_by": json.loads(c.read_by) if c.read_by else [],
    }


async def broadcast_communication(comm: Communication) -> None:
    from app.services.ws_manager import manager

    teams = json.loads(comm.visible_to_teams) if comm.visible_to_teams else None
    message = {
        "type": "communication_received",
        "exercise_id": comm.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": comm_payload(comm),
    }
    if teams:
        await manager.broadcast_to_teams(comm.exercise_id, teams, message)
    else:
        await manager.broadcast_to_exercise(comm.exercise_id, message)


def schedule_triggered_comms(
    session: Session,
    inject: Inject,
    trigger_comms: list,  # list[TriggerComm] from scenario definition
) -> None:
    """Fire asyncio tasks to create each triggered communication after its delay."""
    for tc in trigger_comms:
        asyncio.create_task(
            _delayed_comm(
                exercise_id=inject.exercise_id,
                inject_id=inject.id,
                direction=tc.direction,
                external_entity=tc.external_entity,
                subject=tc.subject,
                body=tc.body,
                delay=tc.delay_after_release_seconds,
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
) -> None:
    try:
        if delay > 0:
            await asyncio.sleep(delay)

        from sqlmodel import Session as _Session

        from app.database import engine

        with _Session(engine) as session:
            comm = create_communication(
                session,
                exercise_id=exercise_id,
                direction=CommDirection(direction),
                subject=subject,
                body=body,
                external_entity=external_entity,
                triggered_by_inject_id=inject_id,
            )
            await broadcast_communication(comm)
    except Exception:
        logger.exception("Delayed comm failed for inject %d", inject_id)
