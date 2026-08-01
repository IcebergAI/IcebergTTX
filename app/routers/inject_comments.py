from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import get_current_user
from app.models.exercise import ExerciseState
from app.models.inject import Inject, InjectState
from app.models.inject_comment import InjectComment
from app.models.user import User, UserRole
from app.schemas.api import InjectCommentPublic
from app.services.access_control import (
    exercise_group_for_user,
    inject_visible_to,
    require_exercise_access,
    require_inject_visible,
    require_operational_mutability,
)
from app.services.inject_comment_service import (
    comment_group_for_user,
    comment_payloads,
    create_inject_comment,
)
from app.services.inject_service import get_inject_or_404

router = APIRouter(prefix="/exercises/{exercise_id}/inject-comments", tags=["inject-comments"])

CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class CreateInjectCommentRequest(BaseModel):
    inject_id: int
    content: str




def _can_see_comment(
    comment: InjectComment,
    user: User,
    injects: dict[int, Inject],
    group_id: str | None,
) -> bool:
    """Visibility against a pre-resolved inject map and exercise group (#263).

    Both used to be re-fetched per comment — an inject lookup plus an ExerciseMember
    query for every row in the thread.
    """
    inject = injects.get(comment.inject_id)
    if inject is None or not inject_visible_to(inject, user, group_id):
        return False
    if user.role in (UserRole.facilitator, UserRole.observer):
        return True
    return comment.group_id == (group_id or user.team)


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
    injects = {
        inject.id: inject
        for inject in (
            await session.exec(select(Inject).where(Inject.exercise_id == exercise_id))
        ).all()
        if inject.id is not None
    }
    group_id = await exercise_group_for_user(session, exercise_id, current_user)
    visible = [c for c in comments if _can_see_comment(c, current_user, injects, group_id)]
    return await comment_payloads(session, visible)


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

    _comment, payload = await create_inject_comment(
        session,
        inject_id=inject.id,
        exercise_id=exercise_id,
        user_id=current_user.id,
        group_id=await comment_group_for_user(session, inject, current_user),
        content=content,
    )
    return payload
