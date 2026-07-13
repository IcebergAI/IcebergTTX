"""Pause-aware, restart-safe exercise scheduling (#116, #194).

An inject may carry a ``release_offset_minutes`` — minutes after the exercise's
effective start at which it auto-releases. The registry also covers scenario
``triggers_communications``, so every
pending timer is deferred on pause, cancelled on completion, and rehydrated after restart.

Single-process only: the registry is in-memory, so a
multi-process deployment would need a task queue (Celery/ARQ) — see the single-replica
note in CLAUDE.md. Startup rehydration (``app/main.py``) re-arms schedules for active
exercises after a single-process restart; it does not survive across replicas.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import Communication
from app.models.exercise import Exercise, ExerciseState, ExerciseStateTransition
from app.models.inject import Inject, InjectState
from app.services import audit_service

logger = logging.getLogger(__name__)

# exercise_id -> {inject_id -> pending release task}. Holds a strong reference so the
# task isn't GC'd (cf. background.spawn) *and* lets us cancel a specific timer.
_scheduled: dict[int, dict[int, asyncio.Task]] = {}
_scheduled_comms: dict[int, dict[str, asyncio.Task]] = {}


def _effective_elapsed_seconds(exercise: Exercise) -> float:
    """Seconds of *running* time since start, excluding completed pause spans (#116).

    Called only for active exercises (paused_at is None post-resume), so the current
    wall-clock gap is all running time; prior pauses are already in the accumulator.
    """
    if exercise.started_at is None:
        return 0.0
    now = datetime.now(UTC)
    return (now - exercise.started_at).total_seconds() - exercise.accumulated_pause_seconds


def _forget(exercise_id: int, inject_id: int, task: asyncio.Task) -> None:
    ex_tasks = _scheduled.get(exercise_id)
    # Only drop if this is still the registered task — a re-arm may have replaced it.
    if ex_tasks and ex_tasks.get(inject_id) is task:
        ex_tasks.pop(inject_id, None)
        if not ex_tasks:
            _scheduled.pop(exercise_id, None)


def _arm(exercise_id: int, inject_id: int, delay: float) -> None:
    task = asyncio.ensure_future(_release_when_due(exercise_id, inject_id, delay))
    _scheduled.setdefault(exercise_id, {})[inject_id] = task
    task.add_done_callback(lambda t: _forget(exercise_id, inject_id, t))


def _forget_comm(exercise_id: int, trigger_key: str, task: asyncio.Task) -> None:
    exercise_tasks = _scheduled_comms.get(exercise_id)
    if exercise_tasks and exercise_tasks.get(trigger_key) is task:
        exercise_tasks.pop(trigger_key, None)
        if not exercise_tasks:
            _scheduled_comms.pop(exercise_id, None)


def arm_triggered_communication(
    *,
    exercise_id: int,
    inject_id: int,
    direction: str,
    external_entity: str,
    subject: str,
    body: str,
    delay: float,
    trigger_key: str,
) -> None:
    """Arm one logical communication unless that key is already pending."""
    if trigger_key in _scheduled_comms.get(exercise_id, {}):
        return
    task = asyncio.ensure_future(
        _communication_when_due(
            exercise_id=exercise_id,
            inject_id=inject_id,
            direction=direction,
            external_entity=external_entity,
            subject=subject,
            body=body,
            delay=max(0.0, delay),
            trigger_key=trigger_key,
        )
    )
    _scheduled_comms.setdefault(exercise_id, {})[trigger_key] = task
    task.add_done_callback(lambda done: _forget_comm(exercise_id, trigger_key, done))


def cancel_inject_schedule(exercise_id: int, inject_id: int | None) -> None:
    """Cancel one inject's pending release timer (release-early, cancel, or re-arm)."""
    if inject_id is None:
        return
    ex_tasks = _scheduled.get(exercise_id)
    if not ex_tasks:
        return
    task = ex_tasks.pop(inject_id, None)
    if not ex_tasks:
        _scheduled.pop(exercise_id, None)
    # Never cancel the worker that is itself calling this (via release_inject on fire).
    if task is not None and task is not asyncio.current_task():
        task.cancel()


def cancel_exercise_schedules(exercise_id: int) -> None:
    """Cancel every pending exercise timer (pause defers, completion drops)."""
    ex_tasks = _scheduled.pop(exercise_id, None) or {}
    comm_tasks = _scheduled_comms.pop(exercise_id, None) or {}
    current = asyncio.current_task()
    for task in (*ex_tasks.values(), *comm_tasks.values()):
        if task is not current:
            task.cancel()


def _active_elapsed_since(
    exercise: Exercise,
    released_at: datetime,
    transitions: list[ExerciseStateTransition],
) -> float:
    """Running seconds since release, excluding every persisted pause span."""
    now = datetime.now(UTC)
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


async def _schedule_exercise_communications(
    session: AsyncSession, exercise: Exercise
) -> None:
    """Reconstruct undelivered logical communications from durable scenario state."""
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
            arm_triggered_communication(
                exercise_id=exercise.id,
                inject_id=inject.id,
                direction=trigger.direction,
                external_entity=trigger.external_entity,
                subject=trigger.subject,
                body=trigger.body,
                delay=trigger.delay_after_release_seconds - elapsed,
                trigger_key=trigger_key,
            )


def arm_inject_schedule(exercise: Exercise, inject: Inject) -> None:
    """(Re)arm a single inject's timer against a running exercise — used by runtime edits."""
    if (
        exercise.state != ExerciseState.active
        or exercise.started_at is None
        or inject.state != InjectState.pending
        or inject.release_offset_minutes is None
        or inject.id is None
    ):
        return
    assert exercise.id is not None
    cancel_inject_schedule(exercise.id, inject.id)
    delay = inject.release_offset_minutes * 60 - _effective_elapsed_seconds(exercise)
    _arm(exercise.id, inject.id, max(0.0, delay))


async def schedule_exercise_injects(session: AsyncSession, exercise: Exercise) -> None:
    """Arm persisted inject and communication timers on start, resume, or restart."""
    if exercise.state != ExerciseState.active or exercise.started_at is None or exercise.id is None:
        return
    elapsed = _effective_elapsed_seconds(exercise)
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
        assert inject.id is not None and inject.release_offset_minutes is not None
        cancel_inject_schedule(exercise.id, inject.id)  # idempotent re-arm
        delay = inject.release_offset_minutes * 60 - elapsed
        _arm(exercise.id, inject.id, max(0.0, delay))
    await _schedule_exercise_communications(session, exercise)


async def rehydrate_schedules() -> None:
    """Re-arm pending exercise timers for every active exercise on startup.

    In-memory timers don't survive a process restart (cf. background.py). This re-derives
    them from persisted state so a single-process restart mid-exercise doesn't silently
    drop pending releases or communications. Multi-process deployments still need a
    task queue.
    """
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        exercises = (
            await session.exec(select(Exercise).where(Exercise.state == ExerciseState.active))
        ).all()
        for exercise in exercises:
            await schedule_exercise_injects(session, exercise)


async def _release_when_due(exercise_id: int, inject_id: int, delay: float) -> None:
    """Sleep, then release the inject through the normal path (WS + triggered comms)."""
    try:
        if delay > 0:
            await asyncio.sleep(delay)

        from app.database import engine
        from app.services.inject_service import release_inject

        async with AsyncSession(engine, expire_on_commit=False) as session:
            inject = await session.get(Inject, inject_id)
            exercise = await session.get(Exercise, exercise_id)
            # Guard the pause/cancel/manual-release race: only fire if still pending,
            # still active, and still scheduled.
            if (
                inject is None
                or exercise is None
                or inject.state != InjectState.pending
                or exercise.state != ExerciseState.active
                or inject.release_offset_minutes is None
            ):
                return
            await release_inject(session, inject, released_by=None)
            audit_service.emit(
                "inject.release",
                actor=None,
                target_type="inject",
                target_id=inject_id,
                reason="scheduled",
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Scheduled release failed for inject %d", inject_id)


async def _communication_when_due(
    *,
    exercise_id: int,
    inject_id: int,
    direction: str,
    external_entity: str,
    subject: str,
    body: str,
    delay: float,
    trigger_key: str,
) -> None:
    """Deliver through the durable idempotent insert only while exercise is active."""
    try:
        if delay > 0:
            await asyncio.sleep(delay)
        from app.database import engine
        from app.services.communication_service import deliver_triggered_communication

        async with AsyncSession(engine, expire_on_commit=False) as session:
            exercise = await session.get(Exercise, exercise_id)
            if exercise is None or exercise.state != ExerciseState.active:
                return
            await deliver_triggered_communication(
                session,
                exercise_id=exercise_id,
                inject_id=inject_id,
                direction=direction,
                external_entity=external_entity,
                subject=subject,
                body=body,
                trigger_key=trigger_key,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Scheduled communication failed for trigger %s", trigger_key)
