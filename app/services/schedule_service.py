"""Pause-aware, durable exercise scheduling (#116, #194, #211, #218, #213).

An inject may carry a ``release_offset_minutes`` — minutes after the exercise's effective
start at which it auto-releases. Scenario ``triggers_communications`` work the same way,
measured from their inject's release.

Both are **jobs in Postgres**, enqueued inside the transaction that makes them due (see
``task_queue``). That is what makes them survive a restart, and what lets more than one
replica run: before this, they were ``asyncio`` timers in a dict, so a deploy mid-exercise
silently dropped every pending communication (#211).

A schedule says *when* an inject may release; the progression cursor still says *whether*
it may. Two mechanisms compose to keep a schedule from silently evaporating when the room
runs slow (#218):

* **the clock** — ``schedule_exercise_injects`` enqueues *every* scheduled inject on start
  and resume, whether or not a cursor has reached it yet.
* **the gate opened** — ``arm_cursor_reached_injects`` enqueues a fresh job the moment a
  response advances a cursor onto an inject's node, in that response's own transaction. A
  job that ran too early simply found the gate shut and stopped; this is what brings it
  back, at delay 0 if the offset has already elapsed, so an overdue inject releases at
  once rather than never.

There is deliberately no cancellation. Pausing, completing, or releasing early leaves the
job in place; it fires, re-reads durable state, and no-ops (or re-defers itself if a pause
moved its deadline). Duplicate and stale jobs are therefore expected and cheap — the CAS
release and the ``(exercise_id, trigger_key)`` unique insert already make them no-ops.
Losing a job is the only outcome that would actually hurt, and the transactional enqueue
is what rules that out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import Communication
from app.models.exercise import (
    Exercise,
    ExerciseProgress,
    ExerciseState,
    ExerciseStateTransition,
)
from app.models.inject import Inject, InjectState
from app.services import task_queue

logger = logging.getLogger(__name__)


# ── The pause-aware clock ─────────────────────────────────────────────────────


def _effective_elapsed_seconds(exercise: Exercise) -> float:
    """Seconds of *running* time since start, excluding completed pause spans (#116).

    Called only for active exercises (paused_at is None post-resume), so the current
    wall-clock gap is all running time; prior pauses are already in the accumulator.
    """
    if exercise.started_at is None:
        return 0.0
    now = task_queue.now()
    return (now - exercise.started_at).total_seconds() - exercise.accumulated_pause_seconds


def seconds_until_due(exercise: Exercise, inject: Inject) -> float:
    """How much running time is left before this inject's offset comes up."""
    if inject.release_offset_minutes is None:
        return 0.0
    return inject.release_offset_minutes * 60 - _effective_elapsed_seconds(exercise)


def _active_elapsed_since(
    exercise: Exercise,
    released_at: datetime,
    transitions: list[ExerciseStateTransition],
) -> float:
    """Running seconds since release, excluding every persisted pause span."""
    now = task_queue.now()
    end = min(exercise.ended_at or now, now)
    paused_seconds = 0.0
    pause_started: datetime | None = None
    for transition in sorted(transitions, key=lambda item: item.transitioned_at):
        if transition.transitioned_at < released_at:
            continue
        if transition.to_state == ExerciseState.paused:
            pause_started = transition.transitioned_at
        elif transition.from_state == ExerciseState.paused and pause_started is not None:
            paused_seconds += (transition.transitioned_at - pause_started).total_seconds()
            pause_started = None
    if pause_started is not None:
        paused_seconds += (end - pause_started).total_seconds()
    return max(0.0, (end - released_at).total_seconds() - paused_seconds)


async def active_seconds_since(
    session: AsyncSession, exercise: Exercise, released_at: datetime
) -> float:
    """``_active_elapsed_since`` with the transitions read for you."""
    transitions = list(
        (
            await session.exec(
                select(ExerciseStateTransition).where(
                    ExerciseStateTransition.exercise_id == exercise.id
                )
            )
        ).all()
    )
    return _active_elapsed_since(exercise, released_at, transitions)


# ── Enqueueing ────────────────────────────────────────────────────────────────


async def defer_inject_release(
    session: AsyncSession, exercise: Exercise, inject: Inject, *, delay: float | None = None
) -> None:
    """Enqueue one inject's release, on the caller's transaction."""
    if exercise.id is None or inject.id is None:
        return
    wait = seconds_until_due(exercise, inject) if delay is None else delay
    await task_queue.defer_job(
        session,
        task_queue.TASK_RELEASE_INJECT,
        args={"exercise_id": exercise.id, "inject_id": inject.id},
        scheduled_at=task_queue.now() + timedelta(seconds=max(0.0, wait)),
    )


async def defer_triggered_comm(
    session: AsyncSession,
    *,
    exercise_id: int,
    inject_id: int,
    node_id: str,
    trigger_index: int,
    delay: float,
) -> None:
    """Enqueue one scenario-triggered communication, on the caller's transaction."""
    await task_queue.defer_job(
        session,
        task_queue.TASK_DELIVER_COMM,
        args={
            "exercise_id": exercise_id,
            "inject_id": inject_id,
            "node_id": node_id,
            "trigger_index": trigger_index,
        },
        scheduled_at=task_queue.now() + timedelta(seconds=max(0.0, delay)),
    )


async def arm_inject_schedule(
    session: AsyncSession, exercise: Exercise, inject: Inject
) -> None:
    """(Re)enqueue a single inject's release — used by runtime schedule edits."""
    if (
        exercise.state != ExerciseState.active
        or exercise.started_at is None
        or inject.state != InjectState.pending
        or inject.release_offset_minutes is None
        or inject.id is None
    ):
        return
    await defer_inject_release(session, exercise, inject)


async def schedule_exercise_injects(session: AsyncSession, exercise: Exercise) -> None:
    """Enqueue every pending scheduled inject and undelivered comm — start and resume.

    Resume is why this re-enqueues unconditionally: jobs written before the pause are
    still in the table, pointing at deadlines computed against the old clock. Rather
    than hunt them down, this adds correct ones and lets the stale ones fire and
    re-defer or no-op.
    """
    if exercise.state != ExerciseState.active or exercise.started_at is None or exercise.id is None:
        return
    injects = (
        await session.exec(
            select(Inject).where(
                Inject.exercise_id == exercise.id,
                Inject.state == InjectState.pending,
                col(Inject.release_offset_minutes).is_not(None),
            )
        )
    ).all()
    for inject in injects:
        await defer_inject_release(session, exercise, inject)
    await schedule_exercise_communications(session, exercise)


async def schedule_exercise_communications(session: AsyncSession, exercise: Exercise) -> None:
    """Enqueue the triggered comms that durable state says are still owed.

    Derived entirely from rows — released injects, the scenario definition, and the
    ``trigger_key``s already persisted — so it is correct whether it runs on resume, on
    reconciliation, or after a restart that lost nothing but memory.
    """
    if exercise.state != ExerciseState.active or exercise.id is None:
        return
    from app.services.scenario_service import definition_for_exercise

    definition = await definition_for_exercise(session, exercise.id)
    if definition is None:
        return
    nodes = {node.id: node for node in definition.injects}
    injects = (
        await session.exec(
            select(Inject)
            .where(
                Inject.exercise_id == exercise.id,
                col(Inject.released_at).is_not(None),
                col(Inject.scenario_node_id).is_not(None),
            )
            .order_by(col(Inject.released_at), col(Inject.id))
        )
    ).all()
    delivered = {
        key
        for key in (
            await session.exec(
                select(Communication.trigger_key).where(
                    Communication.exercise_id == exercise.id,
                    col(Communication.trigger_key).is_not(None),
                )
            )
        ).all()
        if key is not None
    }
    transitions = list(
        (
            await session.exec(
                select(ExerciseStateTransition).where(
                    ExerciseStateTransition.exercise_id == exercise.id
                )
            )
        ).all()
    )
    considered = set(delivered)
    for inject in injects:
        assert inject.id is not None and inject.released_at is not None
        node = nodes.get(inject.scenario_node_id or "")
        if node is None:
            continue
        elapsed = _active_elapsed_since(exercise, inject.released_at, transitions)
        for index, trigger in enumerate(node.triggers_communications):
            trigger_key = f"{node.id}:{index}"
            if trigger_key in considered:
                continue
            considered.add(trigger_key)
            await defer_triggered_comm(
                session,
                exercise_id=exercise.id,
                inject_id=inject.id,
                node_id=node.id,
                trigger_index=index,
                delay=trigger.delay_after_release_seconds - elapsed,
            )


async def schedule_release_triggered_comms(
    session: AsyncSession, exercise_id: int, inject: Inject
) -> None:
    """Enqueue the comms a just-released inject triggers, in the release's transaction.

    This used to be a post-commit subscriber, which meant a release could commit and the
    process die before its comms were armed — #211 exactly. Enqueued here it is part of
    the same unit of work as the release itself.
    """
    from app.services.scenario_service import definition_for_exercise, get_inject_node

    if not inject.scenario_node_id or inject.id is None:
        return
    definition = await definition_for_exercise(session, exercise_id)
    if definition is None:
        return
    node = get_inject_node(definition, inject.scenario_node_id)
    if node is None:
        return
    for index, trigger in enumerate(node.triggers_communications):
        await defer_triggered_comm(
            session,
            exercise_id=exercise_id,
            inject_id=inject.id,
            node_id=node.id,
            trigger_index=index,
            delay=trigger.delay_after_release_seconds,
        )


async def arm_cursor_reached_injects(session: AsyncSession, exercise: Exercise) -> None:
    """Enqueue scheduled injects a progression cursor now points at (#218).

    A job that came due before the team reached its node found the gate shut and
    stopped. This brings it back the moment a response advances a cursor onto that node
    — and because the enqueue rides the response's own transaction, the window the old
    in-memory version had to coordinate around (worker deciding to stand down while a
    response commits) cannot exist: either the response committed and the job is there,
    or neither happened.

    Matches on ``current_node_id`` because that is what ``release_is_allowed`` matches on;
    the two are the same predicate seen from either end and have to stay in lockstep.
    ``current_inject_id`` would be wrong — it points at the inject just *resolved*.
    """
    if exercise.state != ExerciseState.active or exercise.started_at is None or exercise.id is None:
        return
    cursors = (
        await session.exec(
            select(ExerciseProgress).where(ExerciseProgress.exercise_id == exercise.id)
        )
    ).all()
    node_ids = {cursor.current_node_id for cursor in cursors if cursor.current_node_id}
    if not node_ids:
        return
    # No group filter: a branch may hand off between teams, so release_is_allowed honours
    # *any* cursor sitting on the node. Filtering here would under-arm that handoff. One
    # node can also seed a physical inject per target team — both need arming.
    injects = (
        await session.exec(
            select(Inject).where(
                Inject.exercise_id == exercise.id,
                Inject.state == InjectState.pending,
                col(Inject.release_offset_minutes).is_not(None),
                col(Inject.scenario_node_id).in_(node_ids),
            )
        )
    ).all()
    for inject in injects:
        await defer_inject_release(session, exercise, inject)


async def reconcile_release_jobs() -> None:
    """Re-enqueue schedules for active exercises on startup.

    Jobs are durable, so unlike the rehydration this replaces, it is not load-bearing —
    it is a safety net, and the cutover path for exercises that were already running
    when this version was deployed and whose schedules only ever lived in the previous
    process's memory.
    """
    async with AsyncSession(engine_for_session(), expire_on_commit=False) as session:
        exercises = (
            await session.exec(select(Exercise).where(Exercise.state == ExerciseState.active))
        ).all()
        for exercise in exercises:
            await schedule_exercise_injects(session, exercise)
        await session.commit()


def engine_for_session():
    """Resolve the engine at call time — the test suite rebinds it after import."""
    from app.database import engine

    return engine
