from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.communication import CommDirection, Communication
from app.models.user import User, UserRole
from app.services.access_control import require_exercise_access
from app.services.communication_service import (
    broadcast_communication,
    comm_payload,
    create_communication,
    list_communications,
    mark_read,
)

router = APIRouter(prefix="/exercises/{exercise_id}/communications", tags=["communications"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[Session, Depends(get_session)]


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


def _get_comm_or_404(session: Session, exercise_id: int, comm_id: int) -> Communication:
    c = session.get(Communication, comm_id)
    if not c or c.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Communication not found")
    return c


def _comm_visible_to_user(comm: Communication, user: User) -> bool:
    if user.role in (UserRole.facilitator, UserRole.observer):
        return True
    if not comm.visible_to_teams:
        return True
    import json

    return user.team in json.loads(comm.visible_to_teams)


@router.get("")
def list_comms(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    require_exercise_access(session, exercise_id, current_user)
    team = current_user.team if current_user.role == UserRole.participant else None
    return [comm_payload(c) for c in list_communications(session, exercise_id, user_team=team)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def send_comm(
    exercise_id: int,
    body: SendCommRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    require_exercise_access(session, exercise_id, current_user)
    if current_user.role != UserRole.participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only participants can send outbound communications",
        )
    comm = create_communication(
        session,
        exercise_id=exercise_id,
        direction=body.direction,
        subject=body.subject,
        body=body.body,
        sender_id=current_user.id,
        external_entity=body.external_entity,
        visible_to_teams=body.visible_to_teams,
    )
    await broadcast_communication(comm)
    return comm_payload(comm)


@router.post("/inject", status_code=status.HTTP_201_CREATED)
async def inject_comm(
    exercise_id: int,
    body: InjectCommRequest,
    _: FacilitatorDep,
    session: SessionDep,
):
    require_exercise_access(session, exercise_id, _)
    """Facilitator injects a simulated inbound message (e.g. from ICO, press)."""
    comm = create_communication(
        session,
        exercise_id=exercise_id,
        direction=CommDirection.inbound,
        subject=body.subject,
        body=body.body,
        external_entity=body.external_entity,
        visible_to_teams=body.visible_to_teams,
    )
    await broadcast_communication(comm)
    return comm_payload(comm)


@router.get("/{comm_id}")
def get_comm(
    exercise_id: int,
    comm_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    assert current_user.id is not None
    require_exercise_access(session, exercise_id, current_user)
    c = _get_comm_or_404(session, exercise_id, comm_id)
    if not _comm_visible_to_user(c, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Communication not found")
    updated = mark_read(session, c, current_user.id)
    return comm_payload(updated)
