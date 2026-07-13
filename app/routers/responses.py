from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.assessment import ResponseAssessment
from app.models.exercise import ExerciseState
from app.models.inject import InjectState
from app.models.response import Response
from app.models.user import User, UserRole
from app.schemas.api import AssessmentPublic, ResponsePublic, SubmitResponseRequest
from app.services.access_control import (
    exercise_group_for_user,
    require_exercise_access,
    require_exercise_owner,
    require_inject_visible,
    require_operational_mutability,
)
from app.services.inject_service import get_inject_or_404
from app.services.llm_service import assessment_payload, queue_llm_pipeline
from app.services.progression_service import progression_snapshot
from app.services.response_service import (
    response_next_inject_suggestions,
    response_payload,
    response_validation_error,
    submit_response,
)
from app.services.scenario_service import get_inject_node, get_scenario_definition

router = APIRouter(prefix="/exercises/{exercise_id}/responses", tags=["responses"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


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
    require_operational_mutability(exercise)
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

    validation_error = response_validation_error(
        node,
        content=body.content,
        selected_option=body.selected_option,
    )
    if validation_error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=validation_error,
        )

    group_id = await exercise_group_for_user(session, exercise_id, current_user)

    response, _next_injects = await submit_response(
        session,
        inject_id=body.inject_id,
        exercise_id=exercise_id,
        user_id=current_user.id,
        content=body.content,
        selected_option=body.selected_option,
        group_id=group_id,
    )
    # The facilitator frame (response + resolved branch + progression) is projected from
    # the committed ResponseSubmitted event, dispatched by submit_response itself.

    if exercise.llm_enabled:
        assert response.id is not None
        queue_llm_pipeline(response.id, body.inject_id, exercise_id)

    participant_progression = await progression_snapshot(session, exercise_id, group_id=group_id)
    return response_payload(response, progression=participant_progression)


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
    current_user: FacilitatorDep,
    session: SessionDep,
):
    exercise = await require_exercise_owner(session, exercise_id, current_user)
    if not exercise.llm_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="AI assessment is disabled for this exercise",
        )
    r = await session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    existing = (
        await session.exec(
            select(ResponseAssessment).where(ResponseAssessment.response_id == response_id)
        )
    ).first()
    if existing is not None:
        if r.assessment_id != existing.id:
            r.assessment_id = existing.id
            session.add(r)
            await session.commit()
        return {"detail": "Assessment already exists"}
    queued = queue_llm_pipeline(response_id, r.inject_id, exercise_id)
    return {"detail": "Assessment queued" if queued else "Assessment already queued"}


@router.get("/{response_id}/assessment", response_model=AssessmentPublic)
async def get_assessment(
    exercise_id: int,
    response_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    await require_exercise_access(session, exercise_id, current_user)
    r = await session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    if r.assessment_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No assessment yet")
    assessment = await session.get(ResponseAssessment, r.assessment_id)
    if not assessment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")
    return assessment_payload(assessment)
