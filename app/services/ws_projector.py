"""The WebSocket projection of the domain events (#212).

Every frame the app pushes to a client is built here, and nowhere else. Services record
what happened (``domain_events``); this module decides what that looks like on the wire
and who receives it.

That single choke point is the point. Before this, nine call sites across five services
each reached into ``ws_manager`` directly, so "who is this frame for?" was a question you
answered by grepping. It also means multi-replica (#213) has exactly one seam to replace:
a Redis-backed fan-out swaps ``manager`` here, and no service changes.

The frame envelope is uniform — ``{type, exercise_id, timestamp, payload}`` — and the
payload dicts are built through the ``schemas/api`` models wherever one exists, so the
HTTP and WebSocket representations of the same thing cannot drift (#21, #31).
"""

from __future__ import annotations

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


def _frame(kind: str, exercise_id: int, payload: Any, timestamp: str | None = None) -> dict:
    return {
        "type": kind,
        "exercise_id": exercise_id,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "payload": payload,
    }


@subscribe(ExerciseStateChanged)
async def on_exercise_state_changed(session: AsyncSession, ev: ExerciseStateChanged) -> None:
    """The canonical lifecycle frame (#129).

    Carries the full pause-aware timing so every client's clock stays correct without
    per-second traffic — the clock ticks locally. The timestamp is the *durable* event's,
    not the moment we happened to broadcast.
    """
    from app.models.exercise import Exercise

    transition = ev.transition
    assert transition.id is not None
    exercise = await session.get(Exercise, ev.exercise_id)
    assert exercise is not None
    payload = ExerciseStateChange(
        transition_id=transition.id,
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
    from app.services.inject_service import inject_payload, inject_target_groups

    # Participants/observers get the redacted payload; facilitators get the full one
    # with branch topology (#266). The manager routes the two frames by role in one pass.
    base = _frame("inject_released", ev.exercise_id, await inject_payload(session, ev.inject))
    full = _frame(
        "inject_released",
        ev.exercise_id,
        await inject_payload(session, ev.inject, include_progression=True),
    )
    groups = inject_target_groups(ev.inject)
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
    from app.services.inject_service import inject_payload

    await manager.send_to_facilitators(
        ev.exercise_id,
        _frame(
            "inject_updated",
            ev.exercise_id,
            await inject_payload(session, ev.inject, include_progression=True),
        ),
    )


@subscribe(InjectCommentCreated)
async def on_inject_comment_created(session: AsyncSession, ev: InjectCommentCreated) -> None:
    frame = _frame("inject_comment_created", ev.exercise_id, ev.payload)
    if ev.comment.group_id:
        await manager.broadcast_to_groups(ev.exercise_id, [ev.comment.group_id], frame)
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
    from app.services.progression_service import progression_snapshot
    from app.services.response_service import (
        compute_next_inject_ids,
        pending_next_injects,
        response_payload,
    )

    r = ev.response
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
    if comm is None:  # deleted between commit and dispatch
        return
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
    await manager.send_to_facilitators(
        ev.exercise_id,
        _frame(
            "assessment_ready",
            ev.exercise_id,
            {"response_id": ev.response_id, "assessment": ev.payload},
        ),
    )


@subscribe(InjectSuggested)
async def on_inject_suggested(session: AsyncSession, ev: InjectSuggested) -> None:
    from app.services.llm_service import suggested_payload

    await manager.send_to_facilitators(
        ev.exercise_id,
        _frame("inject_suggested", ev.exercise_id, suggested_payload(ev.suggested)),
    )


@subscribe(SummaryGenerated)
async def on_summary_generated(session: AsyncSession, ev: SummaryGenerated) -> None:
    await manager.send_to_facilitators(
        ev.exercise_id, _frame("summary_ready", ev.exercise_id, ev.payload)
    )


# ── Non-WebSocket subscribers ─────────────────────────────────────────────────


@subscribe(InjectReleased)
async def schedule_triggered_communications(session: AsyncSession, ev: InjectReleased) -> None:
    """A second subscriber to the same event, and the reason this is a bus rather than a
    broadcast helper: triggered comms are a post-commit consequence of a release in
    exactly the way the frame is, and they used to be a hand-sequenced call inside
    ``release_inject``. Now the release just announces itself."""
    from app.services.communication_service import schedule_triggered_comms
    from app.services.scenario_service import definition_for_exercise, get_inject_node

    inject = ev.inject
    if not inject.scenario_node_id:
        return
    definition = await definition_for_exercise(session, ev.exercise_id)
    if not definition:
        return
    node = get_inject_node(definition, inject.scenario_node_id)
    if node and node.triggers_communications:
        schedule_triggered_comms(inject, node.triggers_communications, node.id)


@subscribe(ResponseSubmitted)
async def arm_schedules_the_response_unlocked(session: AsyncSession, ev: ResponseSubmitted) -> None:
    """A response is the only thing that advances a progression cursor, and a cursor
    advance is the only thing that unlocks a scheduled inject the team had not reached
    (#218). So the re-arm belongs here, on the same post-commit seam as the frame: arming a
    timer against a cursor advance that then rolled back would release an inject the
    participants never chose.

    Registered after ``on_response_submitted`` — subscriber order is registration order, so
    the ``response_submitted`` frame still lists the newly-armed inject as pending, and any
    immediate ``inject_released`` follows it rather than racing it.
    """
    from app.models.exercise import Exercise
    from app.services.schedule_service import arm_cursor_reached_injects

    exercise = await session.get(Exercise, ev.exercise_id)
    if exercise is not None:
        await arm_cursor_reached_injects(session, exercise)
