"""Tests for the LLM pipeline (assess_response, suggest_inject, suggested-injects CRUD).

The active AI provider is mocked at the ``active_provider`` seam — no provider SDK
is constructed and no real network requests are made. Per-provider adapter/config
behaviour is covered in test_llm_providers.py.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise
from app.models.user import User


def test_provider_json_boundary_rejects_non_objects_and_invalid_fields():
    from app.services.llm_service import AssessmentOutput, SuggestedInjectOutput, _parse_json

    fallback = {"assessment_text": "fallback"}
    assert _parse_json("[]", fallback) == fallback
    assert _parse_json("null", fallback) == fallback
    assert _parse_json("not json", fallback) == fallback
    with pytest.raises(ValueError):
        AssessmentOutput.model_validate({"assessment_text": "x", "decision_quality": "invalid"})
    with pytest.raises(ValueError):
        SuggestedInjectOutput.model_validate(
            {"title": "x", "content": "y", "unexpected": "field"}
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _first_released_inject_id(client: AsyncClient, token: str, exercise_id: int) -> int:
    injects = (await client.get(
        f"/api/exercises/{exercise_id}/injects",
        headers={"Authorization": f"Bearer {token}"},
    )).json()
    pending = next(i for i in injects if i["state"] == "pending")
    released = (await client.post(
        f"/api/exercises/{exercise_id}/injects/{pending['id']}/release",
        headers={"Authorization": f"Bearer {token}"},
    )).json()
    return released["id"]


async def _submit(client, token, exercise_id, inject_id, content="We isolated the systems."):
    return await client.post(
        f"/api/exercises/{exercise_id}/responses",
        json={"inject_id": inject_id, "content": content, "selected_option": "opt_a"},
        headers={"Authorization": f"Bearer {token}"},
    )


def _fake_provider(*texts: str, label: str = "test:model"):
    """A stand-in LLMProvider whose ``complete`` returns the given text(s).

    One text → every call returns it; multiple → consumed in order (side_effect),
    for the assess-then-suggest sequence in the pipeline test.
    """
    provider = MagicMock()
    provider.llm_model_label = label
    if len(texts) == 1:
        provider.complete = AsyncMock(return_value=texts[0])
    else:
        provider.complete = AsyncMock(side_effect=list(texts))
    return provider


def _assessment_json():
    return json.dumps({
        "assessment_text": "Good decision — isolating quickly limits blast radius.",
        "decision_quality": "good",
        "recommended_branch_option_id": "opt_a",
    })


def _suggestion_json():
    return json.dumps({
        "title": "Ransomware Note Found",
        "content": "A ransom demand has appeared on affected systems. Respond.",
        "target_teams": ["it_ops"],
    })


# ── llm_service unit tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_assess_response_stores_assessment(
    session: AsyncSession, facilitator: User, sample_scenario, active_exercise: Exercise
):
    from app.models.inject import Inject, InjectState
    from app.models.response import Response
    from app.models.scenario import Scenario
    from app.services.llm_service import assess_response
    from app.services.scenario_service import export_definition

    inject = Inject(
        exercise_id=active_exercise.id,
        scenario_node_id="inject_01",
        title="Test",
        content="What do you do?",
        sequence_order=1,
        state=InjectState.released,
    )
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    response = Response(
        inject_id=inject.id,
        exercise_id=active_exercise.id,
        user_id=facilitator.id,
        content="We isolated immediately.",
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)

    scenario = (await session.get(Scenario, active_exercise.scenario_id))
    definition = export_definition(scenario)

    with patch(
        "app.services.llm_service.active_provider",
        return_value=_fake_provider(_assessment_json()),
    ):
        assessment = await assess_response(session, response, inject, definition)

    assert assessment.id is not None
    assert assessment.response_id == response.id
    assert assessment.decision_quality == "good"
    assert assessment.assessment_text == "Good decision — isolating quickly limits blast radius."
    assert assessment.recommended_branch_option_id == "opt_a"

    await session.refresh(response)
    assert response.assessment_id == assessment.id


@pytest.mark.asyncio
async def test_suggest_inject_stores_suggestion(
    session: AsyncSession, facilitator: User, sample_scenario, active_exercise: Exercise
):
    from app.models.inject import Inject, InjectState
    from app.models.response import Response
    from app.models.scenario import Scenario
    from app.services.llm_service import suggest_inject
    from app.services.scenario_service import export_definition

    inject = Inject(
        exercise_id=active_exercise.id,
        scenario_node_id="inject_01",
        title="Test",
        content="What do you do?",
        sequence_order=1,
        state=InjectState.released,
    )
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    response = Response(
        inject_id=inject.id,
        exercise_id=active_exercise.id,
        user_id=facilitator.id,
        content="We isolated immediately.",
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)

    scenario = (await session.get(Scenario, active_exercise.scenario_id))
    definition = export_definition(scenario)

    with patch(
        "app.services.llm_service.active_provider",
        return_value=_fake_provider(_suggestion_json()),
    ):
        suggested = await suggest_inject(session, response, inject, active_exercise, definition)

    assert suggested.id is not None
    assert suggested.exercise_id == active_exercise.id
    assert suggested.title == "Ransomware Note Found"
    assert suggested.target_teams == ["it_ops"]


@pytest.mark.asyncio
async def test_assess_response_handles_invalid_json(
    session: AsyncSession, facilitator: User, sample_scenario, active_exercise: Exercise
):
    """When the LLM returns non-JSON, assessment_text is the raw text."""
    from app.models.inject import Inject, InjectState
    from app.models.response import Response
    from app.models.scenario import Scenario
    from app.services.llm_service import assess_response
    from app.services.scenario_service import export_definition

    inject = Inject(
        exercise_id=active_exercise.id,
        scenario_node_id="inject_01",
        title="Test",
        content="What do you do?",
        sequence_order=1,
        state=InjectState.released,
    )
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    response = Response(
        inject_id=inject.id,
        exercise_id=active_exercise.id,
        user_id=facilitator.id,
        content="Some free text.",
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)

    scenario = (await session.get(Scenario, active_exercise.scenario_id))
    definition = export_definition(scenario)

    with patch(
        "app.services.llm_service.active_provider",
        return_value=_fake_provider("Plain text, not JSON."),
    ):
        assessment = await assess_response(session, response, inject, definition)

    assert assessment.assessment_text == "Plain text, not JSON."
    assert assessment.decision_quality is None


# ── REST endpoint tests ───────────────────────────────────────────────────────

async def test_trigger_assess_endpoint(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()

    with patch("app.routers.responses.run_llm_pipeline", new_callable=AsyncMock):
        r = await client.post(
            f"/api/exercises/{active_exercise.id}/responses/{resp['id']}/assess",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    assert r.status_code == 202


async def test_trigger_assess_participant_forbidden(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()

    r = await client.post(
        f"/api/exercises/{active_exercise.id}/responses/{resp['id']}/assess",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


async def test_get_assessment_not_found_when_none(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/responses/{resp['id']}/assessment",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


async def test_get_assessment_returns_data(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):

    from app.models.assessment import ResponseAssessment
    from app.models.response import Response

    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp_data = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()

    # Manually create an assessment record
    response = (await session.get(Response, resp_data["id"]))
    assessment = ResponseAssessment(
        response_id=response.id,
        llm_model="claude-sonnet-4-6",
        assessment_text="Solid response.",
        decision_quality="good",
    )
    session.add(assessment)
    await session.commit()
    await session.refresh(assessment)
    response.assessment_id = assessment.id
    session.add(response)
    await session.commit()

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/responses/{resp_data['id']}/assessment",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["decision_quality"] == "good"
    assert data["assessment_text"] == "Solid response."


# ── Suggested injects CRUD ────────────────────────────────────────────────────

async def _make_suggested(
    session: AsyncSession, exercise_id: int, response_id: int, title="Follow-up"
):
    from app.models.suggested_inject import SuggestedInject

    s = SuggestedInject(
        exercise_id=exercise_id,
        triggered_by_response_id=response_id,
        title=title,
        content="What is your next action?",
        llm_model="claude-sonnet-4-6",
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


async def test_list_suggested_injects(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()
    (await _make_suggested(session, active_exercise.id, resp["id"], "First suggestion"))
    (await _make_suggested(session, active_exercise.id, resp["id"], "Second suggestion"))

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/suggested-injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    titles = [s["title"] for s in r.json()]
    assert "First suggestion" in titles
    assert "Second suggestion" in titles


async def test_list_suggested_participant_forbidden(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/suggested-injects",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


async def test_approve_suggested_inject(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()
    suggested = (await _make_suggested(session, active_exercise.id, resp["id"]))

    r = await client.post(
        f"/api/exercises/{active_exercise.id}/suggested-injects/{suggested.id}/approve",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Follow-up"
    assert data["state"] == "pending"

    # Suggestion status updated
    from app.models.suggested_inject import SuggestedInjectStatus
    await session.refresh(suggested)
    assert suggested.status == SuggestedInjectStatus.approved


async def test_approve_already_approved_returns_409(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    from app.models.suggested_inject import SuggestedInjectStatus

    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()
    suggested = (await _make_suggested(session, active_exercise.id, resp["id"]))
    suggested.status = SuggestedInjectStatus.approved
    session.add(suggested)
    await session.commit()

    r = await client.post(
        f"/api/exercises/{active_exercise.id}/suggested-injects/{suggested.id}/approve",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


async def test_reject_suggested_inject(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()
    suggested = (await _make_suggested(session, active_exercise.id, resp["id"]))

    r = await client.post(
        f"/api/exercises/{active_exercise.id}/suggested-injects/{suggested.id}/reject",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"


@pytest.mark.asyncio
async def test_run_llm_pipeline_broadcasts_to_facilitator(
    session: AsyncSession, facilitator: User, sample_scenario, active_exercise: Exercise
):
    """run_llm_pipeline calls send_to_facilitators with assessment_ready and inject_suggested."""
    from unittest.mock import patch as _patch

    from app.models.inject import Inject, InjectState
    from app.models.response import Response
    from app.services.llm_service import run_llm_pipeline

    inject = Inject(
        exercise_id=active_exercise.id,
        scenario_node_id="inject_01",
        title="Test",
        content="What do you do?",
        sequence_order=1,
        state=InjectState.released,
    )
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    response = Response(
        inject_id=inject.id,
        exercise_id=active_exercise.id,
        user_id=facilitator.id,
        content="We isolated the affected hosts.",
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)

    broadcast_calls = []

    async def _fake_send(exercise_id, message):
        broadcast_calls.append(message)

    # run_llm_pipeline opens its own AsyncSession(engine). Patch the engine it
    # imports with this test's AsyncConnection so its session shares the same
    # open transaction and can see the uncommitted rows above.
    test_engine = await session.connection()

    with (
        _patch(
            "app.services.llm_service.active_provider",
            return_value=_fake_provider(_assessment_json(), _suggestion_json()),
        ),
        _patch("app.services.llm_service.engine", test_engine),
        _patch("app.services.ws_manager.manager") as mock_manager,
    ):
        mock_manager.send_to_facilitators = AsyncMock(side_effect=_fake_send)
        await run_llm_pipeline(response.id, inject.id, active_exercise.id)

    types = [c["type"] for c in broadcast_calls]
    assert "assessment_ready" in types
    assert "inject_suggested" in types
