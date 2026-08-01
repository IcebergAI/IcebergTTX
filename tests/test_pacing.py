"""Pacing: exercise clock and durable exercise schedules (#116, #194, #218, #213).

Schedules are rows in procrastinate's job table now, not tasks in a dict, so these
assert on jobs. That is not merely a change of spelling: a job enqueued by the test's
own transaction is *visible to that transaction*, which is what lets the flagship tests
below assert the property the whole design turns on — the job exists if and only if the
work that warranted it committed.

The worker never runs here (no lifespan, no queue worker), so tasks are invoked
directly, exactly as the previous version invoked ``_release_when_due``.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from httpx_ws import aconnect_ws
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import CommDirection, Communication
from app.models.exercise import Exercise, ExerciseState, ExerciseStateTransition
from app.models.inject import Inject, InjectState
from app.models.user import User
from app.schemas.scenario_json import InjectNode, ScenarioDefinition, TriggerComm
from app.services import schedule_service, task_queue
from app.services.exercise_service import (
    create_exercise,
    enrol_member,
    transition_state,
)
from app.services.scenario_service import create_scenario

AUTH = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731


# ── Reading the queue ─────────────────────────────────────────────────────────


async def _jobs(session: AsyncSession, task_name: str | None = None) -> list[dict]:
    """Job rows this transaction can see."""
    connection = await session.connection()
    sql = "SELECT id, task_name, args, scheduled_at, status FROM procrastinate_jobs"
    params: dict = {}
    if task_name is not None:
        sql += " WHERE task_name = :task_name"
        params["task_name"] = task_name
    rows = (await connection.execute(text(sql + " ORDER BY id"), params)).mappings().all()
    return [
        {**row, "args": json.loads(row["args"]) if isinstance(row["args"], str) else row["args"]}
        for row in rows
    ]


async def _release_jobs(session: AsyncSession, inject_id: int | None = None) -> list[dict]:
    jobs = await _jobs(session, task_queue.TASK_RELEASE_INJECT)
    if inject_id is None:
        return jobs
    return [job for job in jobs if job["args"].get("inject_id") == inject_id]


async def _comm_jobs(session: AsyncSession, node_id: str | None = None) -> list[dict]:
    jobs = await _jobs(session, task_queue.TASK_DELIVER_COMM)
    if node_id is None:
        return jobs
    return [job for job in jobs if job["args"].get("node_id") == node_id]


class _CtxSession:
    """Async-context wrapper so a task reuses the test's transactional session
    (an independent AsyncSession(engine) would not see the test's uncommitted rows)."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def task_session(monkeypatch, session):
    """Run queue tasks against the test's session."""
    monkeypatch.setattr(task_queue, "AsyncSession", lambda *a, **k: _CtxSession(session))
    return session


# ── Fixtures ──────────────────────────────────────────────────────────────────


async def _scheduled_scenario(session: AsyncSession, facilitator: User, *, offset: int = 30):
    definition = ScenarioDefinition(
        title="Scheduled Scenario",
        participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
        injects=[
            InjectNode(
                id="inject_01",
                title="Scheduled Brief",
                content="Auto-releases on a timer.",
                target_teams=["it_ops"],
                release_at_minutes=offset,
            ),
        ],
        start_inject_id="inject_01",
    )
    return await create_scenario(session, definition=definition, created_by=facilitator.id)


async def _make_exercise(session, facilitator, participant, *, offset=30, active=False):
    scenario = await _scheduled_scenario(session, facilitator, offset=offset)
    ex = await create_exercise(
        session, scenario_id=scenario.id, title="Sched Ex", created_by=facilitator.id
    )
    await enrol_member(session, exercise=ex, user_id=participant.id, group_id="it_ops")
    if active:
        ex = await transition_state(session, ex, ExerciseState.active)
    return ex


async def _first_inject(session: AsyncSession, exercise_id: int) -> Inject:
    return (
        await session.exec(select(Inject).where(Inject.exercise_id == exercise_id))
    ).first()


# ── Validator ─────────────────────────────────────────────────────────────────


def test_validator_accepts_release_at_minutes():
    node = InjectNode(id="a", title="t", content="c", release_at_minutes=15)
    assert node.release_at_minutes == 15


def test_validator_rejects_negative_release_at_minutes():
    with pytest.raises(ValueError, match="release_at_minutes must be >= 0"):
        InjectNode(id="a", title="t", content="c", release_at_minutes=-1)


def test_scheduled_field_adds_no_cycle_edge():
    # A self-referential release_at_minutes value must not be treated as a graph edge.
    ScenarioDefinition(
        title="s",
        injects=[InjectNode(id="only", title="t", content="c", release_at_minutes=5)],
        start_inject_id="only",
    )


# ── Seeding ───────────────────────────────────────────────────────────────────


async def test_seed_copies_offset_onto_inject(
    client: AsyncClient, session: AsyncSession, facilitator: User, participant: User
):
    ex = await _make_exercise(session, facilitator, participant, offset=20)
    inject = await _first_inject(session, ex.id)
    assert inject.release_offset_minutes == 20


# ── Pause-aware clock ─────────────────────────────────────────────────────────


async def test_pause_timing_fields(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise
):
    # Active: no pause recorded yet.
    r = await client.get(f"/api/exercises/{active_exercise.id}", headers=AUTH(facilitator_token))
    assert r.json()["paused_at"] is None
    assert r.json()["accumulated_pause_seconds"] == 0

    r = await client.post(
        f"/api/exercises/{active_exercise.id}/pause", headers=AUTH(facilitator_token)
    )
    assert r.json()["paused_at"] is not None
    assert r.json()["accumulated_pause_seconds"] == 0

    r = await client.post(
        f"/api/exercises/{active_exercise.id}/resume", headers=AUTH(facilitator_token)
    )
    body = r.json()
    assert body["paused_at"] is None
    # Resuming folds the pause span into the accumulator.
    assert body["accumulated_pause_seconds"] >= 0


async def test_state_change_broadcast_over_ws(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    async with aconnect_ws(
        f"/ws/exercises/{active_exercise.id}", client,
        headers={"origin": "http://testserver", "cookie": f"access_token={facilitator_token}"},
    ) as ws:
        await client.post(
            f"/api/exercises/{active_exercise.id}/pause", headers=AUTH(facilitator_token)
        )
        msg = await ws.receive_json()
    assert msg["type"] == "exercise_state_change"
    assert msg["payload"]["state"] == "paused"
    assert msg["payload"]["paused_at"] is not None


def test_trigger_delay_excludes_multiple_persisted_pause_spans(monkeypatch):
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(task_queue, "now", lambda: now)
    exercise = Exercise(
        id=7,
        scenario_id=3,
        title="Pause-aware trigger",
        state=ExerciseState.active,
        created_by=1,
    )
    transitions = [
        ExerciseStateTransition(
            exercise_id=7,
            from_state=ExerciseState.active,
            to_state=ExerciseState.paused,
            transitioned_at=now - timedelta(seconds=240),
        ),
        ExerciseStateTransition(
            exercise_id=7,
            from_state=ExerciseState.paused,
            to_state=ExerciseState.active,
            transitioned_at=now - timedelta(seconds=180),
        ),
        ExerciseStateTransition(
            exercise_id=7,
            from_state=ExerciseState.active,
            to_state=ExerciseState.paused,
            transitioned_at=now - timedelta(seconds=120),
        ),
        ExerciseStateTransition(
            exercise_id=7,
            from_state=ExerciseState.paused,
            to_state=ExerciseState.active,
            transitioned_at=now - timedelta(seconds=90),
        ),
    ]

    elapsed = schedule_service._active_elapsed_since(
        exercise, now - timedelta(seconds=300), transitions
    )
    assert elapsed == 210


# ── Enqueueing ────────────────────────────────────────────────────────────────


async def test_start_enqueues_the_release(
    client: AsyncClient, facilitator_token: str, session: AsyncSession,
    facilitator: User, participant: User,
):
    ex = await _make_exercise(session, facilitator, participant, offset=30)
    inject = await _first_inject(session, ex.id)

    await client.post(f"/api/exercises/{ex.id}/start", headers=AUTH(facilitator_token))

    jobs = await _release_jobs(session, inject.id)
    assert len(jobs) == 1
    assert jobs[0]["args"] == {"exercise_id": ex.id, "inject_id": inject.id}
    # Thirty minutes out, give or take the second this test took to run.
    assert jobs[0]["scheduled_at"] > task_queue.now() + timedelta(minutes=29)


async def test_the_release_job_survives_a_pause_without_duplicating(
    client: AsyncClient, facilitator_token: str, session: AsyncSession,
    facilitator: User, participant: User,
):
    """Pausing cancels nothing, and resuming stacks nothing.

    The job written at start is still live across the pause, so resume leaves it alone
    rather than adding a second one per cycle. Its deadline is now early — a pause only
    ever extends deadlines — and an early-firing job re-defers itself with the
    recomputed remaining time, so correctness never depended on the re-enqueue."""
    ex = await _make_exercise(session, facilitator, participant, offset=30)
    inject = await _first_inject(session, ex.id)
    await client.post(f"/api/exercises/{ex.id}/start", headers=AUTH(facilitator_token))

    await client.post(f"/api/exercises/{ex.id}/pause", headers=AUTH(facilitator_token))
    assert len(await _release_jobs(session, inject.id)) == 1

    await client.post(f"/api/exercises/{ex.id}/resume", headers=AUTH(facilitator_token))
    assert len(await _release_jobs(session, inject.id)) == 1


async def test_a_paused_exercise_releases_nothing(
    task_session: AsyncSession, facilitator: User, participant: User,
):
    ex = await _make_exercise(task_session, facilitator, participant, offset=5, active=True)
    ex = await transition_state(task_session, ex, ExerciseState.paused)
    inject = await _first_inject(task_session, ex.id)

    await task_queue.release_scheduled_inject(exercise_id=ex.id, inject_id=inject.id)

    assert inject.state == InjectState.pending


async def test_a_job_that_fires_early_re_defers_itself(
    task_session: AsyncSession, facilitator: User, participant: User, monkeypatch,
):
    """A pause moves the deadline out from under a job already in the table.

    Rather than sleep — the model this replaces — the task puts itself back with the
    remaining time and lets go of its worker.
    """
    ex = await _make_exercise(task_session, facilitator, participant, offset=30, active=True)
    inject = await _first_inject(task_session, ex.id)
    before = len(await _release_jobs(task_session, inject.id))

    await task_queue.release_scheduled_inject(exercise_id=ex.id, inject_id=inject.id)

    assert inject.state == InjectState.pending
    assert len(await _release_jobs(task_session, inject.id)) == before + 1


async def test_releasing_early_leaves_a_harmless_job(
    client: AsyncClient, facilitator_token: str, task_session: AsyncSession,
    facilitator: User, participant: User,
):
    """The stale job is not chased down — it fires, finds the inject already released,
    and stops. The CAS in release_inject is what makes that safe."""
    ex = await _make_exercise(task_session, facilitator, participant, offset=30)
    inject = await _first_inject(task_session, ex.id)
    await client.post(f"/api/exercises/{ex.id}/start", headers=AUTH(facilitator_token))
    r = await client.post(
        f"/api/exercises/{ex.id}/injects/{inject.id}/release", headers=AUTH(facilitator_token)
    )
    assert r.status_code == 200

    await task_queue.release_scheduled_inject(exercise_id=ex.id, inject_id=inject.id)

    assert inject.state == InjectState.released
    assert inject.released_by == facilitator.id  # not overwritten by a second release


# ── Runtime schedule editing (PATCH) ──────────────────────────────────────────


async def test_schedule_patch_enqueues_against_the_new_offset(
    client: AsyncClient, facilitator_token: str, session: AsyncSession,
    active_exercise: Exercise,
):
    ir = await client.get(
        f"/api/exercises/{active_exercise.id}/injects", headers=AUTH(facilitator_token)
    )
    inject_id = ir.json()[0]["id"]

    r = await client.patch(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}/schedule",
        json={"release_offset_minutes": 12},
        headers=AUTH(facilitator_token),
    )
    assert r.status_code == 200
    assert r.json()["release_offset_minutes"] == 12
    jobs = await _release_jobs(session, inject_id)
    assert len(jobs) == 1
    assert jobs[0]["scheduled_at"] < task_queue.now() + timedelta(minutes=13)

    r = await client.patch(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}/schedule",
        json={"release_offset_minutes": None},
        headers=AUTH(facilitator_token),
    )
    assert r.json()["release_offset_minutes"] is None
    # Clearing the offset enqueues nothing new; the existing job will find a null offset
    # when it fires and stand down.
    assert len(await _release_jobs(session, inject_id)) == 1


async def test_schedule_patch_rejects_negative(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    ir = await client.get(
        f"/api/exercises/{active_exercise.id}/injects", headers=AUTH(facilitator_token)
    )
    inject_id = ir.json()[0]["id"]
    r = await client.patch(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}/schedule",
        json={"release_offset_minutes": -5},
        headers=AUTH(facilitator_token),
    )
    assert r.status_code == 422


async def test_schedule_patch_rejects_released_inject(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    ir = await client.get(
        f"/api/exercises/{active_exercise.id}/injects", headers=AUTH(facilitator_token)
    )
    inject_id = ir.json()[0]["id"]
    await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
        headers=AUTH(facilitator_token),
    )
    r = await client.patch(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}/schedule",
        json={"release_offset_minutes": 5},
        headers=AUTH(facilitator_token),
    )
    assert r.status_code == 409


# ── The fire path ─────────────────────────────────────────────────────────────


async def test_scheduled_release_fires_and_broadcasts(
    client: AsyncClient,
    facilitator_token: str,
    task_session: AsyncSession,
    facilitator: User,
    participant: User,
):
    ex = await _make_exercise(task_session, facilitator, participant, offset=0, active=True)
    inject = await _first_inject(task_session, ex.id)

    async with aconnect_ws(
        f"/ws/exercises/{ex.id}", client,
        headers={"origin": "http://testserver", "cookie": f"access_token={facilitator_token}"},
    ) as ws:
        await task_queue.release_scheduled_inject(exercise_id=ex.id, inject_id=inject.id)
        msg = await ws.receive_json()

    assert msg["type"] == "inject_released"
    assert msg["payload"]["id"] == inject.id
    assert msg["payload"]["state"] == "released"
    assert msg["payload"]["released_by"] is None  # system/auto release
    task_session.expire_all()
    refreshed = await task_session.get(Inject, inject.id)
    assert refreshed.state == InjectState.released


async def test_release_inject_refused_when_exercise_paused(
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    """The state re-read under the release lock closes the pause/release TOCTOU window (#265).

    Reproduces the real cross-session race: the exercise is pre-loaded as ``active`` (as both
    callers load it), then a pause commits via a Core UPDATE that leaves the identity-mapped
    object stale. release_inject must still refuse — it re-reads the locked row with
    populate_existing, not the cached ``active`` attribute."""
    from fastapi import HTTPException
    from sqlalchemy import update

    from app.services.inject_service import release_inject

    ex = await _make_exercise(session, facilitator, participant, offset=5, active=True)
    inject = await _first_inject(session, ex.id)

    # Pause the row without touching the in-session Exercise, mimicking a pause committed by a
    # separate request. The cached object stays 'active' — the exact stale read that a plain
    # locked select would return.
    await session.exec(
        update(Exercise)
        .where(Exercise.id == ex.id)
        .values(state=ExerciseState.paused)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    assert ex.state == ExerciseState.active  # identity-map instance is stale

    with pytest.raises(HTTPException) as exc:
        await release_inject(session, inject, released_by=facilitator.id)
    assert exc.value.status_code == 409
    assert inject.state == InjectState.pending


# ── The progression cursor gates a scheduled release ──────────────────────────


async def _linear_scheduled_exercise(
    session: AsyncSession, facilitator: User, participant: User, *, offset: int
) -> Exercise:
    """A start node whose *linear successor* carries the schedule.

    Every other test here schedules the start inject, which the cursor points at from the
    moment the exercise begins — so the cursor never gets in the way, and this gap stayed
    invisible. A downstream inject is only reachable once the team responds to the one
    before it.
    """
    definition = ScenarioDefinition(
        title="Pressure builds",
        participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
        injects=[
            InjectNode(
                id="detect",
                title="Anomaly detected",
                content="Unusual outbound traffic.",
                target_teams=["it_ops"],
                next_inject_id="escalate",
            ),
            InjectNode(
                id="escalate",
                title="It is getting worse",
                content="A second unit reports the same symptoms.",
                target_teams=["it_ops"],
                release_at_minutes=offset,
            ),
        ],
        start_inject_id="detect",
    )
    scenario = await create_scenario(session, definition=definition, created_by=facilitator.id)
    exercise = await create_exercise(
        session, scenario_id=scenario.id, title="Linear sched", created_by=facilitator.id
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id, group_id="it_ops")
    return await transition_state(session, exercise, ExerciseState.active)


async def _inject_by_node(session: AsyncSession, exercise_id: int, node_id: str) -> Inject:
    return (
        await session.exec(
            select(Inject)
            .where(Inject.exercise_id == exercise_id)
            .where(Inject.scenario_node_id == node_id)
        )
    ).one()


async def test_due_release_is_deferred_when_the_cursor_has_not_arrived(
    task_session: AsyncSession,
    facilitator: User,
    participant: User,
    monkeypatch,
    caplog,
):
    """A job coming due early is *deferred*, not failed.

    The team has not responded their way to `escalate` yet, so it must not release. The
    point of this test is the *manner* of the refusal: the task checks the gate itself
    and returns, so nothing is logged as an error. Before #218 the release attempt raised
    409 and the worker's `except Exception` swallowed it, which is what made a routine
    "not yet" indistinguishable from a genuine failure.
    """
    exercise = await _linear_scheduled_exercise(
        task_session, facilitator, participant, offset=0
    )
    escalate = await _inject_by_node(task_session, exercise.id, "escalate")
    from app.services import audit_service

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        audit_service, "emit", lambda action, **kw: events.append((action, kw))
    )

    with caplog.at_level("INFO", logger="app.services.task_queue"):
        await task_queue.release_scheduled_inject(
            exercise_id=exercise.id, inject_id=escalate.id
        )

    assert escalate.state == InjectState.pending
    assert [action for action, _ in events] == ["inject.release_deferred"]
    assert events[0][1]["reason"] == "cursor_not_reached"


async def test_a_response_enqueues_the_release_the_cursor_reaches(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """The half of #218 that turns "never" into "when they get there".

    A job that fired before the team arrived stopped for good; the response that advances
    the cursor onto the node is what puts a new one in the table.
    """
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")

    await client.post(
        f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
        headers=AUTH(facilitator_token),
    )
    assert await _release_jobs(session, escalate.id) == []

    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/responses",
            json={"inject_id": detect.id, "content": "Investigating."},
            headers=AUTH(participant_token),
        )
    ).status_code == 201

    assert len(await _release_jobs(session, escalate.id)) == 1


async def test_the_cursor_enqueue_rides_the_responses_transaction(
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    """The #218 race, made unrepresentable.

    The old in-memory version had to coordinate by hand around a worker deciding to stand
    down while a response was mid-commit. Now the enqueue is part of the response's unit
    of work: roll that back and the job goes with it, so there is no state in which the
    cursor advanced but nothing was scheduled.
    """
    from app.services.response_service import submit_response

    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")
    detect.state = InjectState.released
    detect.released_at = datetime.now(UTC)
    session.add(detect)
    await session.commit()

    await submit_response(
        session,
        inject_id=detect.id,
        exercise_id=exercise.id,
        user_id=participant.id,
        content="Investigating.",
        group_id="it_ops",
    )

    assert len(await _release_jobs(session, escalate.id)) == 1


async def test_an_overdue_scheduled_inject_is_enqueued_for_now_when_the_cursor_arrives(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """A cursor arriving *after* the offset has passed releases the inject immediately.

    The delay comes off the offset's absolute basis, so "overdue" falls out as "schedule
    it for now" rather than needing a branch of its own.
    """
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")
    # 45 minutes into a 30-minute offset: the countdown expired while the team was still
    # working on `detect`.
    exercise.started_at = datetime.now(UTC) - timedelta(minutes=45)
    session.add(exercise)
    await session.flush()

    await client.post(
        f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
        headers=AUTH(facilitator_token),
    )
    await client.post(
        f"/api/exercises/{exercise.id}/responses",
        json={"inject_id": detect.id, "content": "Investigating."},
        headers=AUTH(participant_token),
    )

    jobs = await _release_jobs(session, escalate.id)
    assert len(jobs) == 1
    assert jobs[0]["scheduled_at"] <= task_queue.now() + timedelta(seconds=1)


async def test_a_response_does_not_enqueue_the_branch_the_team_did_not_take(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """Enqueueing follows the cursor, so it can only ever reach the chosen branch."""
    definition = ScenarioDefinition(
        title="Fork",
        participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
        injects=[
            InjectNode(
                id="triage",
                title="Triage",
                content="Contain or monitor?",
                target_teams=["it_ops"],
                options=[
                    {"id": "contain", "label": "Contain", "next_inject_id": "containment"},
                    {"id": "monitor", "label": "Monitor", "next_inject_id": "spread"},
                ],
            ),
            InjectNode(
                id="containment",
                title="Containment holds",
                content="Isolated.",
                target_teams=["it_ops"],
                release_at_minutes=30,
            ),
            InjectNode(
                id="spread",
                title="It spreads",
                content="Second host hit.",
                target_teams=["it_ops"],
                release_at_minutes=30,
            ),
        ],
        start_inject_id="triage",
    )
    scenario = await create_scenario(session, definition=definition, created_by=facilitator.id)
    exercise = await create_exercise(
        session, scenario_id=scenario.id, title="Fork Ex", created_by=facilitator.id
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id, group_id="it_ops")
    exercise = await transition_state(session, exercise, ExerciseState.active)
    triage = await _inject_by_node(session, exercise.id, "triage")
    containment = await _inject_by_node(session, exercise.id, "containment")
    spread = await _inject_by_node(session, exercise.id, "spread")

    await client.post(
        f"/api/exercises/{exercise.id}/injects/{triage.id}/release",
        headers=AUTH(facilitator_token),
    )
    await client.post(
        f"/api/exercises/{exercise.id}/responses",
        json={"inject_id": triage.id, "content": "Contain it.", "selected_option": "contain"},
        headers=AUTH(participant_token),
    )

    assert len(await _release_jobs(session, containment.id)) == 1
    assert await _release_jobs(session, spread.id) == []


async def test_scheduled_release_of_an_unreferenced_node_survives_the_first_response(
    client: AsyncClient,
    task_session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """A timed node nothing links to fires on its clock, first response or not.

    No cursor will *ever* point at an orphan, so re-enqueueing cannot save it — the cursor
    lock has to let a *scheduled* release through instead. It still refuses a manual one,
    which test_responses.py::test_unlinked_inject_is_releasable_only_before_the_first_response
    pins from the other side. This is the "at T+40 the press calls" shape: a parallel
    timeline that depends on no branch.
    """
    definition = ScenarioDefinition(
        title="Parallel timeline",
        participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
        injects=[
            InjectNode(
                id="detect",
                title="Anomaly detected",
                content="Unusual outbound traffic.",
                target_teams=["it_ops"],
            ),
            InjectNode(
                id="press_call",
                title="The press calls",
                content="A journalist has the story.",
                target_teams=["it_ops"],
                release_at_minutes=0,
            ),
        ],
        start_inject_id="detect",
    )
    scenario = await create_scenario(
        task_session, definition=definition, created_by=facilitator.id
    )
    exercise = await create_exercise(
        task_session, scenario_id=scenario.id, title="Parallel Ex", created_by=facilitator.id
    )
    await enrol_member(
        task_session, exercise=exercise, user_id=participant.id, group_id="it_ops"
    )
    exercise = await transition_state(task_session, exercise, ExerciseState.active)
    detect = await _inject_by_node(task_session, exercise.id, "detect")
    press_call = await _inject_by_node(task_session, exercise.id, "press_call")

    # The first response anywhere is what used to shut the orphan's release window.
    await client.post(
        f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
        headers=AUTH(facilitator_token),
    )
    await client.post(
        f"/api/exercises/{exercise.id}/responses",
        json={"inject_id": detect.id, "content": "Investigating."},
        headers=AUTH(participant_token),
    )

    await task_queue.release_scheduled_inject(
        exercise_id=exercise.id, inject_id=press_call.id
    )

    assert press_call.state == InjectState.released


async def test_scheduled_release_fires_once_the_team_has_reached_the_inject(
    client: AsyncClient,
    task_session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """The same schedule does fire once a response has advanced the cursor onto it."""
    exercise = await _linear_scheduled_exercise(
        task_session, facilitator, participant, offset=0
    )
    detect = await _inject_by_node(task_session, exercise.id, "detect")
    escalate = await _inject_by_node(task_session, exercise.id, "escalate")

    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
            headers=AUTH(facilitator_token),
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/responses",
            json={"inject_id": detect.id, "content": "Investigating."},
            headers=AUTH(participant_token),
        )
    ).status_code == 201

    await task_queue.release_scheduled_inject(exercise_id=exercise.id, inject_id=escalate.id)

    assert escalate.state == InjectState.released


# ── Triggered communications ──────────────────────────────────────────────────


async def test_releasing_an_inject_enqueues_its_triggered_comms_transactionally(
    session: AsyncSession,
    facilitator: User,
    participant: User,
    sample_scenario,
    active_exercise: Exercise,
):
    """#211, closed structurally.

    The comms used to be armed by a post-commit subscriber, so a release that committed
    and then lost its process left them unarmed forever. Enqueued in the release's own
    transaction, the jobs exist if and only if the release does.
    """
    definition = ScenarioDefinition.model_validate_json(sample_scenario.definition)
    definition.injects[0].triggers_communications = [
        TriggerComm(
            external_entity="NCSC",
            direction="inbound",
            subject="Delayed advisory",
            body="Call the incident hotline.",
            delay_after_release_seconds=300,
        )
    ]
    sample_scenario.definition = definition.model_dump_json()
    session.add(sample_scenario)
    await session.commit()
    inject = await _first_inject(session, active_exercise.id)

    from app.services.inject_service import release_inject

    await release_inject(session, inject, released_by=facilitator.id)

    jobs = await _comm_jobs(session, "inject_01")
    assert len(jobs) == 1
    assert jobs[0]["args"]["trigger_index"] == 0
    assert jobs[0]["scheduled_at"] > task_queue.now() + timedelta(seconds=290)


async def test_triggered_comms_are_reconstructed_from_durable_state(
    session: AsyncSession,
    active_exercise: Exercise,
    sample_scenario,
):
    """Resume and startup reconciliation both derive owed comms from rows alone (#194)."""
    definition = ScenarioDefinition.model_validate_json(sample_scenario.definition)
    definition.injects[0].triggers_communications = [
        TriggerComm(
            external_entity="NCSC",
            direction="inbound",
            subject="Delayed advisory",
            body="Call the incident hotline.",
            delay_after_release_seconds=300,
        )
    ]
    sample_scenario.definition = definition.model_dump_json()
    inject = await _first_inject(session, active_exercise.id)
    inject.state = InjectState.released
    inject.released_at = datetime.now(UTC) - timedelta(seconds=30)
    session.add(sample_scenario)
    session.add(inject)
    await session.commit()

    await schedule_service.schedule_exercise_communications(session, active_exercise)

    jobs = await _comm_jobs(session, "inject_01")
    assert len(jobs) == 1
    # 300s delay, 30s already elapsed since the release.
    assert jobs[0]["scheduled_at"] < task_queue.now() + timedelta(seconds=275)


async def test_an_already_delivered_trigger_is_not_reconstructed(
    session: AsyncSession,
    active_exercise: Exercise,
    sample_scenario,
):
    """The persisted trigger_key is the record of delivery, and it wins over any
    reconstruction — which is what stops a restart from re-sending it."""
    definition = ScenarioDefinition.model_validate_json(sample_scenario.definition)
    definition.injects[0].triggers_communications = [
        TriggerComm(
            external_entity="NCSC",
            direction="inbound",
            subject="Delayed advisory",
            body="Call the incident hotline.",
            delay_after_release_seconds=300,
        )
    ]
    sample_scenario.definition = definition.model_dump_json()
    inject = await _first_inject(session, active_exercise.id)
    inject.state = InjectState.released
    inject.released_at = datetime.now(UTC) - timedelta(seconds=30)
    session.add_all([sample_scenario, inject])
    session.add(
        Communication(
            exercise_id=active_exercise.id,
            direction=CommDirection.inbound,
            external_entity="NCSC",
            subject="Delayed advisory",
            body="Call the incident hotline.",
            triggered_by_inject_id=inject.id,
            trigger_key="inject_01:0",
        )
    )
    await session.commit()

    await schedule_service.schedule_exercise_communications(session, active_exercise)

    assert await _comm_jobs(session, "inject_01") == []


async def test_a_triggered_comm_job_reads_its_content_at_delivery_time(
    task_session: AsyncSession,
    active_exercise: Exercise,
    sample_scenario,
):
    """Content lives in the scenario, not the job args, so a facilitator's edit between
    enqueue and delivery is honoured rather than delivering the superseded text."""
    definition = ScenarioDefinition.model_validate_json(sample_scenario.definition)
    definition.injects[0].triggers_communications = [
        TriggerComm(
            external_entity="NCSC",
            direction="inbound",
            subject="Edited subject",
            body="Edited body.",
            delay_after_release_seconds=0,
        )
    ]
    sample_scenario.definition = definition.model_dump_json()
    inject = await _first_inject(task_session, active_exercise.id)
    inject.state = InjectState.released
    inject.released_at = datetime.now(UTC)
    task_session.add_all([sample_scenario, inject])
    await task_session.commit()

    await task_queue.deliver_triggered_comm(
        exercise_id=active_exercise.id,
        inject_id=inject.id,
        node_id="inject_01",
        trigger_index=0,
    )

    comms = (
        await task_session.exec(
            select(Communication).where(Communication.exercise_id == active_exercise.id)
        )
    ).all()
    assert [c.subject for c in comms] == ["Edited subject"]


async def test_a_triggered_comm_job_delivers_once(
    task_session: AsyncSession,
    active_exercise: Exercise,
    sample_scenario,
):
    """Duplicate jobs are expected — the unique (exercise_id, trigger_key) insert is what
    makes them cost nothing."""
    definition = ScenarioDefinition.model_validate_json(sample_scenario.definition)
    definition.injects[0].triggers_communications = [
        TriggerComm(
            external_entity="NCSC",
            direction="inbound",
            subject="Advisory",
            body="Hotline.",
            delay_after_release_seconds=0,
        )
    ]
    sample_scenario.definition = definition.model_dump_json()
    inject = await _first_inject(task_session, active_exercise.id)
    inject.state = InjectState.released
    inject.released_at = datetime.now(UTC)
    task_session.add_all([sample_scenario, inject])
    await task_session.commit()

    for _ in range(3):
        await task_queue.deliver_triggered_comm(
            exercise_id=active_exercise.id,
            inject_id=inject.id,
            node_id="inject_01",
            trigger_index=0,
        )

    comms = (
        await task_session.exec(
            select(Communication).where(Communication.exercise_id == active_exercise.id)
        )
    ).all()
    assert len(comms) == 1


# ── Startup reconciliation ────────────────────────────────────────────────────


@pytest.fixture
def reconcile_session(monkeypatch, session):
    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )
    return session


async def test_reconcile_enqueues_what_has_no_live_job(
    reconcile_session: AsyncSession, facilitator: User, participant: User,
):
    """The cutover and safety-net path: an exercise active across a deploy from a build
    whose schedules lived in memory has rows but no jobs, and startup restores them."""
    ex = await _make_exercise(
        reconcile_session, facilitator, participant, offset=30, active=True
    )
    inject = await _first_inject(reconcile_session, ex.id)
    assert await _release_jobs(reconcile_session, inject.id) == []

    await schedule_service.reconcile_release_jobs()

    assert len(await _release_jobs(reconcile_session, inject.id)) == 1


async def test_reconcile_is_idempotent_across_restarts(
    reconcile_session: AsyncSession, facilitator: User, participant: User,
):
    """Every replica reconciles at every boot, so without the live-job check a rolling
    deploy would stack one duplicate job set per active exercise per restart."""
    ex = await _make_exercise(
        reconcile_session, facilitator, participant, offset=30, active=True
    )
    inject = await _first_inject(reconcile_session, ex.id)

    for _ in range(3):
        await schedule_service.reconcile_release_jobs()

    assert len(await _release_jobs(reconcile_session, inject.id)) == 1


# ── Durability ────────────────────────────────────────────────────────────────


async def test_a_release_that_rolls_back_enqueues_nothing(
    session: AsyncSession, facilitator: User, participant: User
):
    """The property the whole design turns on, stated directly.

    A job written outside the transaction could outlive a rollback and release an inject
    for a state change that never happened; a job written inside it cannot.
    """
    ex = await _make_exercise(session, facilitator, participant, offset=30, active=True)
    inject = await _first_inject(session, ex.id)
    # Held as plain ints: the rollback below expires every instance, and re-reading one
    # to learn its id would be a query about the state under test.
    inject_id = inject.id

    await session.begin_nested()
    await schedule_service.defer_inject_release(session, ex, inject)
    assert len(await _release_jobs(session, inject_id)) == 1
    await session.rollback()

    assert await _release_jobs(session, inject_id) == []
