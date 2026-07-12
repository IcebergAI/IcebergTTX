"""Pacing: exercise clock + scheduled inject release (#116)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from httpx_ws import aconnect_ws
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseState
from app.models.inject import Inject, InjectState
from app.models.user import User
from app.schemas.scenario_json import InjectNode, ScenarioDefinition
from app.services import schedule_service
from app.services.exercise_service import (
    create_exercise,
    enrol_member,
    transition_state,
)
from app.services.scenario_service import create_scenario

AUTH = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731


@pytest_asyncio.fixture(autouse=True)
async def _clear_schedules():
    """Cancel any timers a test armed so sleeping tasks don't leak between tests."""
    yield
    for ex_id in list(schedule_service._scheduled.keys()):
        schedule_service.cancel_exercise_schedules(ex_id)


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
