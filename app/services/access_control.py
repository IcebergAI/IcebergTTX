import json

from fastapi import HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseMember
from app.models.inject import Inject, InjectState
from app.models.user import User, UserRole
from app.services import audit_service


def is_actual_facilitator(user: User) -> bool:
    return getattr(user, "actual_role", user.role) == UserRole.facilitator


async def get_exercise_or_404(session: AsyncSession, exercise_id: int) -> Exercise:
    exercise = await session.get(Exercise, exercise_id)
    if not exercise:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
    return exercise


async def is_exercise_member(session: AsyncSession, exercise_id: int, user_id: int) -> bool:
    return (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise_id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first() is not None


async def exercise_member_for_user(
    session: AsyncSession, exercise_id: int, user_id: int | None
) -> ExerciseMember | None:
    if user_id is None:
        return None
    return (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise_id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()


async def require_exercise_access(session: AsyncSession, exercise_id: int, user: User) -> Exercise:
    exercise = await get_exercise_or_404(session, exercise_id)
    if user.role == UserRole.facilitator or is_actual_facilitator(user):
        return exercise
    if user.id is not None and await is_exercise_member(session, exercise_id, user.id):
        return exercise
    audit_service.emit(
        "authz.denied",
        result="deny",
        actor=user,
        target_type="exercise",
        target_id=exercise_id,
        reason="not an exercise member",
        severity="warning",
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Exercise access denied")


def inject_target_teams(inject: Inject) -> list[str] | None:
    return json.loads(inject.target_teams) if inject.target_teams else None


async def exercise_group_for_user(
    session: AsyncSession, exercise_id: int, user: User
) -> str | None:
    member = await exercise_member_for_user(session, exercise_id, user.id)
    if member:
        return member.group_id
    if is_actual_facilitator(user) and user.role == UserRole.participant:
        return user.team
    return None


async def is_inject_visible_to_user(session: AsyncSession, inject: Inject, user: User) -> bool:
    if user.role == UserRole.facilitator:
        return True
    if inject.state not in (InjectState.released, InjectState.resolved):
        return False
    if user.role == UserRole.observer:
        return True
    group_id = await exercise_group_for_user(session, inject.exercise_id, user)
    if inject.group_id is not None:
        return group_id == inject.group_id
    teams = inject_target_teams(inject)
    if teams:
        return group_id in teams or user.team in teams
    return True


async def require_inject_visible(session: AsyncSession, inject: Inject, user: User) -> None:
    if not await is_inject_visible_to_user(session, inject, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inject not found")
