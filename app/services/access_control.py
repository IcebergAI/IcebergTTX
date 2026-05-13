import json

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.exercise import Exercise, ExerciseMember
from app.models.inject import Inject, InjectState
from app.models.user import User, UserRole


def is_actual_facilitator(user: User) -> bool:
    return getattr(user, "actual_role", user.role) == UserRole.facilitator


def get_exercise_or_404(session: Session, exercise_id: int) -> Exercise:
    exercise = session.get(Exercise, exercise_id)
    if not exercise:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exercise not found")
    return exercise


def is_exercise_member(session: Session, exercise_id: int, user_id: int) -> bool:
    return (
        session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise_id)
            .where(ExerciseMember.user_id == user_id)
        ).first()
        is not None
    )


def require_exercise_access(session: Session, exercise_id: int, user: User) -> Exercise:
    exercise = get_exercise_or_404(session, exercise_id)
    if user.role == UserRole.facilitator or is_actual_facilitator(user):
        return exercise
    if user.id is not None and is_exercise_member(session, exercise_id, user.id):
        return exercise
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Exercise access denied")


def require_facilitator_or_observer_access(
    session: Session, exercise_id: int, user: User
) -> Exercise:
    exercise = require_exercise_access(session, exercise_id, user)
    if user.role in (UserRole.facilitator, UserRole.observer):
        return exercise
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")


def inject_target_teams(inject: Inject) -> list[str] | None:
    return json.loads(inject.target_teams) if inject.target_teams else None


def is_inject_visible_to_user(inject: Inject, user: User) -> bool:
    if user.role == UserRole.facilitator:
        return True
    if inject.state not in (InjectState.released, InjectState.resolved):
        return False
    if user.role == UserRole.observer:
        return True
    teams = inject_target_teams(inject)
    return teams is None or user.team in teams


def require_inject_visible(inject: Inject, user: User) -> None:
    if not is_inject_visible_to_user(inject, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Inject not found")
