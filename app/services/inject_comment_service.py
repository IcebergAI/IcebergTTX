from datetime import UTC, datetime

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.inject import Inject
from app.models.inject_comment import InjectComment
from app.models.user import User
from app.services.access_control import exercise_group_for_user, inject_target_teams


async def comment_group_for_user(session: AsyncSession, inject: Inject, user: User) -> str | None:
    """Return the exercise team thread a participant comment belongs to."""
    if inject.group_id:
        return inject.group_id

    exercise_group = await exercise_group_for_user(session, inject.exercise_id, user)
    teams = inject_target_teams(inject)
    if teams:
        if exercise_group in teams:
            return exercise_group
        if user.team in teams:
            return user.team
    return exercise_group or user.team


async def create_inject_comment(
    session: AsyncSession,
    *,
    inject_id: int,
    exercise_id: int,
    user_id: int,
    group_id: str | None,
    content: str,
) -> InjectComment:
    comment = InjectComment(
        inject_id=inject_id,
        exercise_id=exercise_id,
        user_id=user_id,
        group_id=group_id,
        content=content,
    )
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return comment


async def broadcast_inject_comment_created(comment: InjectComment, payload: dict) -> None:
    from app.services.ws_manager import manager

    message = {
        "type": "inject_comment_created",
        "exercise_id": comment.exercise_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": payload,
    }
    if comment.group_id:
        await manager.broadcast_to_groups(comment.exercise_id, [comment.group_id], message)
    else:
        await manager.broadcast_to_exercise(comment.exercise_id, message)
