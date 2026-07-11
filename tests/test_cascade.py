"""Foreign-key enforcement and cascade-delete behaviour.

Covers the relationship/cascade refactor: deleting a parent row removes (or
nulls) its dependents, and deleting a scenario that is still referenced by an
exercise is blocked at the route layer rather than cascading. Postgres enforces
the foreign keys natively.
"""

import pytest_asyncio
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.assessment import ResponseAssessment
from app.models.communication import CommDirection, Communication
from app.models.exercise import (
    Exercise,
    ExerciseMember,
    ExerciseState,
    ExerciseStateTransition,
)
from app.models.inject import Inject
from app.models.inject_comment import InjectComment
from app.models.response import Response
from app.models.scenario import Scenario
from app.models.suggested_inject import SuggestedInject
from app.models.user import User


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _count(session: AsyncSession, model) -> int:
    return len((await session.exec(select(model))).all())


@pytest_asyncio.fixture(name="bare_exercise")
async def bare_exercise_fixture(
    session: AsyncSession, facilitator: User, sample_scenario: Scenario
) -> Exercise:
    """An exercise built directly, with no scenario-seeded injects."""
    ex = Exercise(
        scenario_id=sample_scenario.id, title="Cascade Exercise", created_by=facilitator.id
    )
    session.add(ex)
    await session.commit()
    await session.refresh(ex)
    return ex


async def _build_inject(session: AsyncSession, exercise: Exercise, user: User) -> Inject:
    inject = Inject(exercise_id=exercise.id, title="Alert", content="Respond.")
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    response = Response(
        inject_id=inject.id,
        exercise_id=exercise.id,
        user_id=user.id,
        content="We isolate the host.",
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)

    session.add(
        ResponseAssessment(
            response_id=response.id,
            llm_model="claude-test",
            assessment_text="Good call.",
        )
    )
    session.add(
        SuggestedInject(
            exercise_id=exercise.id,
            triggered_by_response_id=response.id,
            title="Follow-up",
            content="Escalate.",
            llm_model="claude-test",
        )
    )
    session.add(
        InjectComment(
            inject_id=inject.id,
            exercise_id=exercise.id,
            user_id=user.id,
            content="On it.",
        )
    )
    session.add(
        Communication(
            exercise_id=exercise.id,
            direction=CommDirection.outbound,
            subject="ICO notification",
            body="Notifying the regulator.",
            triggered_by_inject_id=inject.id,
        )
    )
    session.add(
        ExerciseMember(
            exercise_id=exercise.id,
            user_id=user.id,
            role_at_enrolment=user.role,
        )
    )
    await session.commit()
    return inject


async def test_delete_exercise_cascades_children(
    session: AsyncSession, bare_exercise: Exercise, participant: User
):
    await _build_inject(session, bare_exercise, participant)
    from app.services.exercise_service import transition_state

    await transition_state(
        session,
        bare_exercise,
        ExerciseState.active,
        actor_id=participant.id,
    )
    assert await _count(session, Inject) == 1
    assert await _count(session, Response) == 1
    assert await _count(session, ResponseAssessment) == 1
    assert await _count(session, SuggestedInject) == 1
    assert await _count(session, InjectComment) == 1
    assert await _count(session, Communication) == 1
    assert await _count(session, ExerciseMember) == 1
    assert await _count(session, ExerciseStateTransition) == 1

    await session.delete(bare_exercise)
    await session.commit()

    assert await _count(session, Inject) == 0
    assert await _count(session, Response) == 0
    assert await _count(session, ResponseAssessment) == 0
    assert await _count(session, SuggestedInject) == 0
    assert await _count(session, InjectComment) == 0
    assert await _count(session, Communication) == 0
    assert await _count(session, ExerciseMember) == 0
    assert await _count(session, ExerciseStateTransition) == 0


async def test_delete_inject_cascades_responses_and_nulls_communication(
    session: AsyncSession, bare_exercise: Exercise, participant: User
):
    inject = await _build_inject(session, bare_exercise, participant)

    await session.delete(inject)
    await session.commit()

    # Responses, their assessments, dependent suggested injects, and comments go.
    assert await _count(session, Inject) == 0
    assert await _count(session, Response) == 0
    assert await _count(session, ResponseAssessment) == 0
    assert await _count(session, SuggestedInject) == 0
    assert await _count(session, InjectComment) == 0
    # The communication record survives with its inject back-reference nulled.
    comm = (await session.exec(select(Communication))).one()
    assert comm.triggered_by_inject_id is None


async def test_delete_response_cascades_assessment_and_suggested(
    session: AsyncSession, bare_exercise: Exercise, participant: User
):
    await _build_inject(session, bare_exercise, participant)
    response = (await session.exec(select(Response))).one()

    await session.delete(response)
    await session.commit()

    assert await _count(session, Response) == 0
    assert await _count(session, ResponseAssessment) == 0
    assert await _count(session, SuggestedInject) == 0
    # The parent inject is untouched.
    assert await _count(session, Inject) == 1


async def test_delete_scenario_in_use_is_blocked(
    client: AsyncClient, facilitator_token: str, draft_exercise: Exercise
):
    scenario_id = draft_exercise.scenario_id
    resp = await client.delete(
        f"/api/scenarios/{scenario_id}", headers=_headers(facilitator_token)
    )
    assert resp.status_code == 409
