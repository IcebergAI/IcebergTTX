"""The WebSocket projection of the domain events (#212).

Every frame the app pushes to a client is built here, and nowhere else. Services record
what happened (``domain_events``); this module decides what that looks like on the wire
and who receives it.

That single choke point is the point. Before this, nine call sites across five services
each reached into ``ws_manager`` directly, so "who is this frame for?" was a question you
answered by grepping. It is also what made multi-replica (#213) a change of one seam:
these handlers run identically whether the event was recorded by this replica or relayed
from another over the Postgres bus, because they are given ids and read the rows
themselves. ``ws_manager`` stays per-replica — it holds this process's actual sockets.

The frame envelope is uniform — ``{type, exercise_id, timestamp, payload}`` — and the
payload dicts are built through the ``schemas/api`` models wherever one exists, so the
HTTP and WebSocket representations of the same thing cannot drift (#21, #31).

Every handler tolerates a missing row. Between the commit and the projection — and
especially across the bus, where the read happens on another machine — the row can have
been deleted, and a retention sweep or a cascading delete must not fill the log with
tracebacks.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.schemas.api import ExerciseStateChange
from app.services.domain_events import (
    CommunicationCreated,
    ExerciseStateChanged,
    InjectCommentCreated,
    InjectReleased,
    InjectSuggested,
    InjectUpdated,
    ResponseAssessed,
    ResponseSubmitted,
    SummaryGenerated,
    subscribe,
)
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)


def _frame(kind: str, exercise_id: int, payload: Any, timestamp: str | None = None) -> dict:
    return {
        "type": kind,
        "exercise_id": exercise_id,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "payload": payload,
    }


def _gone(what: str, ident: int) -> None:
    logger.debug("skipping projection: %s %s no longer exists", what, ident)


@subscribe(ExerciseStateChanged)
async def on_exercise_state_changed(session: AsyncSession, ev: ExerciseStateChanged) -> None:
    """The canonical lifecycle frame (#129).

    Carries the full pause-aware timing so every client's clock stays correct without
    per-second traffic — the clock ticks locally. The timestamp is the *durable* event's,
    not the moment we happened to broadcast.
    """
    from app.models.exercise import Exercise, ExerciseStateTransition

    transition = await session.get(ExerciseStateTransition, ev.transition_id)
    if transition is None:
        return _gone("transition", ev.transition_id)
    exercise = await session.get(Exercise, ev.exercise_id)
    if exercise is None:
        return _gone("exercise", ev.exercise_id)
    payload = ExerciseStateChange(
        transition_id=ev.transition_id,
        exercise_id=ev.exercise_id,
        previous_state=transition.from_state,
        new_state=transition.to_state,
        state=transition.to_state,
        actor_id=transition.actor_id,
        transitioned_at=transition.transitioned_at.isoformat(),
        started_at=exercise.started_at.isoformat() if exercise.started_at else None,
        ended_at=exercise.ended_at.isoformat() if exercise.ended_at else None,
        paused_at=exercise.paused_at.isoformat() if exercise.paused_at else None,
        accumulated_pause_seconds=exercise.accumulated_pause_seconds,
    ).model_dump(mode="json")
    await manager.broadcast_to_exercise(
        ev.exercise_id,
        _frame(
            "exercise_state_change",
            ev.exercise_id,
            payload,
            timestamp=transition.transitioned_at.isoformat(),
        ),
    )


@subscribe(InjectReleased)
async def on_inject_released(session: AsyncSession, ev: InjectReleased) -> None:
    from app.models.inject import Inject
    from app.services.inject_service import inject_payload, inject_target_groups

    inject = await session.get(Inject, ev.inject_id)
    if inject is None:
        return _gone("inject", ev.inject_id)
    # Participants/observers get the redacted payload; facilitators get the full one
    # with branch topology (#266). The manager routes the two frames by role in one pass.
    base = _frame("inject_released", ev.exercise_id, await inject_payload(session, inject))
    full = _frame(
        "inject_released",
        ev.exercise_id,
        await inject_payload(session, inject, include_progression=True),
    )
    groups = inject_target_groups(inject)
    if groups:
        await manager.broadcast_to_groups(
            ev.exercise_id, groups, base, facilitator_message=full
        )
    else:
        await manager.broadcast_to_exercise(ev.exercise_id, base, facilitator_message=full)


@subscribe(InjectUpdated)
async def on_inject_updated(session: AsyncSession, ev: InjectUpdated) -> None:
    """Facilitator-only: participants see *released* injects, so a pending inject's
    schedule edit is irrelevant to them and stays off their socket."""
    from app.models.inject import Inject
    from app.services.inject_service import inject_payload

    inject = await session.get(Inject, ev.inject_id)
    if inject is None:
        return _gone("inject", ev.inject_id)
    await manager.send_to_facilitators(
        ev.exercise_id,
        _frame(
            "inject_updated",
            ev.exercise_id,
            await inject_payload(session, inject, include_progression=True),
        ),
    )


@subscribe(InjectCommentCreated)
async def on_inject_comment_created(session: AsyncSession, ev: InjectCommentCreated) -> None:
    from app.models.inject_comment import InjectComment
    from app.services.inject_comment_service import comment_payload

    comment = await session.get(InjectComment, ev.comment_id)
    if comment is None:
        return _gone("comment", ev.comment_id)
    payload = await comment_payload(session, comment)
    frame = _frame("inject_comment_created", ev.exercise_id, payload)
    if comment.group_id:
        await manager.broadcast_to_groups(ev.exercise_id, [comment.group_id], frame)
    else:
        await manager.broadcast_to_exercise(ev.exercise_id, frame)


@subscribe(ResponseSubmitted)
async def on_response_submitted(session: AsyncSession, ev: ResponseSubmitted) -> None:
    """Facilitators get the response plus the branch the team's choice resolved to.

    ``next_injects`` and the progression snapshot are recomputed here rather than carried
    on the event, because both are post-commit reads — the pending-inject filter and the
    cursor can only be read once the response is durable. They were computed post-commit
    before this seam too, so the payload is unchanged.
    """
    from app.models.response import Response
    from app.services.progression_service import progression_snapshot
    from app.services.response_service import (
        compute_next_inject_ids,
        pending_next_injects,
        response_payload,
    )

    r = await session.get(Response, ev.response_id)
    if r is None:
        return _gone("response", ev.response_id)
    next_ids = await compute_next_inject_ids(
        session, ev.exercise_id, r.inject_id, r.selected_option
    )
    next_injects = await pending_next_injects(session, ev.exercise_id, next_ids, r.group_id)
    progression = await progression_snapshot(session, ev.exercise_id, include_all_groups=True)
    await manager.send_to_facilitators(
        ev.exercise_id,
        _frame(
            "response_submitted",
            ev.exercise_id,
            {
                "response": response_payload(r),
                # scenario-node ids, not Inject ids — see next_injects[] for the latter
                "next_inject_ids": [item["scenario_node_id"] for item in next_injects],
                "next_injects": next_injects,
                "progression": progression,
            },
        ),
    )


@subscribe(CommunicationCreated)
async def on_communication_created(session: AsyncSession, ev: CommunicationCreated) -> None:
    """The most selective fan-out in the app.

    Outbound comms are the participants' own messages, so they go back to the sender and
    the facilitators (plus the sender's team, where there is one); inbound comms are the
    simulation talking, and reach whichever teams they are addressed to.
    """
    from app.models.communication import CommDirection, Communication
    from app.services.communication_service import comm_payload

    comm = await session.get(Communication, ev.communication_id)
    if comm is None:
        return _gone("communication", ev.communication_id)
    # comm_payload is called WITHOUT a session, and the fan-out reads the raw
    # visible_to_teams column rather than the inbound-expanding helper — both exactly as
    # the inline broadcast did. Handing it the live session here would silently start
    # resolving sender_team from the database and change the frame.
    frame = _frame("communication_received", ev.exercise_id, await comm_payload(comm))
    teams = comm.visible_to_teams
    if comm.direction == CommDirection.outbound:
        if teams:
            await manager.send_to_facilitators_user_and_groups(
                ev.exercise_id, comm.sender_id, teams, frame
            )
        else:
            await manager.send_to_facilitators_and_user(ev.exercise_id, comm.sender_id, frame)
    elif teams:
        await manager.broadcast_to_groups(ev.exercise_id, teams, frame)
    else:
        await manager.broadcast_to_exercise(ev.exercise_id, frame)


@subscribe(ResponseAssessed)
async def on_response_assessed(session: AsyncSession, ev: ResponseAssessed) -> None:
    from app.models.assessment import ResponseAssessment
    from app.services.llm_service import assessment_payload

    assessment = await session.get(ResponseAssessment, ev.assessment_id)
    if assessment is None:
        return _gone("assessment", ev.assessment_id)
    await manager.send_to_facilitators(
        ev.exercise_id,
        _frame(
            "assessment_ready",
            ev.exercise_id,
            {"response_id": ev.response_id, "assessment": assessment_payload(assessment)},
        ),
    )


@subscribe(InjectSuggested)
async def on_inject_suggested(session: AsyncSession, ev: InjectSuggested) -> None:
    from app.models.suggested_inject import SuggestedInject
    from app.services.llm_service import suggested_payload

    suggested = await session.get(SuggestedInject, ev.suggested_id)
    if suggested is None:
        return _gone("suggested inject", ev.suggested_id)
    await manager.send_to_facilitators(
        ev.exercise_id,
        _frame("inject_suggested", ev.exercise_id, suggested_payload(suggested)),
    )


@subscribe(SummaryGenerated)
async def on_summary_generated(session: AsyncSession, ev: SummaryGenerated) -> None:
    from app.models.report_summary import ExecutiveSummary
    from app.services.llm_service import summary_payload

    summary = await session.get(ExecutiveSummary, ev.summary_id)
    if summary is None:
        return _gone("summary", ev.summary_id)
    await manager.send_to_facilitators(
        ev.exercise_id, _frame("summary_ready", ev.exercise_id, summary_payload(summary))
    )


# Every subscriber here is a pure projection, and that is now a property worth keeping:
# each one renders a frame for whichever sockets its own replica holds, so running it
# everywhere is exactly right. Work that *acts* — arming a schedule, delivering a comm —
# moved into the transaction that occasions it (see ``schedule_service``), because
# running it once per replica would do it once per replica. If something ever has to act
# from here, register it with ``subscribe_local``.
