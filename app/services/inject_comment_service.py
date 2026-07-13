from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.inject import Inject
from app.models.inject_comment import InjectComment
from app.models.user import User
from app.services.access_control import exercise_group_for_user, inject_target_teams
from app.services.domain_events import InjectCommentCreated, dispatch, record


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


async def comment_payload(session: AsyncSession, comment: InjectComment) -> dict:
    """Canonical comment serialization, shared by the HTTP reply and the WS frame.

    Lives here rather than in the router because the event that carries it is recorded
    *inside* the transaction, so the payload has to be built before the commit.
    """
    author = await session.get(User, comment.user_id)
    return {
        "id": comment.id,
        "inject_id": comment.inject_id,
        "exercise_id": comment.exercise_id,
        "user_id": comment.user_id,
        "author_name": author.display_name if author else f"User #{comment.user_id}",
        "group_id": comment.group_id,
        "content": comment.content,
        "created_at": comment.created_at.isoformat(),
    }


async def create_inject_comment(
    session: AsyncSession,
    *,
    inject_id: int,
    exercise_id: int,
    user_id: int,
    group_id: str | None,
    content: str,
) -> tuple[InjectComment, dict]:
    """Create a comment and return it with its payload, which the caller also broadcasts."""
    comment = InjectComment(
        inject_id=inject_id,
        exercise_id=exercise_id,
        user_id=user_id,
        group_id=group_id,
        content=content,
    )
    session.add(comment)
    await session.flush()  # id + created_at, so the payload is complete pre-commit
    payload = await comment_payload(session, comment)
    record(
        session,
        InjectCommentCreated(exercise_id=exercise_id, comment=comment, payload=payload),
    )
    await session.commit()
    await session.refresh(comment)
    await dispatch(session)
    return comment, payload


