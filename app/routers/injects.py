import json
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.exercise import ExerciseState
from app.models.inject import Inject
from app.models.user import User, UserRole
from app.services.access_control import (
    require_exercise_access,
    require_inject_visible,
)
from app.services.exercise_service import validate_group_id
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
    group_id: str | None = None
    sequence_order: int = 0


def _inject_options(session: Session, inject: Inject) -> list[dict]:
    if not inject.scenario_node_id:
        return []
    from app.models.exercise import Exercise
    from app.models.scenario import Scenario
    from app.services.scenario_service import export_definition, get_inject_node

    exercise = session.get(Exercise, inject.exercise_id)
    if not exercise:
        return []
    scenario = session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return []
    node = get_inject_node(export_definition(scenario), inject.scenario_node_id)
    if not node:
        return []
    return [
        {"id": option.id, "label": option.label, "next_inject_id": option.next_inject_id}
        for option in node.options
    ]


def _inject_out(inject: Inject, session: Session | None = None) -> dict:
    data = {
        "id": inject.id,
        "exercise_id": inject.exercise_id,
        "scenario_node_id": inject.scenario_node_id,
        "title": inject.title,
        "content": inject.content,
        "target_teams": json.loads(inject.target_teams) if inject.target_teams else None,
        "group_id": inject.group_id,
        "sequence_order": inject.sequence_order,
        "state": inject.state,
        "released_at": inject.released_at.isoformat() if inject.released_at else None,
        "released_by": inject.released_by,
    }
    if session is not None:
        data["options"] = _inject_options(session, inject)
    return data


@router.get("")
def list_injects(exercise_id: int, current_user: CurrentUserDep, session: SessionDep):
    require_exercise_access(session, exercise_id, current_user)
    injects = session.exec(
        select(Inject)
        .where(Inject.exercise_id == exercise_id)
        .order_by(cast(Any, Inject.sequence_order))
    ).all()
    visible = [
        i
        for i in injects
        if current_user.role == UserRole.facilitator
        or require_visible_bool(session, i, current_user)
    ]
    return [_inject_out(i, session) for i in visible]


@router.post("", status_code=status.HTTP_201_CREATED)
def create(
    exercise_id: int,
    body: CreateInjectRequest,
    _: FacilitatorDep,
    session: SessionDep,
):
    exercise = require_exercise_access(session, exercise_id, _)
    group_id = validate_group_id(session, exercise, body.group_id)
    if group_id is None and body.target_teams and len(body.target_teams) == 1:
        group_id = validate_group_id(session, exercise, body.target_teams[0])
    inject = create_inject(
        session,
        exercise_id=exercise_id,
        title=body.title,
        content=body.content,
        scenario_node_id=body.scenario_node_id,
        target_teams=body.target_teams,
        group_id=group_id,
        sequence_order=body.sequence_order,
    )
    return _inject_out(inject, session)


@router.get("/{inject_id}")
def get_inject(exercise_id: int, inject_id: int, current_user: CurrentUserDep, session: SessionDep):
    require_exercise_access(session, exercise_id, current_user)
    inject = get_inject_or_404(session, exercise_id, inject_id)
    require_inject_visible(session, inject, current_user)
    return _inject_out(inject, session)


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
    assert current_user.id is not None
    exercise = require_exercise_access(session, exercise_id, current_user)
    if exercise.state != ExerciseState.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only active exercises can release injects",
        )
    inject = get_inject_or_404(session, exercise_id, inject_id)
    return _inject_out(await release_inject(session, inject, released_by=current_user.id), session)


def require_visible_bool(session: Session, inject: Inject, user: User) -> bool:
    try:
        require_inject_visible(session, inject, user)
        return True
    except HTTPException:
        return False
