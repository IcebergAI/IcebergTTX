from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models.exercise import VALID_TRANSITIONS, Exercise, ExerciseMember, ExerciseState
from app.models.scenario import Scenario
from app.services.inject_service import seed_injects_from_scenario
from app.services.scenario_service import export_definition


def create_exercise(
    session: Session,
    *,
    scenario_id: int,
    title: str,
    created_by: int,
    llm_enabled: bool = False,
) -> Exercise:
    scenario = session.get(Scenario, scenario_id)
    if not scenario:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scenario not found")

    definition = export_definition(scenario)
    exercise = Exercise(
        scenario_id=scenario_id,
        title=title,
        created_by=created_by,
        llm_enabled=llm_enabled,
        current_node_id=definition.start_inject_id,
    )
    session.add(exercise)
    session.commit()
    session.refresh(exercise)

    seed_injects_from_scenario(session, exercise.id, scenario)
    return exercise


def transition_state(session: Session, exercise: Exercise, new_state: ExerciseState) -> Exercise:
    if new_state not in VALID_TRANSITIONS[exercise.state]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot transition from '{exercise.state}' to '{new_state}'",
        )

    now = datetime.now(UTC)
    if new_state == ExerciseState.active and exercise.started_at is None:
        exercise.started_at = now
    if new_state == ExerciseState.completed:
        exercise.ended_at = now

    exercise.state = new_state
    session.add(exercise)
    session.commit()
    session.refresh(exercise)
    return exercise


def enrol_member(session: Session, *, exercise: Exercise, user_id: int) -> ExerciseMember:
    existing = session.exec(
        select(ExerciseMember)
        .where(ExerciseMember.exercise_id == exercise.id)
        .where(ExerciseMember.user_id == user_id)
    ).first()
    if existing:
        return existing  # idempotent

    member = ExerciseMember(exercise_id=exercise.id, user_id=user_id)
    session.add(member)
    session.commit()
    session.refresh(member)
    return member


def remove_member(session: Session, *, exercise: Exercise, user_id: int) -> None:
    member = session.exec(
        select(ExerciseMember)
        .where(ExerciseMember.exercise_id == exercise.id)
        .where(ExerciseMember.user_id == user_id)
    ).first()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in exercise"
        )
    session.delete(member)
    session.commit()
