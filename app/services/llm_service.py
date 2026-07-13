import json
import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.database import engine
from app.schemas.api import AssessmentPublic, ExecutiveSummaryPublic, SuggestedInjectPublic
from app.services import llm_settings_service
from app.services.background import spawn
from app.services.llm.service import active_provider
from app.services.team_service import validate_team_ids

logger = logging.getLogger(__name__)

_MAX_LLM_TEXT = 12_000
_MAX_LLM_TITLE = 300
_MAX_LLM_TEAMS = 100


class AssessmentOutput(BaseModel):
    """Strict, untrusted provider response for a participant assessment."""

    model_config = ConfigDict(extra="forbid")
    assessment_text: str = Field(min_length=1, max_length=_MAX_LLM_TEXT)
    decision_quality: Literal["good", "adequate", "poor"] | None = None
    recommended_branch_option_id: str | None = Field(default=None, max_length=200)


class SuggestedInjectOutput(BaseModel):
    """Strict, untrusted provider response for a suggested inject."""

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=_MAX_LLM_TITLE)
    content: str = Field(min_length=1, max_length=_MAX_LLM_TEXT)
    target_teams: list[str] | None = Field(default=None, max_length=_MAX_LLM_TEAMS)


# The application is deliberately single-replica while its real-time and rate-limit
# state is in memory (CLAUDE.md). Keep one assessment task per response in flight so
# automatic and manual triggers cannot duplicate provider calls within that supported
# deployment model. The worker also checks persisted state before calling a provider.
_assessment_inflight: set[int] = set()

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

_SUMMARY_SYSTEM = (
    "You are an expert tabletop exercise facilitator writing the executive summary of an "
    "after-action report for senior leadership. Be concise, factual, and constructive: "
    "cover what happened, how key decisions were made, and the main strengths and areas "
    "for improvement. Reply with plain prose (2-3 short paragraphs) — no JSON, no headings."
)


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


async def _call(provider, system: str, cached_context: str, user_prompt: str) -> str:
    """Delegate one completion to the active provider (provider-agnostic)."""
    return await provider.complete(
        system, cached_context, user_prompt, llm_settings_service.get_config().llm_max_tokens
    )


def _build_context(inject, definition, response) -> tuple[str, str]:
    """Shared LLM prompt context for assess/suggest: the cached scenario+inject
    summary (the prefix-cacheable block) and the optional selected-option line."""
    node = next((n for n in definition.injects if n.id == inject.scenario_node_id), None)
    cached = _scenario_summary(definition) + "\n\n" + _inject_summary(inject, node)
    selected_line = (
        f"\nSelected option: {response.selected_option}" if response.selected_option else ""
    )
    return cached, selected_line


def _parse_json(text: str, fallback: dict) -> dict:
    """Parse an LLM JSON object; return ``fallback`` for malformed/non-object output."""
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else fallback
    except json.JSONDecodeError:
        return fallback


def _validated_suggested_target_teams(value: object, definition) -> list[str] | None:
    """Validate untrusted provider audiences against the scenario's team catalog."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(team, str) for team in value):
        raise ValueError("LLM target_teams must be a list of team id strings or null")
    return validate_team_ids(value, definition, field="LLM target_teams")


async def _assess_response_result(session, response, inject, definition):
    """Return the assessment plus whether this invocation created it."""
    from app.models.assessment import ResponseAssessment

    response_id = response.id
    assert response_id is not None
    if response.assessment_id is not None:
        existing = await session.get(ResponseAssessment, response.assessment_id)
        if existing is not None:
            return existing, False

    existing = (
        await session.exec(
            select(ResponseAssessment).where(ResponseAssessment.response_id == response_id)
        )
    ).one_or_none()
    if existing is not None:
        return existing, False

    provider = active_provider()
    if provider is None:
        return None, False

    cached, selected_line = _build_context(inject, definition, response)
    user_prompt = (
        f"Participant response:\n{response.content}{selected_line}\n\n"
        "Assess this response. Reply as JSON with keys: "
        "\"assessment_text\" (2-3 sentence narrative), "
        "\"decision_quality\" (\"good\", \"adequate\", or \"poor\"), "
        "\"recommended_branch_option_id\" (option id string or null)."
    )

    text = await _call(provider, _ASSESSMENT_SYSTEM, cached, user_prompt)
    data = _parse_json(text, {
        "assessment_text": text,
        "decision_quality": None,
        "recommended_branch_option_id": None,
    })

    try:
        output = AssessmentOutput.model_validate(data)
    except ValidationError:
        output = AssessmentOutput(
            assessment_text=(text.strip() or "Provider returned invalid assessment")[:_MAX_LLM_TEXT]
        )
    option_ids = {option.id for option in node.options} if (node := next(
        (item for item in definition.injects if item.id == inject.scenario_node_id), None
    )) else set()
    if output.recommended_branch_option_id not in option_ids:
        output = output.model_copy(update={"recommended_branch_option_id": None})

    assessment = ResponseAssessment(
        response_id=response_id,
        llm_model=provider.llm_model_label,
        assessment_text=output.assessment_text,
        decision_quality=output.decision_quality,
        recommended_branch_option_id=output.recommended_branch_option_id,
    )
    session.add(assessment)
    try:
        await session.flush()
        assert assessment.id is not None
        response.assessment_id = assessment.id
        session.add(response)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.exec(
                select(ResponseAssessment).where(
                    ResponseAssessment.response_id == response_id
                )
            )
        ).one_or_none()
        if existing is not None:
            return existing, False
        raise
    except Exception:
        await session.rollback()
        raise
    await session.refresh(assessment)

    return assessment, True


async def assess_response(session, response, inject, definition):
    assessment, _ = await _assess_response_result(session, response, inject, definition)
    return assessment


async def _suggest_inject_result(session, response, inject, exercise, definition):
    """Return the suggestion plus whether this invocation created it."""
    from app.models.suggested_inject import SuggestedInject

    response_id = response.id
    assert response_id is not None
    existing = (
        await session.exec(
            select(SuggestedInject).where(
                SuggestedInject.triggered_by_response_id == response_id
            )
        )
    ).first()
    if existing is not None:
        return existing, False

    provider = active_provider()
    if provider is None:
        return None, False

    cached, selected_line = _build_context(inject, definition, response)
    user_prompt = (
        f"Participant response:\n{response.content}{selected_line}\n\n"
        "Suggest a follow-up inject. Reply as JSON with keys: "
        "\"title\" (short string), "
        "\"content\" (inject body text), "
        "\"target_teams\" (list of team id strings from the scenario, or null for all teams)."
    )

    text = await _call(provider, _SUGGESTION_SYSTEM, cached, user_prompt)
    data = _parse_json(text, {"title": "Follow-up inject", "content": text, "target_teams": None})
    try:
        output = SuggestedInjectOutput.model_validate(data)
    except ValidationError:
        output = SuggestedInjectOutput(
            title="Follow-up inject",
            content=(text.strip() or "Provider returned invalid suggestion")[:_MAX_LLM_TEXT],
        )
    target_teams = _validated_suggested_target_teams(output.target_teams, definition)
    suggested = SuggestedInject(
        exercise_id=exercise.id,
        triggered_by_response_id=response_id,
        title=output.title,
        content=output.content,
        target_teams=target_teams,
        llm_model=provider.llm_model_label,
    )
    session.add(suggested)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.exec(
                select(SuggestedInject).where(
                    SuggestedInject.triggered_by_response_id == response_id
                )
            )
        ).one_or_none()
        if existing is not None:
            return existing, False
        raise
    except Exception:
        await session.rollback()
        raise
    await session.refresh(suggested)

    return suggested, True


async def suggest_inject(session, response, inject, exercise, definition):
    suggested, _ = await _suggest_inject_result(
        session, response, inject, exercise, definition
    )
    return suggested


async def run_llm_pipeline(response_id: int, inject_id: int, exercise_id: int) -> None:
    """Background task: assess response and optionally suggest a follow-up inject."""
    try:
        await _run_llm_pipeline(response_id, inject_id, exercise_id)
    except Exception:
        logger.exception("LLM pipeline failed for response %d", response_id)


def queue_llm_pipeline(response_id: int, inject_id: int, exercise_id: int) -> bool:
    """Queue one assessment per response; return whether a task was created."""
    if response_id in _assessment_inflight:
        return False
    _assessment_inflight.add(response_id)
    coroutine = run_llm_pipeline(response_id, inject_id, exercise_id)
    try:
        task = spawn(coroutine)
    except Exception:
        coroutine.close()
        _assessment_inflight.discard(response_id)
        raise
    task.add_done_callback(lambda _: _assessment_inflight.discard(response_id))
    return True


async def _run_llm_pipeline(response_id: int, inject_id: int, exercise_id: int) -> None:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.assessment import ResponseAssessment
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
        if not (
            response
            and inject
            and exercise
            and exercise.scenario_id
            and exercise.llm_enabled
        ):
            return
        if (
            response.exercise_id != exercise_id
            or response.inject_id != inject_id
            or inject.exercise_id != exercise_id
        ):
            return

        # A completed assessment makes retries idempotent. Also repair the legacy
        # two-commit failure mode where the assessment row exists but Response was
        # never updated to point at it.
        existing = (
            await session.exec(
                select(ResponseAssessment).where(ResponseAssessment.response_id == response_id)
            )
        ).first()
        if existing is not None:
            if response.assessment_id != existing.id:
                response.assessment_id = existing.id
                session.add(response)
                await session.commit()
            return

        if active_provider() is None:
            logger.info("LLM pipeline skipped: no AI provider configured (LLM_PROVIDER)")
            return

        scenario = await session.get(Scenario, exercise.scenario_id)
        if not scenario:
            return

        definition = export_definition(scenario)

        assessment, assessment_created = await _assess_response_result(
            session, response, inject, definition
        )

        if assessment is not None and assessment_created:
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
            suggested, suggested_created = await _suggest_inject_result(
                session, response, inject, exercise, definition
            )
            if suggested is not None and suggested_created:
                await manager.send_to_facilitators(
                    exercise_id,
                    {
                        "type": "inject_suggested",
                        "exercise_id": exercise_id,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "payload": _suggested_payload(suggested),
                    },
                )


def _build_summary_context(report: dict) -> tuple[str, str]:
    """Cacheable scenario block + the per-run decision/comms/debrief prompt."""
    sc = report["scenario"]
    teams = ", ".join(t["label"] for t in report["teams"]) or "All participants"
    cached = f"Scenario: {sc['title']}\n{sc['description'] or ''}\nParticipant teams: {teams}"

    decisions: list[str] = []
    for inj in report["injects"]:
        decisions.append(f"Inject '{inj['title']}':")
        for r in inj["responses"]:
            q = f" [{r['decision_quality']}]" if r["decision_quality"] else ""
            decisions.append(f"- {r['author']}{q}: {r['content']}")
    comms = [f"- [{c['direction']}] {c['subject']}" for c in report["communications"]]
    debrief = report["debrief"]["debrief_notes"] or "None"

    user_prompt = (
        "Write the executive summary for this exercise.\n\n"
        "Decisions:\n" + ("\n".join(decisions) or "None recorded") + "\n\n"
        "Communications:\n" + ("\n".join(comms) or "None") + "\n\n"
        "Facilitator observations:\n" + debrief
    )
    return cached, user_prompt


async def generate_executive_summary(session, exercise_id: int):
    """Generate (or regenerate) the executive summary for one exercise. Upserts the
    single ExecutiveSummary row and resets ``edited``. Returns None when disabled."""
    from app.models.report_summary import ExecutiveSummary
    from app.services.report_service import build_report

    provider = active_provider()
    if provider is None:
        return None

    report = await build_report(session, exercise_id)
    if report is None:
        return None

    cached, user_prompt = _build_summary_context(report)
    text = (await _call(provider, _SUMMARY_SYSTEM, cached, user_prompt)).strip()

    existing = (
        await session.exec(
            select(ExecutiveSummary).where(ExecutiveSummary.exercise_id == exercise_id)
        )
    ).first()
    if existing is not None:
        existing.summary_text = text
        existing.llm_model = provider.llm_model_label
        existing.generated_at = datetime.now(UTC)
        existing.edited = False
        summary = existing
    else:
        summary = ExecutiveSummary(
            exercise_id=exercise_id,
            summary_text=text,
            llm_model=provider.llm_model_label,
        )
    session.add(summary)
    await session.commit()
    await session.refresh(summary)
    return summary


async def run_summary_pipeline(exercise_id: int) -> None:
    """Background task (#113): draft the after-action executive summary."""
    try:
        await _run_summary_pipeline(exercise_id)
    except Exception:
        logger.exception("Summary pipeline failed for exercise %d", exercise_id)


async def _run_summary_pipeline(exercise_id: int) -> None:
    if active_provider() is None:
        logger.info("Summary pipeline skipped: no AI provider configured (LLM_PROVIDER)")
        return

    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.exercise import Exercise
    from app.services.ws_manager import manager

    async with AsyncSession(engine, expire_on_commit=False) as session:
        exercise = await session.get(Exercise, exercise_id)
        # Gate on the exercise's own AI opt-in too, matching the assessment path.
        if not exercise or not exercise.llm_enabled:
            return
        summary = await generate_executive_summary(session, exercise_id)
        if summary is None:
            return
        await manager.send_to_facilitators(
            exercise_id,
            {
                "type": "summary_ready",
                "exercise_id": exercise_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "payload": _summary_payload(summary),
            },
        )


def _summary_payload(s) -> dict:
    return ExecutiveSummaryPublic.from_model(s).model_dump(mode="json")


def _assessment_payload(a) -> dict:
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
    return SuggestedInjectPublic(
        id=s.id,
        exercise_id=s.exercise_id,
        triggered_by_response_id=s.triggered_by_response_id,
        title=s.title,
        content=s.content,
        target_teams=s.target_teams,
        llm_model=s.llm_model,
        status=s.status,
        reviewed_by=s.reviewed_by,
        reviewed_at=s.reviewed_at.isoformat() if s.reviewed_at else None,
        generated_at=s.generated_at.isoformat(),
    ).model_dump(mode="json")
