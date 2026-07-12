from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_role
from app.models.inject import Inject
from app.models.suggested_inject import SuggestedInject, SuggestedInjectStatus
from app.models.user import User, UserRole
from app.schemas.api import SuggestedInjectPublic
from app.services.inject_service import create_inject
from app.services.llm_service import _suggested_payload

router = APIRouter(prefix="/exercises/{exercise_id}/suggested-injects", tags=["suggested-injects"])

FacilitatorDep = Annotated[User, Depends(require_role(UserRole.facilitator))]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _get_or_404(
    session: AsyncSession, exercise_id: int, suggested_id: int
) -> SuggestedInject:
    s = await session.get(SuggestedInject, suggested_id)
    if not s or s.exercise_id != exercise_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Suggested inject not found"
        )
    return s


@router.get("", response_model=list[SuggestedInjectPublic])
async def list_suggested(exercise_id: int, current_user: FacilitatorDep, session: SessionDep):
    items = (
        await session.exec(
            select(SuggestedInject).where(SuggestedInject.exercise_id == exercise_id)
        )
    ).all()
    return [_suggested_payload(s) for s in items]


@router.post("/{suggested_id}/approve", status_code=status.HTTP_201_CREATED)
async def approve(
    exercise_id: int,
    suggested_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    s = (
        await session.exec(
            select(SuggestedInject)
            .where(SuggestedInject.id == suggested_id)
            .where(SuggestedInject.exercise_id == exercise_id)
            .with_for_update()
        )
    ).one_or_none()
    if s is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Suggested inject not found"
        )
    if s.status != SuggestedInjectStatus.pending_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending_review suggestions can be approved",
        )


    existing = (
        await session.exec(select(Inject).where(Inject.exercise_id == exercise_id))
    ).all()
    next_order = max((i.sequence_order for i in existing), default=0) + 1

    target_teams = s.target_teams
    group_id = target_teams[0] if target_teams and len(target_teams) == 1 else None
    inject = await create_inject(
        session,
        exercise_id=exercise_id,
        title=s.title,
        content=s.content,
        target_teams=target_teams,
        group_id=group_id,
        sequence_order=next_order,
        commit=False,
    )

    s.status = SuggestedInjectStatus.approved
    s.reviewed_by = current_user.id
    s.reviewed_at = datetime.now(UTC)
    session.add(s)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(inject)

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


@router.post(
    "/{suggested_id}/reject",
    status_code=status.HTTP_200_OK,
    response_model=SuggestedInjectPublic,
)
async def reject(
    exercise_id: int,
    suggested_id: int,
    current_user: FacilitatorDep,
    session: SessionDep,
):
    s = await _get_or_404(session, exercise_id, suggested_id)
    if s.status != SuggestedInjectStatus.pending_review:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending_review suggestions can be rejected",
        )
    s.status = SuggestedInjectStatus.rejected
    s.reviewed_by = current_user.id
    s.reviewed_at = datetime.now(UTC)
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return _suggested_payload(s)
