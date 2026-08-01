"""The durable task queue (#213).

Everything this app schedules for later — an inject's timed release, a scenario's
triggered communications, an LLM pipeline, the nightly audit purge — used to be an
``asyncio`` task holding its state in a dict. That is why a restart mid-exercise lost
pending work (#211) and why the app could not run more than one replica.

Jobs now live in Postgres, via procrastinate. The reason it is procrastinate and not
Celery or ARQ is **transactional enqueue**: the job row is INSERTed by
``procrastinate_defer_jobs_v1`` on *the caller's own session*, inside the caller's
transaction, and procrastinate's insert trigger raises its ``pg_notify`` — which
Postgres holds until COMMIT. So the job exists if and only if the state change that
warranted it committed. With a Redis-backed queue that is two stores and no shared
transaction, and "committed but never enqueued" comes back as permanent architecture.

Two consequences shape every task below:

* **Guards, not cancellation.** Nothing cancels a job when an exercise pauses or an
  inject is released early. Each task re-derives from durable state whether its work
  still applies and no-ops if not, which the existing CAS release and the
  ``(exercise_id, trigger_key)`` unique insert already make safe. A duplicate or stale
  job is therefore cheap and correct, and losing one is the only real failure — which
  is the trade the transactional enqueue is designed around.

* **Re-defer, don't sleep.** A task that finds itself early (a pause moved its deadline
  out) re-defers itself for the remaining time rather than sleeping. A worker holding a
  connection for two hours is exactly the model this replaces.

The worker runs in-process on every replica, so a deployment is still one container.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from procrastinate import App, PsycopgConnector
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)


def _dsn() -> str:
    from app.config import settings
    from app.database import make_asyncpg_dsn

    return make_asyncpg_dsn(settings.database_url)


queue_app = App(connector=PsycopgConnector(conninfo=_dsn()))
"""Task registry and worker host. Constructing it opens nothing — the pool is lazy."""


# Task names are declared, never derived, so a rename cannot orphan the jobs a
# previous release already wrote into the table.
TASK_RELEASE_INJECT = "inject.release_scheduled"
TASK_DELIVER_COMM = "comm.deliver_triggered"
TASK_ASSESS_RESPONSE = "llm.assess_response"
TASK_GENERATE_SUMMARY = "llm.generate_summary"

QUEUE_DEFAULT = "default"

# How early a task may run and still count as due. Postgres timestamps and the worker's
# poll interval do not align perfectly, and re-deferring by a fraction of a second would
# just burn a round trip.
DUE_EPSILON_SECONDS = 1.0


# ── Enqueueing ────────────────────────────────────────────────────────────────

_DEFER_SQL = text(
    """
    SELECT procrastinate_defer_jobs_v1(
        ARRAY[
            ROW(
                CAST(:queue_name AS character varying),
                CAST(:task_name AS character varying),
                CAST(:priority AS integer),
                CAST(:lock AS text),
                CAST(:queueing_lock AS text),
                CAST(:args AS jsonb),
                CAST(:scheduled_at AS timestamptz)
            )
        ]::procrastinate_job_to_defer_v1[]
    )
    """
)


async def defer_job(
    session: AsyncSession,
    task_name: str,
    *,
    args: dict[str, Any] | None = None,
    scheduled_at: datetime | None = None,
    queue: str = QUEUE_DEFAULT,
    priority: int = 0,
    lock: str | None = None,
    queueing_lock: str | None = None,
) -> None:
    """Enqueue a job on the caller's transaction.

    Nothing is dispatched here. The row commits with everything else in the unit of
    work, and the worker is woken by a NOTIFY that Postgres releases at the same moment.
    Call this *before* the commit that makes the job's premise true.
    """
    connection = await session.connection()
    await connection.execute(
        _DEFER_SQL,
        {
            "queue_name": queue,
            "task_name": task_name,
            "priority": priority,
            "lock": lock,
            "queueing_lock": queueing_lock,
            "args": json.dumps(args or {}),
            # A datetime, not an ISO string: the CAST tells asyncpg to expect timestamptz
            # and it refuses to coerce text into one.
            "scheduled_at": scheduled_at,
        },
    )


async def defer_job_once(
    session: AsyncSession, task_name: str, *, queueing_lock: str, **kwargs: Any
) -> bool:
    """Enqueue unless an identical job is already waiting. True if this call created it.

    The cross-replica replacement for an in-process "is it already running?" set: the
    queueing lock is a partial unique index over jobs still to do, so the database
    arbitrates rather than one process's memory. The savepoint is what keeps a losing
    race from poisoning the caller's transaction — a raw unique violation would abort
    everything the request had done so far.
    """
    try:
        async with session.begin_nested():
            await defer_job(session, task_name, queueing_lock=queueing_lock, **kwargs)
    except IntegrityError:
        return False
    return True


# ── Tasks ─────────────────────────────────────────────────────────────────────


@queue_app.task(name=TASK_RELEASE_INJECT)
async def release_scheduled_inject(*, exercise_id: int, inject_id: int) -> None:
    """Release an inject whose offset has come up.

    Every precondition is re-read here rather than trusted from enqueue time, because
    the job may be a duplicate, may predate a pause, or may have been written by a
    replica that has since died.
    """
    from app.models.exercise import Exercise, ExerciseState
    from app.models.inject import Inject, InjectState
    from app.services import audit_service, schedule_service
    from app.services.inject_service import release_inject
    from app.services.progression_service import release_is_allowed

    async with AsyncSession(engine_for_session(), expire_on_commit=False) as session:
        inject = await session.get(Inject, inject_id)
        exercise = await session.get(Exercise, exercise_id)
        if inject is None or exercise is None:
            return
        if (
            inject.state != InjectState.pending
            or exercise.state != ExerciseState.active
            or inject.release_offset_minutes is None
        ):
            # Released early, cancelled, paused, or finished. A paused exercise re-defers
            # everything on resume, so there is nothing to reschedule here.
            return

        remaining = schedule_service.seconds_until_due(exercise, inject)
        if remaining > DUE_EPSILON_SECONDS:
            # A pause pushed the deadline out after this job was written. Come back then.
            await schedule_service.defer_inject_release(session, exercise, inject)
            await session.commit()
            return

        if not await release_is_allowed(session, inject, scheduled=True):
            # Not a failure: the team simply has not reached this node. The response that
            # advances a cursor onto it enqueues a fresh job in its own transaction, so
            # unlike the old in-memory timer there is no race to lose here (#218).
            logger.info(
                "Scheduled release deferred for inject %d: no cursor has reached it", inject_id
            )
            audit_service.emit(
                "inject.release_deferred",
                actor=None,
                target_type="inject",
                target_id=inject_id,
                reason="cursor_not_reached",
            )
            return

        await release_inject(session, inject, released_by=None, scheduled=True)
        audit_service.emit(
            "inject.release",
            actor=None,
            target_type="inject",
            target_id=inject_id,
            reason="scheduled",
        )


@queue_app.task(name=TASK_DELIVER_COMM)
async def deliver_triggered_comm(
    *, exercise_id: int, inject_id: int, node_id: str, trigger_index: int
) -> None:
    """Deliver one scenario-triggered communication.

    The message content is re-read from the scenario rather than carried in the job
    args: a job written before a scenario edit would otherwise deliver text the
    facilitator has since changed.
    """
    from app.models.exercise import Exercise, ExerciseState
    from app.models.inject import Inject
    from app.services import schedule_service
    from app.services.communication_service import deliver_triggered_communication
    from app.services.scenario_service import definition_for_exercise, get_inject_node

    async with AsyncSession(engine_for_session(), expire_on_commit=False) as session:
        exercise = await session.get(Exercise, exercise_id)
        inject = await session.get(Inject, inject_id)
        if exercise is None or inject is None or exercise.state != ExerciseState.active:
            return
        if inject.released_at is None:
            return
        definition = await definition_for_exercise(session, exercise_id)
        if definition is None:
            return
        node = get_inject_node(definition, node_id)
        if node is None or trigger_index >= len(node.triggers_communications):
            return
        trigger = node.triggers_communications[trigger_index]

        elapsed = await schedule_service.active_seconds_since(
            session, exercise, inject.released_at
        )
        remaining = trigger.delay_after_release_seconds - elapsed
        if remaining > DUE_EPSILON_SECONDS:
            await schedule_service.defer_triggered_comm(
                session,
                exercise_id=exercise_id,
                inject_id=inject_id,
                node_id=node_id,
                trigger_index=trigger_index,
                delay=remaining,
            )
            await session.commit()
            return

        await deliver_triggered_communication(
            session,
            exercise_id=exercise_id,
            inject_id=inject_id,
            direction=trigger.direction,
            external_entity=trigger.external_entity,
            subject=trigger.subject,
            body=trigger.body,
            trigger_key=f"{node_id}:{trigger_index}",
        )


@queue_app.task(name=TASK_ASSESS_RESPONSE)
async def assess_response(*, response_id: int, inject_id: int, exercise_id: int) -> None:
    from app.services.llm_service import run_llm_pipeline

    await run_llm_pipeline(response_id, inject_id, exercise_id)


@queue_app.task(name=TASK_GENERATE_SUMMARY)
async def generate_summary(*, exercise_id: int) -> None:
    from app.services.llm_service import run_summary_pipeline

    await run_summary_pipeline(exercise_id)


# ── Periodic maintenance ──────────────────────────────────────────────────────
#
# Cross-replica single-fire is procrastinate's, not ours: a periodic defer is unique on
# (task, periodic_id, timestamp), so whichever replica gets there first wins and the
# rest lose the insert. That is what makes it safe to run a worker in every replica —
# before this, a second replica meant a second concurrent audit purge.


@queue_app.periodic(cron="17 3 * * *")
@queue_app.task(name="maintenance.retention_sweep")
async def retention_sweep(timestamp: int) -> None:
    from app.services.retention_service import sweep_once

    await sweep_once()


@queue_app.periodic(cron="43 4 * * *")
@queue_app.task(name="maintenance.remove_old_jobs")
async def remove_old_jobs(timestamp: int) -> None:
    """Keep the job table from growing without bound.

    Failed jobs are kept: they are the record of work this app promised and did not do,
    which is worth more than the rows cost.
    """
    await queue_app.job_manager.delete_old_jobs(nb_hours=24 * 7)


@queue_app.periodic(cron="29 * * * *")
@queue_app.task(name="maintenance.rate_limit_sweep")
async def rate_limit_sweep(timestamp: int) -> None:
    """Delete throttle counters past their window.

    Hourly and centralised, because the keys are attacker-controlled: the previous
    in-process version swept on a read path, so rows for a key nobody looked at again
    stayed forever.
    """
    from app.services.rate_limit import sweep

    removed = await sweep()
    if removed:
        logger.info("swept %d expired rate-limit hits", removed)


@queue_app.periodic(cron="*/10 * * * *")
@queue_app.task(name="maintenance.retry_stalled_jobs")
async def retry_stalled_jobs(timestamp: int) -> None:
    """Return jobs orphaned by a killed worker to the queue.

    A replica SIGKILLed mid-job (an OOM, a node eviction) leaves its job marked *doing*
    with nobody working it. Retrying is safe precisely because every task above is a
    guarded, idempotent no-op when its work has already happened.
    """
    stalled = await queue_app.job_manager.get_stalled_jobs(seconds_since_heartbeat=120)
    for job in stalled:
        logger.warning("retrying stalled job %s (%s)", job.id, job.task_name)
        await queue_app.job_manager.retry_job(job)


# ── Wiring ────────────────────────────────────────────────────────────────────


def engine_for_session() -> Any:
    """Resolve the engine at call time — the test suite rebinds it after import."""
    from app.database import engine

    return engine


def schema_sql() -> str:
    """procrastinate's own DDL, for the Alembic revision that installs it."""
    from procrastinate.schema import SchemaManager

    return SchemaManager.get_schema()


def apply_schema(dsn: str | None = None) -> None:
    """Install the schema over a synchronous connection.

    Used by the Alembic revision and by the test harness, which builds its schema from
    the models and so never runs Alembic. Synchronous because procrastinate's DDL is a
    multi-statement script, which asyncpg's prepared-statement path cannot execute.
    """
    from procrastinate import SyncPsycopgConnector
    from procrastinate.schema import SchemaManager

    connector = SyncPsycopgConnector(conninfo=dsn or _dsn())
    connector.open()
    try:
        SchemaManager(connector=connector).apply_schema()
    finally:
        connector.close()


_worker_task: asyncio.Task | None = None


async def start_worker() -> None:
    """Open the pool and run a worker for this replica."""
    global _worker_task
    if _worker_task is not None:
        return
    await queue_app.open_async()
    _worker_task = asyncio.create_task(
        queue_app.run_worker_async(
            install_signal_handlers=False,  # the lifespan owns shutdown
            listen_notify=True,
            concurrency=4,
        )
    )
    logger.info("task queue worker started")


async def stop_worker(timeout: float = 10.0) -> None:
    """Stop the worker, giving running jobs a bounded chance to finish.

    A job killed here is not lost — it stays *doing* until ``retry_stalled_jobs`` picks
    it up, and every task is safe to run twice.
    """
    global _worker_task
    task, _worker_task = _worker_task, None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=timeout)
    with contextlib.suppress(Exception):
        await queue_app.close_async()


def now() -> datetime:
    """Indirection so tests can freeze the clock the schedulers compute against."""
    return datetime.now(UTC)
