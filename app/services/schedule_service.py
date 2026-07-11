"""Scheduled inject release (#116).

An inject may carry a ``release_offset_minutes`` — minutes after the exercise's
effective start at which it auto-releases. This mirrors the ``triggers_communications``
pattern (``communication_service._delayed_comm`` fired via ``asyncio.create_task`` +
``asyncio.sleep``) but adds what that pattern lacks: a **keyed, cancellable registry**,
so a pending timer can be deferred on pause, cancelled outright, or superseded when the
offset is edited at runtime.

Single-process only, exactly like triggered comms: the registry is in-memory, so a
multi-process deployment would need a task queue (Celery/ARQ) — see the single-replica
note in CLAUDE.md. Startup rehydration (``app/main.py``) re-arms schedules for active
exercises after a single-process restart; it does not survive across replicas.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseState
from app.models.inject import Inject, InjectState
from app.services import audit_service

logger = logging.getLogger(__name__)

# exercise_id -> {inject_id -> pending release task}. Holds a strong reference so the
# task isn't GC'd (cf. background.spawn) *and* lets us cancel a specific timer.
_scheduled: dict[int, dict[int, asyncio.Task]] = {}


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
    """Cancel every pending release for an exercise (pause defers, complete drops)."""
    ex_tasks = _scheduled.pop(exercise_id, None)
    if not ex_tasks:
        return
    current = asyncio.current_task()
    for task in ex_tasks.values():
        if task is not current:
            task.cancel()


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
    """Arm timers for every pending, scheduled inject — called on start and resume."""
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


async def rehydrate_schedules() -> None:
    """Re-arm scheduled releases for every active exercise on startup (#116).

    In-memory timers don't survive a process restart (cf. background.py). This re-derives
    them from persisted state so a single-process restart mid-exercise doesn't silently
    drop pending releases. Multi-process deployments still need a task queue.
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
