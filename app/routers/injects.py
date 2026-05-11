import json
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.inject import Inject
from app.models.user import User, UserRole
from app.services.inject_service import (
    create_inject,
    get_inject_or_404,
    release_inject,
)

router = APIRouter(prefix="/exercises/{exercise_id}/injects", tags=["injects"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[Session, Depends(get_session)]


class CreateInjectRequest(BaseModel):
    title: str
    content: str
    scenario_node_id: str | None = None
    target_teams: list[str] | None = None
    sequence_order: int = 0


def _inject_out(inject: Inject) -> dict:
    return {
        "id": inject.id,
        "exercise_id": inject.exercise_id,
        "scenario_node_id": inject.scenario_node_id,
        "title": inject.title,
        "content": inject.content,
        "target_teams": json.loads(inject.target_teams) if inject.target_teams else None,
        "sequence_order": inject.sequence_order,
        "state": inject.state,
        "released_at": inject.released_at.isoformat() if inject.released_at else None,
        "released_by": inject.released_by,
    }


@router.get("")
def list_injects(exercise_id: int, _: CurrentUserDep, session: SessionDep):
    injects = session.exec(
        select(Inject)
        .where(Inject.exercise_id == exercise_id)
        .order_by(Inject.sequence_order)
    ).all()
    return [_inject_out(i) for i in injects]


@router.post("", status_code=status.HTTP_201_CREATED)
def create(
    exercise_id: int,
    body: CreateInjectRequest,
    _: FacilitatorDep,
    session: SessionDep,
):
    inject = create_inject(
        session,
        exercise_id=exercise_id,
        title=body.title,
        content=body.content,
        scenario_node_id=body.scenario_node_id,
        target_teams=body.target_teams,
        sequence_order=body.sequence_order,
    )
    return _inject_out(inject)


@router.get("/{inject_id}")
def get_inject(exercise_id: int, inject_id: int, _: CurrentUserDep, session: SessionDep):
    return _inject_out(get_inject_or_404(session, exercise_id, inject_id))


@router.delete("/{inject_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_inject(exercise_id: int, inject_id: int, _: FacilitatorDep, session: SessionDep):
    inject = get_inject_or_404(session, exercise_id, inject_id)
    session.delete(inject)
    session.commit()


@router.post("/{inject_id}/release")
async def release(
    exercise_id: int,
    inject_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    inject = get_inject_or_404(session, exercise_id, inject_id)
    return _inject_out(await release_inject(session, inject, released_by=current_user.id))
