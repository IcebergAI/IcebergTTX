"""The durable task queue's primitives (#213).

The behavioural tests for what the tasks *do* live in test_pacing.py; these pin the
enqueue mechanism itself, including the two things that would otherwise only be
discovered in production: that a job is written inside the caller's transaction, and
that the migration's way of executing a multi-statement script actually works.
"""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.services import task_queue


async def _jobs(session: AsyncSession, task_name: str) -> list[dict]:
    connection = await session.connection()
    rows = (
        await connection.execute(
            text(
                "SELECT task_name, queue_name, priority, lock, queueing_lock, args, "
                "scheduled_at, status FROM procrastinate_jobs WHERE task_name = :task "
                "ORDER BY id"
            ),
            {"task": task_name},
        )
    ).mappings().all()
    return [
        {**row, "args": json.loads(row["args"]) if isinstance(row["args"], str) else row["args"]}
        for row in rows
    ]


async def test_defer_job_writes_the_row_the_worker_will_pick_up(session: AsyncSession):
    when = task_queue.now() + timedelta(minutes=5)

    await task_queue.defer_job(
        session,
        task_queue.TASK_RELEASE_INJECT,
        args={"exercise_id": 1, "inject_id": 2},
        scheduled_at=when,
    )

    jobs = await _jobs(session, task_queue.TASK_RELEASE_INJECT)
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"exercise_id": 1, "inject_id": 2}
    assert jobs[0]["status"] == "todo"
    assert jobs[0]["queue_name"] == task_queue.QUEUE_DEFAULT
    assert abs((jobs[0]["scheduled_at"] - when).total_seconds()) < 1


async def test_an_enqueue_is_visible_only_inside_its_transaction(session: AsyncSession):
    """The property everything else rests on.

    ``defer_job`` runs on the caller's connection, so the row lives and dies with the
    caller's transaction. A queue in another store could not do this, and that is the
    whole argument for keeping the queue in Postgres.
    """
    await session.begin_nested()
    await task_queue.defer_job(session, task_queue.TASK_GENERATE_SUMMARY, args={"exercise_id": 9})
    assert len(await _jobs(session, task_queue.TASK_GENERATE_SUMMARY)) == 1

    await session.rollback()

    assert await _jobs(session, task_queue.TASK_GENERATE_SUMMARY) == []


async def test_defer_job_once_lets_the_first_caller_through(session: AsyncSession):
    assert (
        await task_queue.defer_job_once(
            session, task_queue.TASK_GENERATE_SUMMARY, queueing_lock="summary:5",
            args={"exercise_id": 5},
        )
        is True
    )
    assert (
        await task_queue.defer_job_once(
            session, task_queue.TASK_GENERATE_SUMMARY, queueing_lock="summary:5",
            args={"exercise_id": 5},
        )
        is False
    )

    assert len(await _jobs(session, task_queue.TASK_GENERATE_SUMMARY)) == 1


async def test_a_rejected_enqueue_leaves_the_transaction_usable(session: AsyncSession):
    """Without the savepoint inside ``defer_job_once`` the unique violation would abort
    the caller's whole transaction — a facilitator's double-click would 500 the second
    request instead of answering "already queued"."""
    await task_queue.defer_job_once(
        session, task_queue.TASK_ASSESS_RESPONSE, queueing_lock="assess:1", args={}
    )
    await task_queue.defer_job_once(
        session, task_queue.TASK_ASSESS_RESPONSE, queueing_lock="assess:1", args={}
    )

    await task_queue.defer_job(session, task_queue.TASK_RELEASE_INJECT, args={"x": 1})
    assert len(await _jobs(session, task_queue.TASK_RELEASE_INJECT)) == 1


async def test_distinct_locks_do_not_block_each_other(session: AsyncSession):
    assert await task_queue.defer_job_once(
        session, task_queue.TASK_ASSESS_RESPONSE, queueing_lock="assess:1", args={}
    )
    assert await task_queue.defer_job_once(
        session, task_queue.TASK_ASSESS_RESPONSE, queueing_lock="assess:2", args={}
    )


async def test_every_task_name_is_registered_with_the_app():
    """A name declared but never registered enqueues jobs no worker can run — they sit
    in the table as ``todo`` forever, which looks exactly like a silent hang."""
    registered = set(task_queue.queue_app.tasks)
    for name in (
        task_queue.TASK_RELEASE_INJECT,
        task_queue.TASK_DELIVER_COMM,
        task_queue.TASK_ASSESS_RESPONSE,
        task_queue.TASK_GENERATE_SUMMARY,
    ):
        assert name in registered


@pytest.mark.parametrize(
    "cron_task",
    [
        "maintenance.retention_sweep",
        "maintenance.remove_old_jobs",
        "maintenance.retry_stalled_jobs",
    ],
)
def test_maintenance_tasks_are_periodic(cron_task):
    """These replace per-process loops, so a missing schedule means the work silently
    stops happening rather than happening twice."""
    registered = task_queue.queue_app.periodic_registry.periodic_tasks
    assert any(name == cron_task for name, _periodic_id in registered)


async def test_a_multi_statement_script_runs_on_the_migration_path():
    """Guards the mechanism the procrastinate schema migration depends on.

    SQLAlchemy's asyncpg adapter puts every statement through a prepared statement, and
    Postgres will not prepare more than one command — so the migration reaches the raw
    asyncpg connection instead. If that stops working, migrations break on a fresh
    database and nowhere else, which is the worst place to find out.
    """
    script = """
        CREATE TEMP TABLE multi_stmt_probe (id integer);
        INSERT INTO multi_stmt_probe VALUES (1);
        INSERT INTO multi_stmt_probe VALUES (2);
    """
    async with engine.connect() as connection:
        raw = (await connection.get_raw_connection()).driver_connection
        await raw.execute(script)
        count = await raw.fetchval("SELECT count(*) FROM multi_stmt_probe")

    assert count == 2


async def test_the_worker_picks_up_and_runs_a_committed_job():
    """The whole delivery loop against a real database: open the pool, run the worker,
    commit a job, and watch it execute.

    Everything else in the suite invokes task functions directly, so without this a
    wiring mistake — the connector DSN, the NOTIFY wake-up, a task procrastinate cannot
    deserialise — would pass every test and hang silently on the first real deploy.

    The job targets ids that do not exist, so the task itself is a guarded no-op; what
    is under test is that the worker ran it at all.
    """
    from sqlmodel.ext.asyncio.session import AsyncSession

    await task_queue.start_worker()
    try:
        async with AsyncSession(engine) as session:
            await task_queue.defer_job(
                session,
                task_queue.TASK_RELEASE_INJECT,
                args={"exercise_id": 0, "inject_id": 0},
            )
            await session.commit()

        status = None
        for _ in range(100):
            async with engine.connect() as connection:
                status = (
                    await connection.execute(
                        text(
                            "SELECT status FROM procrastinate_jobs "
                            "WHERE task_name = :task AND args->>'inject_id' = '0' "
                            "ORDER BY id DESC LIMIT 1"
                        ),
                        {"task": task_queue.TASK_RELEASE_INJECT},
                    )
                ).scalar_one_or_none()
            if status == "succeeded":
                break
            await asyncio.sleep(0.1)

        assert status == "succeeded"
    finally:
        await task_queue.stop_worker()
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM procrastinate_jobs WHERE args->>'inject_id' = '0'")
            )
