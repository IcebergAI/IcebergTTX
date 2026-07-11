"""Merged, chronological event feed for one exercise — the AAR foundation (#111).

Assembles a single time-ordered timeline from the tables that record what happened
during an exercise (injects released, responses, communications, inject comments,
and durable state transitions), so the facilitator can replay a run and the
after-action report (#113) can render from one source. Read-only; no schema of its own.

Ownership scoping is enforced by the caller (``require_exercise_owner``) — this service
only reads by ``exercise_id``.
"""

from datetime import datetime

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.assessment import ResponseAssessment
from app.models.communication import Communication
from app.models.exercise import Exercise, ExerciseStateTransition, transition_action
from app.models.inject import Inject
from app.models.inject_comment import InjectComment
from app.models.response import Response

# Stable secondary sort so events sharing a timestamp never swap between calls.
_KIND_ORDER = {
    "state_change": 0,
    "inject_released": 1,
    "response": 2,
    "comment": 3,
    "communication": 4,
}


def _event(at: datetime, kind: str, ref_id: int, **payload) -> dict:
    return {"kind": kind, "at": at.isoformat(), **payload, "_sort": (at, _KIND_ORDER[kind], ref_id)}


async def build_timeline(session: AsyncSession, exercise_id: int) -> list[dict]:
    """Return the merged event feed for ``exercise_id``, oldest first.

    Each event is a dict with ``kind`` + ``at`` (ISO string) plus per-kind payload
    fields. Callers must have already checked ownership.
    """
    exercise = await session.get(Exercise, exercise_id)
    if exercise is None:
        return []

    events: list[dict] = []

    # ── Injects released ──────────────────────────────────────────────────────
    injects = (
        await session.exec(
            select(Inject).where(
                Inject.exercise_id == exercise_id,
                col(Inject.released_at).is_not(None),
            )
        )
    ).all()
    for i in injects:
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

    # ── Responses (+ LLM decision quality where assessed) ─────────────────────
    responses = (
        await session.exec(select(Response).where(Response.exercise_id == exercise_id))
    ).all()
    quality_by_response: dict[int, str | None] = {}
    if responses:
        assessments = (
            await session.exec(
                select(ResponseAssessment).where(
                    col(ResponseAssessment.response_id).in_([r.id for r in responses])
                )
            )
        ).all()
        quality_by_response = {a.response_id: a.decision_quality for a in assessments}
    for r in responses:
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
    comms = (
        await session.exec(select(Communication).where(Communication.exercise_id == exercise_id))
    ).all()
    for c in comms:
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
    comments = (
        await session.exec(select(InjectComment).where(InjectComment.exercise_id == exercise_id))
    ).all()
    for cm in comments:
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
    transitions = (
        await session.exec(
            select(ExerciseStateTransition).where(
                ExerciseStateTransition.exercise_id == exercise_id
            )
        )
    ).all()
    for transition in transitions:
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
