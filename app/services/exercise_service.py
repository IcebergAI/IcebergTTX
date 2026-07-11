from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import VALID_TRANSITIONS, Exercise, ExerciseMember, ExerciseState
from app.models.scenario import Scenario
from app.models.user import User
from app.services.inject_service import seed_injects_from_scenario
from app.services.scenario_service import export_definition


async def create_exercise(
    session: AsyncSession,
    *,
    scenario_id: int,
    title: str,
    created_by: int,
    llm_enabled: bool = False,
) -> Exercise:
    scenario = await session.get(Scenario, scenario_id)
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
    await session.commit()
    await session.refresh(exercise)
    assert exercise.id is not None

    await seed_injects_from_scenario(session, exercise.id, scenario)
    return exercise


async def transition_state(
    session: AsyncSession, exercise: Exercise, new_state: ExerciseState
) -> Exercise:
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
    await session.commit()
    await session.refresh(exercise)
    return exercise


async def scenario_group_ids(session: AsyncSession, exercise: Exercise) -> set[str]:
    scenario = await session.get(Scenario, exercise.scenario_id)
    if not scenario:
        return set()
    definition = export_definition(scenario)
    return {team.id for team in definition.participant_teams}


async def validate_group_id(
    session: AsyncSession, exercise: Exercise, group_id: str | None
) -> str | None:
    if group_id is None:
        return None
    normalized = group_id.strip()
    if not normalized:
        return None
    if normalized not in await scenario_group_ids(session, exercise):
        raise HTTPException(
            status_code=422,
            detail="group_id is not defined in this scenario",
        )
    return normalized


async def default_group_for_user(
    session: AsyncSession, exercise: Exercise, user: User
) -> str | None:
    if not user.team:
        return None
    return user.team if user.team in await scenario_group_ids(session, exercise) else None


async def enrol_member(
    session: AsyncSession,
    *,
    exercise: Exercise,
    user_id: int,
    group_id: str | None = None,
) -> ExerciseMember:
    assert exercise.id is not None
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    normalized_group_id = await validate_group_id(session, exercise, group_id)
    existing = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if existing:
        return existing  # idempotent

    resolved_group_id = (
        normalized_group_id
        if normalized_group_id is not None
        else await default_group_for_user(session, exercise, user)
    )
    member = ExerciseMember(
        exercise_id=exercise.id,
        user_id=user_id,
        group_id=resolved_group_id,
    )
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def update_member_group(
    session: AsyncSession,
    *,
    exercise: Exercise,
    user_id: int,
    group_id: str | None,
) -> ExerciseMember:
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in exercise"
        )
    member.group_id = await validate_group_id(session, exercise, group_id)
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


async def remove_member(session: AsyncSession, *, exercise: Exercise, user_id: int) -> None:
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == exercise.id)
            .where(ExerciseMember.user_id == user_id)
        )
    ).first()
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in exercise"
        )
    await session.delete(member)
    await session.commit()
