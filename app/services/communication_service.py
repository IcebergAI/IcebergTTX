import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import desc
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import CommDirection, Communication
from app.models.exercise import Exercise, ExerciseMember
from app.models.inject import Inject
from app.models.scenario import Scenario
from app.models.user import User
from app.services.scenario_service import export_definition

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
        visible_to_teams=json.dumps(visible_to_teams) if visible_to_teams else None,
    )
    session.add(comm)
    await session.commit()
    await session.refresh(comm)
    return comm


async def mark_read(session: AsyncSession, comm: Communication, user_id: int) -> Communication:
    readers: list[int] = json.loads(comm.read_by) if comm.read_by else []
    if user_id not in readers:
        readers.append(user_id)
        comm.read_by = json.dumps(readers)
        session.add(comm)
        await session.commit()
        await session.refresh(comm)
    return comm


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
        teams = json.loads(c.visible_to_teams) if c.visible_to_teams else None
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
    exercise = await session.get(Exercise, exercise_id)
    if not exercise:
        return []
    scenario = await session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return []
    definition = export_definition(scenario)
    return [team.id for team in definition.participant_teams]


async def visible_to_teams_for_payload(
    c: Communication, session: AsyncSession | None = None
) -> list[str] | None:
    if c.visible_to_teams:
        return json.loads(c.visible_to_teams)
    if c.direction == CommDirection.inbound and session is not None:
        return await all_team_ids_for_exercise(session, c.exercise_id) or None
    return None


async def comm_payload(c: Communication, session: AsyncSession | None = None) -> dict:
    from app.schemas.api import CommunicationPublic

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
        read_by=json.loads(c.read_by) if c.read_by else [],
    ).model_dump(mode="json")


async def broadcast_communication(comm: Communication) -> None:
    from app.services.ws_manager import manager

    teams = json.loads(comm.visible_to_teams) if comm.visible_to_teams else None
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
        await manager.broadcast_to_teams(comm.exercise_id, teams, message)
    else:
        await manager.broadcast_to_exercise(comm.exercise_id, message)


def schedule_triggered_comms(
    inject: Inject,
    trigger_comms: list,  # list[TriggerComm] from scenario definition
) -> None:
    """Fire asyncio tasks to create each triggered communication after its delay."""
    from app.services.background import spawn

    assert inject.id is not None
    for tc in trigger_comms:
        spawn(
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

        from app.database import engine

        async with AsyncSession(engine, expire_on_commit=False) as session:
            comm = await create_communication(
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
