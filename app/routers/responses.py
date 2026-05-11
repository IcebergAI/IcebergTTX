import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import get_current_user, require_role
from app.models.assessment import ResponseAssessment
from app.models.exercise import Exercise
from app.models.response import Response
from app.models.user import User, UserRole
from app.services.inject_service import get_inject_or_404
from app.services.llm_service import _assessment_payload, run_llm_pipeline
from app.services.response_service import broadcast_response_submitted, submit_response

router = APIRouter(prefix="/exercises/{exercise_id}/responses", tags=["responses"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[Session, Depends(get_session)]


class SubmitResponseRequest(BaseModel):
    inject_id: int
    content: str
    selected_option: str | None = None


def _response_out(r: Response) -> dict:
    return {
        "id": r.id,
        "inject_id": r.inject_id,
        "exercise_id": r.exercise_id,
        "user_id": r.user_id,
        "content": r.content,
        "selected_option": r.selected_option,
        "submitted_at": r.submitted_at.isoformat(),
        "assessment_id": r.assessment_id,
    }


@router.get("")
def list_responses(
    exercise_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    q = select(Response).where(Response.exercise_id == exercise_id)
    if current_user.role == UserRole.participant:
        q = q.where(Response.user_id == current_user.id)
    return [_response_out(r) for r in session.exec(q).all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def submit(
    exercise_id: int,
    body: SubmitResponseRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    get_inject_or_404(session, exercise_id, body.inject_id)
    response, next_ids = submit_response(
        session,
        inject_id=body.inject_id,
        exercise_id=exercise_id,
        user_id=current_user.id,
        content=body.content,
        selected_option=body.selected_option,
    )
    await broadcast_response_submitted(response, next_ids)

    exercise = session.get(Exercise, exercise_id)
    if exercise and exercise.llm_enabled:
        asyncio.create_task(
            run_llm_pipeline(response.id, body.inject_id, exercise_id)
        )

    return _response_out(response)


@router.get("/{response_id}")
def get_response(
    exercise_id: int,
    response_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    r = session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    if current_user.role == UserRole.participant and r.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return _response_out(r)


@router.post("/{response_id}/assess", status_code=status.HTTP_202_ACCEPTED)
async def trigger_assess(
    exercise_id: int,
    response_id: int,
    _: FacilitatorDep,
    session: SessionDep,
):
    r = session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    asyncio.create_task(run_llm_pipeline(response_id, r.inject_id, exercise_id))
    return {"detail": "Assessment queued"}


@router.get("/{response_id}/assessment")
def get_assessment(
    exercise_id: int,
    response_id: int,
    _: FacilitatorDep,
    session: SessionDep,
):
    r = session.get(Response, response_id)
    if not r or r.exercise_id != exercise_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")
    if r.assessment_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No assessment yet")
    assessment = session.get(ResponseAssessment, r.assessment_id)
    if not assessment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assessment not found")
    return _assessment_payload(assessment)
