import json
import logging
from datetime import UTC, datetime

import anthropic

from app.config import settings
from app.database import engine

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

_ASSESSMENT_SYSTEM = (
    "You are an expert tabletop exercise facilitator. "
    "Assess participant responses concisely and constructively. "
    "Reply only with valid JSON."
)

_SUGGESTION_SYSTEM = (
    "You are an expert tabletop exercise facilitator. "
    "Suggest realistic, challenging follow-up injects that build on participant decisions. "
    "Reply only with valid JSON."
)


def _async_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _scenario_summary(definition) -> str:
    teams = ", ".join(t.label for t in definition.participant_teams) or "All participants"
    return (
        f"Scenario: {definition.title}\n"
        f"{definition.description or ''}\n"
        f"Participant teams: {teams}"
    )


def _inject_summary(inject, node) -> str:
    lines = [f"Inject: {inject.title}", inject.content]
    if node and node.options:
        lines.append("Options: " + "; ".join(f"{o.id}={o.label}" for o in node.options))
    if node and node.expected_actions:
        lines.append("Expected actions: " + "; ".join(node.expected_actions))
    return "\n".join(lines)


async def _call(system: str, cached_context: str, user_prompt: str) -> str:
    client = _async_client()
    msg = await client.messages.create(
        model=_MODEL,
        max_tokens=600,
        system=system,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": cached_context,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": user_prompt},
                ],
            }
        ],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    for block in msg.content:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return ""


async def assess_response(session, response, inject, definition):
    from app.models.assessment import ResponseAssessment

    node = next((n for n in definition.injects if n.id == inject.scenario_node_id), None)
    cached = _scenario_summary(definition) + "\n\n" + _inject_summary(inject, node)

    selected_line = (
        f"\nSelected option: {response.selected_option}" if response.selected_option else ""
    )
    user_prompt = (
        f"Participant response:\n{response.content}{selected_line}\n\n"
        "Assess this response. Reply as JSON with keys: "
        "\"assessment_text\" (2-3 sentence narrative), "
        "\"decision_quality\" (\"good\", \"adequate\", or \"poor\"), "
        "\"recommended_branch_option_id\" (option id string or null)."
    )

    text = await _call(_ASSESSMENT_SYSTEM, cached, user_prompt)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {
            "assessment_text": text,
            "decision_quality": None,
            "recommended_branch_option_id": None,
        }

    assessment = ResponseAssessment(
        response_id=response.id,
        llm_model=_MODEL,
        assessment_text=data.get("assessment_text", text),
        decision_quality=data.get("decision_quality"),
        recommended_branch_option_id=data.get("recommended_branch_option_id"),
    )
    session.add(assessment)
    await session.commit()
    await session.refresh(assessment)

    response.assessment_id = assessment.id
    session.add(response)
    await session.commit()

    return assessment


async def suggest_inject(session, response, inject, exercise, definition):
    from app.models.suggested_inject import SuggestedInject

    node = next((n for n in definition.injects if n.id == inject.scenario_node_id), None)
    cached = _scenario_summary(definition) + "\n\n" + _inject_summary(inject, node)

    selected_line = (
        f"\nSelected option: {response.selected_option}" if response.selected_option else ""
    )
    user_prompt = (
        f"Participant response:\n{response.content}{selected_line}\n\n"
        "Suggest a follow-up inject. Reply as JSON with keys: "
        "\"title\" (short string), "
        "\"content\" (inject body text), "
        "\"target_teams\" (list of team id strings from the scenario, or null for all teams)."
    )

    text = await _call(_SUGGESTION_SYSTEM, cached, user_prompt)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"title": "Follow-up inject", "content": text, "target_teams": None}

    target_teams = data.get("target_teams")
    suggested = SuggestedInject(
        exercise_id=exercise.id,
        triggered_by_response_id=response.id,
        title=data.get("title", "Follow-up inject"),
        content=data.get("content", text),
        target_teams=json.dumps(target_teams) if target_teams else None,
        llm_model=_MODEL,
    )
    session.add(suggested)
    await session.commit()
    await session.refresh(suggested)

    return suggested


async def run_llm_pipeline(response_id: int, inject_id: int, exercise_id: int) -> None:
    """Background task: assess response and optionally suggest a follow-up inject."""
    try:
        await _run_llm_pipeline(response_id, inject_id, exercise_id)
    except Exception:
        logger.exception("LLM pipeline failed for response %d", response_id)


async def _run_llm_pipeline(response_id: int, inject_id: int, exercise_id: int) -> None:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.exercise import Exercise
    from app.models.inject import Inject
    from app.models.response import Response
    from app.models.scenario import Scenario
    from app.services.scenario_service import export_definition
    from app.services.ws_manager import manager

    async with AsyncSession(engine, expire_on_commit=False) as session:
        response = await session.get(Response, response_id)
        inject = await session.get(Inject, inject_id)
        exercise = await session.get(Exercise, exercise_id)
        if not (response and inject and exercise and exercise.scenario_id):
            return
        scenario = await session.get(Scenario, exercise.scenario_id)
        if not scenario:
            return

        definition = export_definition(scenario)

        assessment = await assess_response(session, response, inject, definition)

        await manager.send_to_facilitators(
            exercise_id,
            {
                "type": "assessment_ready",
                "exercise_id": exercise_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": {
                    "response_id": response_id,
                    "assessment": _assessment_payload(assessment),
                },
            },
        )

        node = next((n for n in definition.injects if n.id == inject.scenario_node_id), None)
        if node and node.free_text_response:
            suggested = await suggest_inject(session, response, inject, exercise, definition)

            await manager.send_to_facilitators(
                exercise_id,
                {
                    "type": "inject_suggested",
                    "exercise_id": exercise_id,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "payload": _suggested_payload(suggested),
                },
            )


def _assessment_payload(a) -> dict:
    from app.schemas.api import AssessmentPublic

    return AssessmentPublic(
        id=a.id,
        response_id=a.response_id,
        llm_model=a.llm_model,
        assessment_text=a.assessment_text,
        decision_quality=a.decision_quality,
        recommended_branch_option_id=a.recommended_branch_option_id,
        assessed_at=a.assessed_at.isoformat(),
    ).model_dump(mode="json")


def _suggested_payload(s) -> dict:
    from app.schemas.api import SuggestedInjectPublic

    return SuggestedInjectPublic(
        id=s.id,
        exercise_id=s.exercise_id,
        triggered_by_response_id=s.triggered_by_response_id,
        title=s.title,
        content=s.content,
        target_teams=json.loads(s.target_teams) if s.target_teams else None,
        llm_model=s.llm_model,
        status=s.status,
        reviewed_by=s.reviewed_by,
        reviewed_at=s.reviewed_at.isoformat() if s.reviewed_at else None,
        generated_at=s.generated_at.isoformat(),
    ).model_dump(mode="json")
