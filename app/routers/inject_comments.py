from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.database import get_session
from app.dependencies import get_current_user
from app.models.exercise import ExerciseState
from app.models.inject import InjectState
from app.models.inject_comment import InjectComment
from app.models.user import User, UserRole
from app.services.access_control import (
    exercise_group_for_user,
    require_exercise_access,
    require_inject_visible,
)
from app.services.inject_comment_service import (
    broadcast_inject_comment_created,
    comment_group_for_user,
    create_inject_comment,
)
from app.services.inject_service import get_inject_or_404

router = APIRouter(prefix="/exercises/{exercise_id}/inject-comments", tags=["inject-comments"])

CurrentUserDep = Annotated[User, Depends(get_current_user)]
SessionDep = Annotated[Session, Depends(get_session)]


class CreateInjectCommentRequest(BaseModel):
    inject_id: int
    content: str


def _author_name(session: Session, user_id: int) -> str:
    user = session.get(User, user_id)
    return user.display_name if user else f"User #{user_id}"


def _comment_out(session: Session, comment: InjectComment) -> dict:
    return {
        "id": comment.id,
        "inject_id": comment.inject_id,
        "exercise_id": comment.exercise_id,
        "user_id": comment.user_id,
        "author_name": _author_name(session, comment.user_id),
        "group_id": comment.group_id,
        "content": comment.content,
        "created_at": comment.created_at.isoformat(),
    }


def _can_see_comment(session: Session, comment: InjectComment, user: User) -> bool:
    inject = get_inject_or_404(session, comment.exercise_id, comment.inject_id)
    try:
        require_inject_visible(session, inject, user)
    except HTTPException:
        return False

    if user.role in (UserRole.facilitator, UserRole.observer):
        return True

    group_id = exercise_group_for_user(session, comment.exercise_id, user) or user.team
    return comment.group_id == group_id


@router.get("")
def list_inject_comments(
    exercise_id: int,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    require_exercise_access(session, exercise_id, current_user)
    comments = session.exec(
        select(InjectComment)
        .where(InjectComment.exercise_id == exercise_id)
        .order_by(InjectComment.created_at)
    ).all()
    return [
        _comment_out(session, comment)
        for comment in comments
        if _can_see_comment(session, comment, current_user)
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_comment(
    exercise_id: int,
    body: CreateInjectCommentRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
):
    assert current_user.id is not None
    exercise = require_exercise_access(session, exercise_id, current_user)
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

    inject = get_inject_or_404(session, exercise_id, body.inject_id)
    require_inject_visible(session, inject, current_user)
    if inject.state not in (InjectState.released, InjectState.resolved):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Comments can only be added to released injects",
        )

    comment = create_inject_comment(
        session,
        inject_id=inject.id,
        exercise_id=exercise_id,
        user_id=current_user.id,
        group_id=comment_group_for_user(session, inject, current_user),
        content=content,
    )
    payload = _comment_out(session, comment)
    await broadcast_inject_comment_created(comment, payload)
    return payload
