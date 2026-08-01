"""Merged, chronological event feed for one exercise — the AAR foundation (#111).

Assembles a single time-ordered timeline from the tables that record what happened
during an exercise (injects released, responses, communications, inject comments,
and durable state transitions), so the facilitator can replay a run and the
after-action report (#113) can render from one source. Read-only; no schema of its own.

Ownership scoping is enforced by the caller (``require_exercise_owner``) — this service
only reads by ``exercise_id``.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.assessment import ResponseAssessment
from app.models.communication import Communication
from app.models.exercise import (
    Exercise,
    ExerciseMember,
    ExerciseStateTransition,
    transition_action,
)
from app.models.inject import Inject, InjectProgress, InjectState
from app.models.inject_comment import InjectComment
from app.models.report_summary import ExecutiveSummary
from app.models.response import Response
from app.models.scenario import Scenario
from app.models.user import User
from app.schemas.scenario_json import ScenarioDefinition
from app.services import user_service
from app.services.scenario_service import export_definition

# Stable secondary sort so events sharing a timestamp never swap between calls.
_KIND_ORDER = {
    "state_change": 0,
    "inject_released": 1,
    "inject_resolved": 2,
    "response": 3,
    "comment": 4,
    "communication": 5,
}


@dataclass(frozen=True)
class ExerciseBundle:
    """One authoritative read snapshot for every exercise projection."""

    exercise: Exercise
    scenario: Scenario | None
    definition: ScenarioDefinition | None
    users: tuple[User, ...]
    members: tuple[ExerciseMember, ...]
    injects: tuple[Inject, ...]
    resolutions: tuple[InjectProgress, ...]
    responses: tuple[Response, ...]
    assessments: tuple[ResponseAssessment, ...]
    communications: tuple[Communication, ...]
    comments: tuple[InjectComment, ...]
    transitions: tuple[ExerciseStateTransition, ...]
    summary: ExecutiveSummary | None


async def load_exercise_bundle(
    session: AsyncSession, exercise_id: int
) -> ExerciseBundle | None:
    """Load each exercise-domain table once for report/timeline/export projections."""
    exercise = await session.get(Exercise, exercise_id)
    if exercise is None:
        return None
    scenario = await session.get(Scenario, exercise.scenario_id)
    definition = export_definition(scenario) if scenario else None
    members = tuple(
        (
            await session.exec(
                select(ExerciseMember).where(ExerciseMember.exercise_id == exercise_id)
            )
        ).all()
    )
    injects = tuple(
        (await session.exec(select(Inject).where(Inject.exercise_id == exercise_id))).all()
    )
    resolutions = tuple(
        (
            await session.exec(
                select(InjectProgress).where(InjectProgress.exercise_id == exercise_id)
            )
        ).all()
    )
    responses = tuple(
        (await session.exec(select(Response).where(Response.exercise_id == exercise_id))).all()
    )
    response_ids = [response.id for response in responses if response.id is not None]
    assessments = (
        tuple(
            (
                await session.exec(
                    select(ResponseAssessment).where(
                        col(ResponseAssessment.response_id).in_(response_ids)
                    )
                )
            ).all()
        )
        if response_ids
        else ()
    )
    communications = tuple(
        (
            await session.exec(
                select(Communication).where(Communication.exercise_id == exercise_id)
            )
        ).all()
    )
    comments = tuple(
        (
            await session.exec(
                select(InjectComment).where(InjectComment.exercise_id == exercise_id)
            )
        ).all()
    )
    transitions = tuple(
        (
            await session.exec(
                select(ExerciseStateTransition).where(
                    ExerciseStateTransition.exercise_id == exercise_id
                )
            )
        ).all()
    )
    summary = (
        await session.exec(
            select(ExecutiveSummary).where(ExecutiveSummary.exercise_id == exercise_id)
        )
    ).first()
    # Only the users this exercise actually references — the bundle's sole use of them is
    # the report name map (report_service). An unscoped `select(User)` loaded every account
    # on the instance to name a handful of participants (#245).
    referenced_ids = {
        exercise.created_by,
        *(member.user_id for member in members),
        *(inject.released_by for inject in injects),
        *(inject.resolved_by for inject in injects),
        *(resolution.resolved_by for resolution in resolutions),
        *(response.user_id for response in responses),
        *(communication.sender_id for communication in communications),
        *(comment.user_id for comment in comments),
        *(transition.actor_id for transition in transitions),
    }
    users = tuple(
        await user_service.get_by_ids(
            session, {uid for uid in referenced_ids if uid is not None}
        )
    )
    return ExerciseBundle(
        exercise=exercise,
        scenario=scenario,
        definition=definition,
        users=users,
        members=members,
        injects=injects,
        resolutions=resolutions,
        responses=responses,
        assessments=assessments,
        communications=communications,
        comments=comments,
        transitions=transitions,
        summary=summary,
    )


def inject_resolution_projection(bundle: ExerciseBundle, inject: Inject) -> dict:
    """Derive the compatible scalar state from authoritative per-context rows."""
    assert inject.id is not None
    rows = [row for row in bundle.resolutions if row.inject_id == inject.id]
    resolved = [row for row in rows if row.state == InjectState.resolved]
    complete = bool(rows) and len(resolved) == len(rows)
    if complete:
        last = max(
            resolved,
            key=lambda row: row.resolved_at or datetime.min.replace(tzinfo=UTC),
        )
        state = InjectState.resolved
        resolved_at = last.resolved_at
        resolved_by = last.resolved_by
        reason = last.resolution_reason
    elif rows:
        # Once progression exists the compatible scalar remains partial until every
        # enrolled participant context has resolved.
        state = InjectState.released if inject.released_at else InjectState.pending
        resolved_at = None
        resolved_by = None
        reason = None
    else:
        state = inject.state
        resolved_at = inject.resolved_at
        resolved_by = inject.resolved_by
        reason = inject.resolution_reason
    return {
        "state": state,
        "resolved_at": resolved_at,
        "resolved_by": resolved_by,
        "resolution_reason": reason,
        "resolutions": rows,
    }


def _event(at: datetime, kind: str, ref_id: int, **payload) -> dict:
    return {"kind": kind, "at": at.isoformat(), **payload, "_sort": (at, _KIND_ORDER[kind], ref_id)}


async def build_timeline(
    session: AsyncSession,
    exercise_id: int,
    *,
    bundle: ExerciseBundle | None = None,
) -> list[dict]:
    """Return the merged event feed for ``exercise_id``, oldest first.

    Each event is a dict with ``kind`` + ``at`` (ISO string) plus per-kind payload
    fields. Callers must have already checked ownership.
    """
    bundle = bundle or await load_exercise_bundle(session, exercise_id)
    if bundle is None:
        return []

    events: list[dict] = []

    # ── Injects released ──────────────────────────────────────────────────────
    injects = [inject for inject in bundle.injects if inject.released_at is not None]
    for i in injects:
        assert i.id is not None
        assert i.released_at is not None  # guaranteed by the WHERE clause
        events.append(
            _event(
                i.released_at,
                "inject_released",
                i.id,
                inject_id=i.id,
                scenario_node_id=i.scenario_node_id,
                title=i.title,
                target_teams=i.target_teams,  # None = all teams
                group_id=i.group_id,
                released_by=i.released_by,
            )
        )

    inject_titles = {inject.id: inject.title for inject in injects}
    resolutions = [
        resolution
        for resolution in bundle.resolutions
        if resolution.resolved_at is not None
    ]
    for resolution in resolutions:
        assert resolution.resolved_at is not None
        events.append(
            _event(
                resolution.resolved_at,
                "inject_resolved",
                resolution.id or 0,
                inject_id=resolution.inject_id,
                title=inject_titles.get(resolution.inject_id),
                group_id=resolution.group_id,
                resolved_by=resolution.resolved_by,
                resolution_reason=resolution.resolution_reason,
            )
        )

    # ── Responses (+ LLM decision quality where assessed) ─────────────────────
    quality_by_response = {
        assessment.response_id: assessment.decision_quality
        for assessment in bundle.assessments
    }
    for r in bundle.responses:
        assert r.id is not None
        events.append(
            _event(
                r.submitted_at,
                "response",
                r.id,
                response_id=r.id,
                inject_id=r.inject_id,
                user_id=r.user_id,
                group_id=r.group_id,
                selected_option=r.selected_option,
                content=r.content,
                decision_quality=quality_by_response.get(r.id),
            )
        )

    # ── Communications ────────────────────────────────────────────────────────
    for c in bundle.communications:
        assert c.id is not None
        events.append(
            _event(
                c.sent_at,
                "communication",
                c.id,
                communication_id=c.id,
                direction=c.direction,
                external_entity=c.external_entity,
                subject=c.subject,
                sender_id=c.sender_id,
                sender_team=c.sender_team,
                visible_to_teams=c.visible_to_teams,  # None = all teams
                triggered_by_inject_id=c.triggered_by_inject_id,
            )
        )

    # ── Inject comments ───────────────────────────────────────────────────────
    for cm in bundle.comments:
        assert cm.id is not None
        events.append(
            _event(
                cm.created_at,
                "comment",
                cm.id,
                comment_id=cm.id,
                inject_id=cm.inject_id,
                user_id=cm.user_id,
                group_id=cm.group_id,
                content=cm.content,
            )
        )

    # ── State transitions ─────────────────────────────────────────────────────
    # Lifecycle history is an authoritative domain record committed in the same
    # transaction as Exercise.state (#129), not an optional audit-log projection.
    for transition in bundle.transitions:
        events.append(
            _event(
                transition.transitioned_at,
                "state_change",
                transition.id or 0,
                transition_id=transition.id,
                action=transition_action(transition.from_state, transition.to_state),
                actor_id=transition.actor_id,
                previous_state=transition.from_state,
                new_state=transition.to_state,
            )
        )

    events.sort(key=lambda e: e["_sort"])
    for e in events:
        del e["_sort"]
    return events
