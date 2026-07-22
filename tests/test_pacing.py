"""Pacing: exercise clock and durable exercise schedules (#116, #194, #218)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from httpx_ws import aconnect_ws
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import CommDirection, Communication
from app.models.exercise import Exercise, ExerciseState, ExerciseStateTransition
from app.models.inject import Inject, InjectState
from app.models.user import User
from app.schemas.scenario_json import InjectNode, ScenarioDefinition, TriggerComm
from app.services import background, progression_service, schedule_service
from app.services.exercise_service import (
    create_exercise,
    enrol_member,
    transition_state,
)
from app.services.response_service import submit_response
from app.services.scenario_service import create_scenario

AUTH = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731


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


# ── Scheduler registry: arm / defer / cancel / re-arm ─────────────────────────


async def test_start_arms_schedule(
    client: AsyncClient, facilitator_token: str, session: AsyncSession,
    facilitator: User, participant: User,
):
    ex = await _make_exercise(session, facilitator, participant, offset=30)
    inject = await _first_inject(session, ex.id)
    await client.post(f"/api/exercises/{ex.id}/start", headers=AUTH(facilitator_token))
    assert ex.id in schedule_service._scheduled
    assert inject.id in schedule_service._scheduled[ex.id]


async def test_pause_defers_and_resume_rearms(
    client: AsyncClient, facilitator_token: str, session: AsyncSession,
    facilitator: User, participant: User,
):
    ex = await _make_exercise(session, facilitator, participant, offset=30)
    inject = await _first_inject(session, ex.id)
    await client.post(f"/api/exercises/{ex.id}/start", headers=AUTH(facilitator_token))
    await client.post(f"/api/exercises/{ex.id}/pause", headers=AUTH(facilitator_token))
    assert ex.id not in schedule_service._scheduled  # deferred
    await client.post(f"/api/exercises/{ex.id}/resume", headers=AUTH(facilitator_token))
    assert inject.id in schedule_service._scheduled.get(ex.id, {})  # re-armed


async def test_triggered_comms_rehydrate_and_follow_pause_resume(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    active_exercise: Exercise,
    sample_scenario,
):
    """Pending trigger timers derive from durable state after restart or resume (#194)."""
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

    # Startup rehydration delegates to this persisted-state reconstruction.
    await schedule_service.schedule_exercise_injects(session, active_exercise)
    assert "inject_01:0" in schedule_service._scheduled_comms.get(active_exercise.id, {})

    await client.post(
        f"/api/exercises/{active_exercise.id}/pause", headers=AUTH(facilitator_token)
    )
    assert active_exercise.id not in schedule_service._scheduled_comms
    await client.post(
        f"/api/exercises/{active_exercise.id}/resume", headers=AUTH(facilitator_token)
    )
    assert "inject_01:0" in schedule_service._scheduled_comms.get(active_exercise.id, {})

    # A persisted delivery key wins over reconstruction, including after a restart.
    schedule_service.cancel_exercise_schedules(active_exercise.id)
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
    await session.refresh(active_exercise)
    await schedule_service.schedule_exercise_injects(session, active_exercise)
    assert active_exercise.id not in schedule_service._scheduled_comms


def test_trigger_delay_excludes_multiple_persisted_pause_spans(monkeypatch):
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(schedule_service, "datetime", FrozenDateTime)
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


async def test_complete_cancels_schedules(
    client: AsyncClient, facilitator_token: str, session: AsyncSession,
    facilitator: User, participant: User,
):
    ex = await _make_exercise(session, facilitator, participant, offset=30)
    await client.post(f"/api/exercises/{ex.id}/start", headers=AUTH(facilitator_token))
    await client.post(f"/api/exercises/{ex.id}/complete", headers=AUTH(facilitator_token))
    assert ex.id not in schedule_service._scheduled


async def test_release_early_cancels_schedule(
    client: AsyncClient, facilitator_token: str, session: AsyncSession,
    facilitator: User, participant: User,
):
    ex = await _make_exercise(session, facilitator, participant, offset=30)
    inject = await _first_inject(session, ex.id)
    await client.post(f"/api/exercises/{ex.id}/start", headers=AUTH(facilitator_token))
    r = await client.post(
        f"/api/exercises/{ex.id}/injects/{inject.id}/release", headers=AUTH(facilitator_token)
    )
    assert r.status_code == 200
    assert r.json()["state"] == "released"
    assert inject.id not in schedule_service._scheduled.get(ex.id, {})


# ── Runtime schedule editing (PATCH) ──────────────────────────────────────────


async def test_schedule_patch_sets_and_clears(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
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
    assert inject_id in schedule_service._scheduled.get(active_exercise.id, {})

    r = await client.patch(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}/schedule",
        json={"release_offset_minutes": None},
        headers=AUTH(facilitator_token),
    )
    assert r.json()["release_offset_minutes"] is None
    assert inject_id not in schedule_service._scheduled.get(active_exercise.id, {})


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


# ── Worker fire path ──────────────────────────────────────────────────────────


class _CtxSession:
    """Async-context wrapper so the worker reuses the test's transactional session
    (an independent AsyncSession(engine) would not see the test's uncommitted rows)."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_scheduled_release_fires_and_broadcasts(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    monkeypatch,
):
    ex = await _make_exercise(session, facilitator, participant, offset=5, active=True)
    inject = await _first_inject(session, ex.id)

    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )

    async with aconnect_ws(
        f"/ws/exercises/{ex.id}", client,
        headers={"origin": "http://testserver", "cookie": f"access_token={facilitator_token}"},
    ) as ws:
        await schedule_service._release_when_due(ex.id, inject.id, 0)
        msg = await ws.receive_json()

    assert msg["type"] == "inject_released"
    assert msg["payload"]["id"] == inject.id
    assert msg["payload"]["state"] == "released"
    assert msg["payload"]["released_by"] is None  # system/auto release
    session.expire_all()
    refreshed = await session.get(Inject, inject.id)
    assert refreshed.state == InjectState.released


async def test_worker_skips_when_paused(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    monkeypatch,
):
    ex = await _make_exercise(session, facilitator, participant, offset=5, active=True)
    ex = await transition_state(session, ex, ExerciseState.paused)
    inject = await _first_inject(session, ex.id)

    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )
    await schedule_service._release_when_due(ex.id, inject.id, 0)

    # Guard held — no release while paused. The worker shares this session's identity
    # map, so the in-memory inject is authoritative (and unchanged).
    assert inject.state == InjectState.pending


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
    session: AsyncSession,
    facilitator: User,
    participant: User,
    monkeypatch,
    caplog,
):
    """A timer coming due early is *deferred*, not failed.

    The team has not responded their way to `escalate` yet, so it must not release. The
    point of this test is the *manner* of the refusal: the worker checks the gate itself
    and returns, so nothing is logged as an error. Before #218 the release attempt raised
    409 inside release_inject and the worker's `except Exception` swallowed it, which is
    what made a routine "not yet" indistinguishable from a genuine failure.
    """
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    escalate = await _inject_by_node(session, exercise.id, "escalate")
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        schedule_service.audit_service, "emit", lambda action, **kw: events.append((action, kw))
    )
    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )

    with caplog.at_level("INFO", logger="app.services.schedule_service"):
        await schedule_service._release_when_due(exercise.id, escalate.id, 0)

    assert escalate.state == InjectState.pending
    assert not [r for r in caplog.records if "Scheduled release failed" in r.message]
    assert [action for action, _ in events] == ["inject.release_deferred"]
    assert events[0][1]["reason"] == "cursor_not_reached"


async def test_a_response_rearms_the_scheduled_inject_the_cursor_reaches(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """The deferred timer comes back the moment the team's response unlocks the node.

    This is the half of #218 that turns "never" into "when they get there": the timer is
    one-shot, so without a re-arm on cursor advance the deferral above would be permanent.
    The offset is still in the future here, so the re-armed task just sleeps — asserting on
    the registry keeps it deterministic (see the note on _CtxSession above).
    """
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")

    await client.post(
        f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
        headers=AUTH(facilitator_token),
    )
    assert escalate.id not in schedule_service._scheduled.get(exercise.id, {})

    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/responses",
            json={"inject_id": detect.id, "content": "Investigating."},
            headers=AUTH(participant_token),
        )
    ).status_code == 201

    assert escalate.id in schedule_service._scheduled[exercise.id]


async def test_an_overdue_scheduled_inject_arms_at_zero_when_the_cursor_arrives(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
    monkeypatch,
):
    """A cursor arriving *after* the offset has passed releases the inject immediately.

    The delay comes off the offset's absolute basis, so "overdue" falls out as delay 0
    rather than needing a branch of its own. Spy _arm instead of awaiting the task: it may
    already have run and been forgotten by the time we look.
    """
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")
    # 45 minutes into a 30-minute offset: the countdown expired while the team was still
    # working on `detect`, and its timer has long since deferred and dropped.
    exercise.started_at = datetime.now(UTC) - timedelta(minutes=45)
    session.add(exercise)
    await session.flush()

    armed: list[tuple[int, int, float]] = []
    monkeypatch.setattr(
        schedule_service,
        "_arm",
        lambda ex_id, inject_id, delay: armed.append((ex_id, inject_id, delay)),
    )
    await client.post(
        f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
        headers=AUTH(facilitator_token),
    )
    await client.post(
        f"/api/exercises/{exercise.id}/responses",
        json={"inject_id": detect.id, "content": "Investigating."},
        headers=AUTH(participant_token),
    )

    assert armed == [(exercise.id, escalate.id, 0.0)]


async def test_a_response_does_not_arm_the_branch_the_team_did_not_take(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """Re-arming follows the cursor, so it can only ever arm the chosen branch."""
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

    assert containment.id in schedule_service._scheduled[exercise.id]
    assert spread.id not in schedule_service._scheduled[exercise.id]


async def test_rearming_does_not_replace_a_live_timer(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
):
    """A second response must not cancel-and-replace a timer that is already armed.

    arm_inject_schedule cancels before it arms, and cancel_inject_schedule only refuses to
    cancel the *calling* task. So a blind re-arm could kill a worker mid-release — between
    its commit and its dispatch — leaving an inject released in the database with no frame
    and no triggered comms. The deadline cannot have moved, so the armed task must survive
    untouched.
    """
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")

    await client.post(
        f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
        headers=AUTH(facilitator_token),
    )
    await client.post(
        f"/api/exercises/{exercise.id}/responses",
        json={"inject_id": detect.id, "content": "Investigating."},
        headers=AUTH(participant_token),
    )
    timer = schedule_service._scheduled[exercise.id][escalate.id]

    # A second cursor advance over the same node — here, the same team replaying its way
    # through the exercise via a fresh response on the next inject — re-runs the arming.
    await schedule_service.arm_cursor_reached_injects(session, exercise)

    assert schedule_service._scheduled[exercise.id][escalate.id] is timer
    assert not timer.task.cancelled()


async def test_a_response_landing_mid_deferral_still_leaves_a_timer(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
    monkeypatch,
):
    """Both sides must not stand down at once, or the inject is stranded after all.

    The registry entry outlives the worker's decision to defer, so there is a window where
    a response can commit, see a live-looking timer, and skip arming — while that worker's
    gate query had already read the *pre-commit* cursor and is about to defer and vanish.
    Neither side arms, and the inject sits pending forever: #218 again, just narrower.

    Drive the interleaving precisely by blocking the worker inside the gate, advancing the
    cursor, and running the arm handler while it is still registered.
    """
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=0)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")
    await client.post(
        f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
        headers=AUTH(facilitator_token),
    )

    in_gate = asyncio.Event()
    resume_gate = asyncio.Event()
    real_gate = progression_service.release_is_allowed

    async def blocking_gate(session_, inject, *, scheduled=False):
        allowed = await real_gate(session_, inject, scheduled=scheduled)
        if inject.id == escalate.id:
            # The answer is now fixed on the pre-response cursor — exactly the stale read.
            in_gate.set()
            await resume_gate.wait()
        return allowed

    monkeypatch.setattr(progression_service, "release_is_allowed", blocking_gate)
    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )

    schedule_service._arm(exercise.id, escalate.id, 0)
    worker = schedule_service._scheduled[exercise.id][escalate.id].task
    await asyncio.wait_for(in_gate.wait(), timeout=5)

    # The response commits and dispatches (arming the cursor-reached injects as it goes)
    # while the worker sits blocked on its stale answer.
    await submit_response(
        session,
        inject_id=detect.id,
        exercise_id=exercise.id,
        user_id=participant.id,
        content="Investigating.",
        group_id="it_ops",
    )

    # Spy the re-arm rather than letting it run: a second delay-0 worker would re-enter this
    # test's session while it is still in use (see the _CtxSession note above).
    rearmed: list[tuple[int, int, float]] = []
    monkeypatch.setattr(
        schedule_service,
        "_arm",
        lambda ex_id, inject_id, delay: rearmed.append((ex_id, inject_id, delay)),
    )
    resume_gate.set()
    await asyncio.wait_for(worker, timeout=5)

    # The worker took the news the response left it and re-armed, instead of both sides
    # standing down. Delay 0, because the offset has long since passed.
    assert rearmed == [(exercise.id, escalate.id, 0.0)]


async def test_scheduled_release_of_an_unreferenced_node_survives_the_first_response(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
    monkeypatch,
):
    """A timed node nothing links to fires on its clock, first response or not.

    No cursor will *ever* point at an orphan, so re-arming cannot save it — the cursor lock
    has to let a *scheduled* release through instead. It still refuses a manual one, which
    test_responses.py::test_unlinked_inject_is_releasable_only_before_the_first_response
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
                release_at_minutes=40,
            ),
        ],
        start_inject_id="detect",
    )
    scenario = await create_scenario(session, definition=definition, created_by=facilitator.id)
    exercise = await create_exercise(
        session, scenario_id=scenario.id, title="Parallel Ex", created_by=facilitator.id
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id, group_id="it_ops")
    exercise = await transition_state(session, exercise, ExerciseState.active)
    detect = await _inject_by_node(session, exercise.id, "detect")
    press_call = await _inject_by_node(session, exercise.id, "press_call")

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

    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )
    await schedule_service._release_when_due(exercise.id, press_call.id, 0)

    # The worker shares this session's identity map, so release_inject's refresh lands on
    # the instance we already hold.
    assert press_call.state == InjectState.released


async def test_scheduled_release_fires_once_the_team_has_reached_the_inject(
    client: AsyncClient,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    participant_token: str,
    facilitator_token: str,
    monkeypatch,
):
    """The same schedule does fire once a response has advanced the cursor onto it."""
    exercise = await _linear_scheduled_exercise(session, facilitator, participant, offset=30)
    detect = await _inject_by_node(session, exercise.id, "detect")
    escalate = await _inject_by_node(session, exercise.id, "escalate")

    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/injects/{detect.id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/responses",
            json={"inject_id": detect.id, "content": "Investigating."},
            headers={"Authorization": f"Bearer {participant_token}"},
        )
    ).status_code == 201

    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )
    await schedule_service._release_when_due(exercise.id, escalate.id, 0)

    assert escalate.state == InjectState.released


# ── Graceful shutdown drains armed timers (#250) ───────────────────────────────


async def test_shutdown_cancels_a_still_sleeping_timer(
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    """A timer still in its sleep is cancelled outright on shutdown (restart re-arms it).

    cancel_all_schedules empties the registry and returns the worker so the caller can
    await its settling; the worker resolves as cancelled without ever touching the DB.
    """
    ex = await _make_exercise(session, facilitator, participant, offset=5, active=True)
    inject = await _first_inject(session, ex.id)

    schedule_service._arm(ex.id, inject.id, 3600)  # long sleep — nowhere near due
    worker = schedule_service._scheduled[ex.id][inject.id].task
    await asyncio.sleep(0)  # let the worker reach its sleep

    pending = schedule_service.cancel_all_schedules()

    assert worker in pending
    assert not schedule_service._scheduled  # registry emptied
    assert not schedule_service._scheduled_comms
    await background.drain(collect_extra=lambda: pending)
    assert worker.cancelled()
    assert inject.state == InjectState.pending


async def test_shutdown_lets_a_mid_release_worker_finish(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
    monkeypatch,
):
    """A worker past its sleep is drained, not killed between its commit and dispatch.

    This is the shutdown face of the released-with-no-frame window #218 closed: once a
    scheduled worker has entered its critical section, cancel_all_schedules must leave it
    running — no cancellation even requested — and background.drain must let it commit and
    broadcast its ``inject_released`` frame before shutdown proceeds (#250).
    """
    ex = await _make_exercise(session, facilitator, participant, offset=5, active=True)
    inject = await _first_inject(session, ex.id)

    monkeypatch.setattr(
        schedule_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )

    in_gate = asyncio.Event()
    resume = asyncio.Event()
    real_gate = progression_service.release_is_allowed

    async def blocking_gate(session_, inject_, *, scheduled=False):
        # Answer the gate, then park the worker *inside* its critical section (it has
        # already marked itself releasing) so shutdown races it exactly mid-release.
        allowed = await real_gate(session_, inject_, scheduled=scheduled)
        in_gate.set()
        await resume.wait()
        return allowed

    monkeypatch.setattr(progression_service, "release_is_allowed", blocking_gate)

    async with aconnect_ws(
        f"/ws/exercises/{ex.id}",
        client,
        headers={
            "origin": "http://testserver",
            "cookie": f"access_token={facilitator_token}",
        },
    ) as ws:
        schedule_service._arm(ex.id, inject.id, 0)
        worker = schedule_service._scheduled[ex.id][inject.id].task
        await asyncio.wait_for(in_gate.wait(), timeout=5)

        pending = schedule_service.cancel_all_schedules()
        assert worker in pending
        assert worker.cancelling() == 0  # left running, not even a cancel requested
        assert not schedule_service._scheduled  # registry still emptied

        resume.set()
        # Re-feed the already-collected worker each round (the registry is cleared, so a
        # bare cancel_all_schedules would no longer surface it) and let the drain await it.
        await background.drain(collect_extra=lambda: pending)
        msg = await asyncio.wait_for(ws.receive_json(), timeout=5)

    assert worker.done() and worker.exception() is None
    assert msg["type"] == "inject_released"
    assert msg["payload"]["id"] == inject.id
    assert msg["payload"]["state"] == "released"

    session.expire_all()
    refreshed = await session.get(Inject, inject.id)
    assert refreshed.state == InjectState.released
