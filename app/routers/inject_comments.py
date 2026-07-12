from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.exercise import ExerciseState
from app.models.inject import InjectState
from app.models.inject_comment import InjectComment
from app.models.user import User, UserRole
from app.schemas.api import InjectCommentPublic
from app.services.access_control import (
    exercise_group_for_user,
    require_exercise_access,
    require_inject_visible,
    require_operational_mutability,
)
from app.services.inject_comment_service import (
    broadcast_inject_comment_created,
    comment_group_for_user,
    create_inject_comment,
)
from app.services.inject_service import get_inject_or_404

router = APIRouter(prefix="/exercises/{exercise_id}/inject-comments", tags=["inject-comments"])

CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class CreateInjectCommentRequest(BaseModel):
    inject_id: int
    content: str


async def _author_name(session: AsyncSession, user_id: int) -> str:
    user = await session.get(User, user_id)
    return user.display_name if user else f"User #{user_id}"


async def _comment_out(session: AsyncSession, comment: InjectComment) -> dict:
    return {
        "id": comment.id,
        "inject_id": comment.inject_id,
        "exercise_id": comment.exercise_id,
        "user_id": comment.user_id,
        "author_name": await _author_name(session, comment.user_id),
        "group_id": comment.group_id,
        "content": comment.content,
        "created_at": comment.created_at.isoformat(),
    }


async def _can_see_comment(session: AsyncSession, comment: InjectComment, user: User) -> bool:
    inject = await get_inject_or_404(session, comment.exercise_id, comment.inject_id)
    try:
        await require_inject_visible(session, inject, user)
    except HTTPException:
        return False

    if user.role in (UserRole.facilitator, UserRole.observer):
        return True

    group_id = await exercise_group_for_user(session, comment.exercise_id, user) or user.team
    return comment.group_id == group_id


@router.get("", response_model=list[InjectCommentPublic])
async def list_inject_comments(
    exercise_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    await require_exercise_access(session, exercise_id, current_user)
    comments = (
        await session.exec(
            select(InjectComment)
            .where(InjectComment.exercise_id == exercise_id)
            .order_by(col(InjectComment.created_at))
        )
    ).all()
    return [
        await _comment_out(session, comment)
        for comment in comments
        if await _can_see_comment(session, comment, current_user)
    ]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InjectCommentPublic)
async def create_comment(
    exercise_id: int,
    body: CreateInjectCommentRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    assert current_user.id is not None
    exercise = await require_exercise_access(session, exercise_id, current_user)
    require_operational_mutability(exercise)
    if current_user.role != UserRole.participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only participants can comment on injects",
        )
    if exercise.state != ExerciseState.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Comments can only be added while the exercise is active",
        )

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="content is required")

    inject = await get_inject_or_404(session, exercise_id, body.inject_id)
    assert inject.id is not None
    await require_inject_visible(session, inject, current_user)
    if inject.state not in (InjectState.released, InjectState.resolved):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Comments can only be added to released injects",
        )

    comment = await create_inject_comment(
        session,
        inject_id=inject.id,
        exercise_id=exercise_id,
        user_id=current_user.id,
        group_id=await comment_group_for_user(session, inject, current_user),
        content=content,
    )
    payload = await _comment_out(session, comment)
    await broadcast_inject_comment_created(comment, payload)
    return payload
