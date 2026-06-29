from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.communication import CommDirection, Communication
from app.models.user import User, UserRole
from app.schemas.api import CommunicationPublic
from app.services.access_control import exercise_group_for_user, require_exercise_access
from app.services.communication_service import (
    all_team_ids_for_exercise,
    broadcast_communication,
    comm_payload,
    create_communication,
    list_communications,
    mark_read,
    sender_team_for_comm,
)
from app.services.exercise_service import validate_group_id

router = APIRouter(prefix="/exercises/{exercise_id}/communications", tags=["communications"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class SendCommRequest(BaseModel):
    direction: CommDirection
    subject: str
    body: str
    external_entity: str | None = None
    visible_to_teams: list[str] | None = None


class InjectCommRequest(BaseModel):
    """Facilitator injects a simulated inbound communication."""
    external_entity: str
    subject: str
    body: str
    visible_to_teams: list[str] | None = None


async def _get_comm_or_404(
    session: AsyncSession, exercise_id: int, comm_id: int
) -> Communication:
    c = await session.get(Communication, comm_id)
    if not c or c.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Communication not found")
    return c


async def _comm_visible_to_user(session: AsyncSession, comm: Communication, user: User) -> bool:
    if user.role in (UserRole.facilitator, UserRole.observer):
        return True
    if comm.direction == CommDirection.outbound:
        group_id = await exercise_group_for_user(session, comm.exercise_id, user) or user.team
        sender_team = await sender_team_for_comm(session, comm)
        sent_by_user = comm.sender_id == user.id and (
            sender_team is None or sender_team == group_id
        )
        if sent_by_user:
            return True
        if not comm.visible_to_teams:
            return False
        import json

        return group_id in json.loads(comm.visible_to_teams)
    if not comm.visible_to_teams:
        return True
    import json

    group_id = await exercise_group_for_user(session, comm.exercise_id, user) or user.team
    return group_id in json.loads(comm.visible_to_teams)


@router.get("", response_model=list[CommunicationPublic])
async def list_comms(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    await require_exercise_access(session, exercise_id, current_user)
    team = None
    if current_user.role == UserRole.participant:
        team = (
            await exercise_group_for_user(session, exercise_id, current_user)
            or current_user.team
        )
    comms = await list_communications(
        session,
        exercise_id,
        user_id=current_user.id,
        user_team=team,
        participant_view=current_user.role == UserRole.participant,
    )
    return [await comm_payload(c, session) for c in comms]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CommunicationPublic)
async def send_comm(
    exercise_id: int,
    body: SendCommRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    exercise = await require_exercise_access(session, exercise_id, current_user)
    if current_user.role != UserRole.participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only participants can send outbound communications",
        )
    if body.direction != CommDirection.outbound:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Participants can only send outbound communications",
        )
    sender_team = (
        await exercise_group_for_user(session, exercise_id, current_user) or current_user.team
    )
    validated_teams = []
    for team_id in (body.visible_to_teams or []):
        team = await validate_group_id(session, exercise, team_id)
        if team is not None:
            validated_teams.append(team)
    visible_to_teams = validated_teams or None
    comm = await create_communication(
        session,
        exercise_id=exercise_id,
        direction=body.direction,
        subject=body.subject,
        body=body.body,
        sender_id=current_user.id,
        sender_team=sender_team,
        external_entity=None if visible_to_teams else body.external_entity,
        visible_to_teams=visible_to_teams,
    )
    await broadcast_communication(comm)
    return await comm_payload(comm, session)


@router.post("/inject", status_code=status.HTTP_201_CREATED, response_model=CommunicationPublic)
async def inject_comm(
    exercise_id: int,
    body: InjectCommRequest,
    _: FacilitatorDep,
    session: SessionDep,
):
    await require_exercise_access(session, exercise_id, _)
    """Facilitator injects a simulated inbound message (e.g. from ICO, press)."""
    visible_to_teams = (
        body.visible_to_teams
        or await all_team_ids_for_exercise(session, exercise_id)
        or None
    )
    comm = await create_communication(
        session,
        exercise_id=exercise_id,
        direction=CommDirection.inbound,
        subject=body.subject,
        body=body.body,
        external_entity=body.external_entity,
        visible_to_teams=visible_to_teams,
    )
    await broadcast_communication(comm)
    return await comm_payload(comm, session)


@router.get("/{comm_id}", response_model=CommunicationPublic)
async def get_comm(
    exercise_id: int,
    comm_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    assert current_user.id is not None
    await require_exercise_access(session, exercise_id, current_user)
    c = await _get_comm_or_404(session, exercise_id, comm_id)
    if not await _comm_visible_to_user(session, c, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Communication not found")
    updated = await mark_read(session, c, current_user.id)
    return await comm_payload(updated, session)
