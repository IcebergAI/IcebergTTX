import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import require_role
from app.models.inject import Inject
from app.models.suggested_inject import SuggestedInject, SuggestedInjectStatus
from app.models.user import User, UserRole
from app.services.inject_service import create_inject
from app.services.llm_service import _suggested_payload

router = APIRouter(prefix="/exercises/{exercise_id}/suggested-injects", tags=["suggested-injects"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
SessionDep = Annotated[Session, Depends(get_session)]


def _get_or_404(session: Session, exercise_id: int, suggested_id: int) -> SuggestedInject:
    s = session.get(SuggestedInject, suggested_id)
    if not s or s.exercise_id != exercise_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Suggested inject not found"
        )
    return s


@router.get("")
def list_suggested(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    items = session.exec(
        select(SuggestedInject).where(SuggestedInject.exercise_id == exercise_id)
    ).all()
    return [_suggested_payload(s) for s in items]


@router.post("/{suggested_id}/approve", status_code=status.HTTP_201_CREATED)
def approve(
    exercise_id: int,
    suggested_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    s = _get_or_404(session, exercise_id, suggested_id)
    if s.status != SuggestedInjectStatus.pending_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending_review suggestions can be approved",
        )


    existing = session.exec(
        select(Inject).where(Inject.exercise_id == exercise_id)
    ).all()
    next_order = max((i.sequence_order for i in existing), default=0) + 1

    target_teams = json.loads(s.target_teams) if s.target_teams else None
    group_id = target_teams[0] if target_teams and len(target_teams) == 1 else None
    inject = create_inject(
        session,
        exercise_id=exercise_id,
        title=s.title,
        content=s.content,
        target_teams=target_teams,
        group_id=group_id,
        sequence_order=next_order,
    )

    s.status = SuggestedInjectStatus.approved
    s.reviewed_by = current_user.id
    s.reviewed_at = datetime.now(UTC)
    session.add(s)
    session.commit()
    session.refresh(inject)

    return {
        "id": inject.id,
        "exercise_id": inject.exercise_id,
        "title": inject.title,
        "content": inject.content,
        "target_teams": target_teams,
        "group_id": inject.group_id,
        "state": inject.state,
        "sequence_order": inject.sequence_order,
    }


@router.post("/{suggested_id}/reject", status_code=status.HTTP_200_OK)
def reject(
    exercise_id: int,
    suggested_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    s = _get_or_404(session, exercise_id, suggested_id)
    if s.status != SuggestedInjectStatus.pending_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending_review suggestions can be rejected",
        )
    s.status = SuggestedInjectStatus.rejected
    s.reviewed_by = current_user.id
    s.reviewed_at = datetime.now(UTC)
    session.add(s)
    session.commit()
    session.refresh(s)
    return _suggested_payload(s)
