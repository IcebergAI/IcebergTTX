"""Tests for the LLM pipeline (assess_response, suggest_inject, suggested-injects CRUD).

The active AI provider is mocked at the ``active_provider`` seam — no provider SDK
is constructed and no real network requests are made. Per-provider adapter/config
behaviour is covered in test_llm_providers.py.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
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
    from app.services.llm_service import _assess_response_result
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

    provider = _fake_provider(_assessment_json())
    with patch("app.services.llm_service.active_provider", return_value=provider):
        assessment, created = await _assess_response_result(
            session, response, inject, definition
        )
        replayed, replay_created = await _assess_response_result(
            session, response, inject, definition
        )

    assert created is True
    assert replay_created is False
    assert replayed.id == assessment.id
    provider.complete.assert_awaited_once()
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
    from app.services.llm_service import _suggest_inject_result
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

    provider = _fake_provider(_suggestion_json())
    with patch("app.services.llm_service.active_provider", return_value=provider):
        suggested, created = await _suggest_inject_result(
            session, response, inject, active_exercise, definition
        )
        replayed, replay_created = await _suggest_inject_result(
            session, response, inject, active_exercise, definition
        )

    assert created is True
    assert replay_created is False
    assert replayed.id == suggested.id
    provider.complete.assert_awaited_once()
    assert suggested.id is not None
    assert suggested.exercise_id == active_exercise.id
    assert suggested.title == "Ransomware Note Found"
    assert suggested.target_teams == ["it_ops"]


@pytest.mark.asyncio
async def test_suggest_inject_rejects_unknown_provider_target_teams(
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
    scenario = await session.get(Scenario, active_exercise.scenario_id)
    definition = export_definition(scenario)

    with patch(
        "app.services.llm_service.active_provider",
        return_value=_fake_provider(
            '{"title":"Unsafe","content":"x","target_teams":["unknown"]}'
        ),
    ), pytest.raises(ValueError, match="not in this scenario"):
        await suggest_inject(session, response, inject, active_exercise, definition)


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
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    inject_id = await _first_released_inject_id(client, facilitator_token, active_exercise.id)
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()
    active_exercise.llm_enabled = True
    session.add(active_exercise)
    await session.commit()

    with patch("app.routers.responses.queue_llm_pipeline", return_value=True) as queue:
        r = await client.post(
            f"/api/exercises/{active_exercise.id}/responses/{resp['id']}/assess",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    assert r.status_code == 202
    assert r.json() == {"detail": "Assessment queued"}
    queue.assert_called_once_with(resp["id"], inject_id, active_exercise.id)


async def test_trigger_assess_rejects_exercise_ai_opt_out(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    inject_id = await _first_released_inject_id(client, facilitator_token, active_exercise.id)
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()

    with patch("app.routers.responses.queue_llm_pipeline") as queue:
        denied = await client.post(
            f"/api/exercises/{active_exercise.id}/responses/{resp['id']}/assess",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    assert denied.status_code == 409
    assert denied.json() == {"detail": "AI assessment is disabled for this exercise"}
    queue.assert_not_called()


async def test_trigger_assess_is_idempotent_while_task_is_inflight(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    inject_id = await _first_released_inject_id(client, facilitator_token, active_exercise.id)
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()
    active_exercise.llm_enabled = True
    session.add(active_exercise)
    await session.commit()

    with patch("app.routers.responses.queue_llm_pipeline", side_effect=[True, False]) as queue:
        first = await client.post(
            f"/api/exercises/{active_exercise.id}/responses/{resp['id']}/assess",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        second = await client.post(
            f"/api/exercises/{active_exercise.id}/responses/{resp['id']}/assess",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    assert first.status_code == second.status_code == 202
    assert first.json() == {"detail": "Assessment queued"}
    assert second.json() == {"detail": "Assessment already queued"}
    assert queue.call_count == 2


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

    active_exercise.llm_enabled = True
    session.add(active_exercise)
    await session.commit()
    with patch("app.routers.responses.queue_llm_pipeline") as queue:
        duplicate = await client.post(
            f"/api/exercises/{active_exercise.id}/responses/{resp_data['id']}/assess",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    assert duplicate.status_code == 202
    assert duplicate.json() == {"detail": "Assessment already exists"}
    queue.assert_not_called()


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
    from app.models.response import Response

    second_response = Response(
        inject_id=inject_id,
        exercise_id=active_exercise.id,
        user_id=active_exercise.created_by,
        content="Facilitator replay fixture",
    )
    session.add(second_response)
    await session.commit()
    await session.refresh(second_response)
    assert second_response.id is not None
    (await _make_suggested(session, active_exercise.id, resp["id"], "First suggestion"))
    (await _make_suggested(
        session,
        active_exercise.id,
        second_response.id,
        "Second suggestion",
    ))

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


async def test_other_facilitator_denied_assessment_and_suggestion_routes(
    client: AsyncClient,
    facilitator_token: str,
    second_facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    from app.models.suggested_inject import SuggestedInjectStatus

    inject_id = await _first_released_inject_id(client, facilitator_token, active_exercise.id)
    response = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()
    suggested = await _make_suggested(
        session, active_exercise.id, response["id"], "Protected suggestion"
    )
    active_exercise.llm_enabled = True
    session.add(active_exercise)
    await session.commit()
    attacker_headers = {"Authorization": f"Bearer {second_facilitator_token}"}

    with patch("app.routers.responses.queue_llm_pipeline") as queue:
        assess = await client.post(
            f"/api/exercises/{active_exercise.id}/responses/{response['id']}/assess",
            headers=attacker_headers,
        )
    assessment = await client.get(
        f"/api/exercises/{active_exercise.id}/responses/{response['id']}/assessment",
        headers=attacker_headers,
    )
    listed = await client.get(
        f"/api/exercises/{active_exercise.id}/suggested-injects",
        headers=attacker_headers,
    )
    approved = await client.post(
        f"/api/exercises/{active_exercise.id}/suggested-injects/{suggested.id}/approve",
        headers=attacker_headers,
    )
    rejected = await client.post(
        f"/api/exercises/{active_exercise.id}/suggested-injects/{suggested.id}/reject",
        headers=attacker_headers,
    )

    assert {assess.status_code, assessment.status_code, listed.status_code} == {403}
    assert approved.status_code == rejected.status_code == 403
    queue.assert_not_called()
    await session.refresh(suggested)
    assert suggested.status == SuggestedInjectStatus.pending_review


def test_queue_llm_pipeline_deduplicates_inflight_response():
    from app.services import llm_service

    llm_service._assessment_inflight.clear()
    task = MagicMock()
    with patch("app.services.llm_service.spawn", return_value=task) as spawn:
        assert llm_service.queue_llm_pipeline(101, 202, 303) is True
        assert llm_service.queue_llm_pipeline(101, 202, 303) is False

    spawn.assert_called_once()
    coroutine = spawn.call_args.args[0]
    coroutine.close()
    done_callback = task.add_done_callback.call_args.args[0]
    done_callback(task)
    assert 101 not in llm_service._assessment_inflight


@pytest.mark.asyncio
async def test_run_llm_pipeline_broadcasts_to_facilitator(
    session: AsyncSession, facilitator: User, sample_scenario, active_exercise: Exercise
):
    """run_llm_pipeline calls send_to_facilitators with assessment_ready and inject_suggested."""
    from unittest.mock import patch as _patch

    from app.models.inject import Inject, InjectState
    from app.models.response import Response
    from app.services.llm_service import run_llm_pipeline

    active_exercise.llm_enabled = True
    session.add(active_exercise)
    await session.commit()

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
        # Patch the projector's `manager`, not ws_manager's module attribute (#212).
        # llm_service used to import `manager` lazily *inside* the function, so patching
        # the module attribute was re-resolved at call time. The frames now terminate in
        # ws_projector, which binds `manager` at import — so that is the reference the
        # patch has to replace, or the mock is simply never consulted.
        _patch("app.services.ws_projector.manager") as mock_manager,
    ):
        mock_manager.send_to_facilitators = AsyncMock(side_effect=_fake_send)
        await run_llm_pipeline(response.id, inject.id, active_exercise.id)

    types = [c["type"] for c in broadcast_calls]
    assert "assessment_ready" in types
    assert "inject_suggested" in types


@pytest.mark.asyncio
async def test_run_llm_pipeline_respects_exercise_ai_opt_out(
    session: AsyncSession, facilitator: User, active_exercise: Exercise
):
    from unittest.mock import patch as _patch

    from app.models.inject import Inject
    from app.models.response import Response
    from app.services.llm_service import run_llm_pipeline

    inject = (
        await session.exec(select(Inject).where(Inject.exercise_id == active_exercise.id))
    ).first()
    response = Response(
        inject_id=inject.id,
        exercise_id=active_exercise.id,
        user_id=facilitator.id,
        content="This must not leave the deployment.",
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)
    provider = _fake_provider(_assessment_json())
    test_engine = await session.connection()

    with (
        _patch("app.services.llm_service.active_provider", return_value=provider),
        _patch("app.services.llm_service.engine", test_engine),
        _patch("app.services.ws_manager.manager") as manager,
    ):
        await run_llm_pipeline(response.id, inject.id, active_exercise.id)

    provider.complete.assert_not_awaited()
    manager.send_to_facilitators.assert_not_called()


@pytest.mark.asyncio
async def test_run_llm_pipeline_rejects_mismatched_response_inject_relationship(
    session: AsyncSession, facilitator: User, active_exercise: Exercise
):
    from unittest.mock import patch as _patch

    from app.models.inject import Inject, InjectState
    from app.models.response import Response
    from app.services.llm_service import run_llm_pipeline

    active_exercise.llm_enabled = True
    session.add(active_exercise)
    first = Inject(
        exercise_id=active_exercise.id,
        title="First",
        content="First",
        sequence_order=90,
        state=InjectState.released,
    )
    second = Inject(
        exercise_id=active_exercise.id,
        title="Second",
        content="Second",
        sequence_order=91,
        state=InjectState.released,
    )
    session.add(first)
    session.add(second)
    await session.commit()
    await session.refresh(first)
    await session.refresh(second)
    response = Response(
        inject_id=first.id,
        exercise_id=active_exercise.id,
        user_id=facilitator.id,
        content="Bound to the first inject.",
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)
    provider = _fake_provider(_assessment_json())
    test_engine = await session.connection()

    with (
        _patch("app.services.llm_service.active_provider", return_value=provider),
        _patch("app.services.llm_service.engine", test_engine),
        _patch("app.services.ws_manager.manager") as manager,
    ):
        await run_llm_pipeline(response.id, second.id, active_exercise.id)

    provider.complete.assert_not_awaited()
    manager.send_to_facilitators.assert_not_called()
