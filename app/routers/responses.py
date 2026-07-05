from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.assessment import ResponseAssessment
from app.models.exercise import ExerciseState
from app.models.inject import InjectState
from app.models.response import Response
from app.models.user import User, UserRole
from app.schemas.api import AssessmentPublic, ResponsePublic
from app.services.access_control import (
    exercise_group_for_user,
    require_exercise_access,
    require_inject_visible,
)
from app.services.background import spawn
from app.services.inject_service import get_inject_or_404
from app.services.llm_service import _assessment_payload, run_llm_pipeline
from app.services.response_service import (
    broadcast_response_submitted,
    response_next_inject_suggestions,
    response_payload,
    submit_response,
)
from app.services.scenario_service import get_inject_node, get_scenario_definition

router = APIRouter(prefix="/exercises/{exercise_id}/responses", tags=["responses"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class SubmitResponseRequest(BaseModel):
    inject_id: int
    content: str
    selected_option: str | None = None


@router.get("", response_model=list[ResponsePublic])
async def list_responses(
    exercise_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    await require_exercise_access(session, exercise_id, current_user)
    q = select(Response).where(Response.exercise_id == exercise_id)
    if current_user.role == UserRole.participant:
        q = q.where(Response.user_id == current_user.id)
        return [response_payload(r) for r in (await session.exec(q)).all()]
    return [
        response_payload(r, await response_next_inject_suggestions(session, r))
        for r in (await session.exec(q)).all()
    ]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ResponsePublic)
async def submit(
    exercise_id: int,
    body: SubmitResponseRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    assert current_user.id is not None
    exercise = await require_exercise_access(session, exercise_id, current_user)
    if current_user.role != UserRole.participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only participants can submit responses"
        )
    if exercise.state != ExerciseState.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Responses can only be submitted while the exercise is active",
        )

    inject = await get_inject_or_404(session, exercise_id, body.inject_id)
    await require_inject_visible(session, inject, current_user)
    if inject.state != InjectState.released:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Responses can only be submitted to released injects",
        )

    existing = (
        await session.exec(
            select(Response)
            .where(Response.exercise_id == exercise_id)
            .where(Response.inject_id == body.inject_id)
            .where(Response.user_id == current_user.id)
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Response already submitted for this inject",
        )

    definition = await get_scenario_definition(session, exercise.scenario_id)
    node = None
    if definition and inject.scenario_node_id is not None:
        node = get_inject_node(definition, inject.scenario_node_id)

    if node and (node.free_text_response or not node.options) and not body.content.strip():
        raise HTTPException(
            status_code=422,
            detail="content is required for this inject",
        )

    if body.selected_option is not None:
        if not node or body.selected_option not in {option.id for option in node.options}:
            raise HTTPException(
                status_code=422,
                detail="selected_option is not valid for this inject",
            )

    group_id = await exercise_group_for_user(session, exercise_id, current_user)

    response, next_injects = await submit_response(
        session,
        inject_id=body.inject_id,
        exercise_id=exercise_id,
        user_id=current_user.id,
        content=body.content,
        selected_option=body.selected_option,
        group_id=group_id,
    )
    await broadcast_response_submitted(response, next_injects)

    if exercise.llm_enabled:
        assert response.id is not None
        spawn(run_llm_pipeline(response.id, body.inject_id, exercise_id))

    return response_payload(response)


@router.get("/{response_id}", response_model=ResponsePublic)
async def get_response(
    exercise_id: int,
    response_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    await require_exercise_access(session, exercise_id, current_user)
    r = await session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    if current_user.role == UserRole.participant and r.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return response_payload(r)


@router.post("/{response_id}/assess", status_code=status.HTTP_202_ACCEPTED)
async def trigger_assess(
    exercise_id: int,
    response_id: int,
    _: FacilitatorDep,
    session: SessionDep,
):
    r = await session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    spawn(run_llm_pipeline(response_id, r.inject_id, exercise_id))
    return {"detail": "Assessment queued"}


@router.get("/{response_id}/assessment", response_model=AssessmentPublic)
async def get_assessment(
    exercise_id: int,
    response_id: int,
    _: FacilitatorDep,
    session: SessionDep,
):
    r = await session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    if r.assessment_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No assessment yet")
    assessment = await session.get(ResponseAssessment, r.assessment_id)
    if not assessment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")
    return _assessment_payload(assessment)
