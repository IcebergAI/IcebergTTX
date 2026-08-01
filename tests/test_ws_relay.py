"""Projecting the events other replicas recorded (#213).

The origin replica projects in-band through ``dispatch``; a peer learns about the same
event over the bus and projects it here. These pin that the two paths agree, that the
peer path skips work that must happen only once, and — the flagship — that an event
reaches the bus if and only if its transaction committed.
"""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine, make_asyncpg_dsn
from app.services import domain_events as de
from app.services import pg_bus, ws_relay


class _CtxSession:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def relay_session(monkeypatch, session):
    """Give the relay the test's transactional session instead of its own."""
    monkeypatch.setattr(ws_relay, "AsyncSession", lambda *a, **k: _CtxSession(session))
    return session


def _foreign(descriptor: dict) -> dict:
    """The same descriptor, as it would arrive from another replica."""
    return {**descriptor, "origin": "another-replica"}


# ── The wire format ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "event",
    [
        de.ExerciseStateChanged(exercise_id=1, transition_id=2, action="start"),
        de.InjectReleased(exercise_id=1, inject_id=2),
        de.InjectUpdated(exercise_id=1, inject_id=2),
        de.InjectCommentCreated(exercise_id=1, comment_id=2),
        de.ResponseSubmitted(exercise_id=1, response_id=2),
        de.CommunicationCreated(exercise_id=1, communication_id=2),
        de.ResponseAssessed(exercise_id=1, response_id=2, assessment_id=3),
        de.InjectSuggested(exercise_id=1, suggested_id=2),
        de.SummaryGenerated(exercise_id=1, summary_id=2),
    ],
    ids=lambda ev: type(ev).__name__,
)
def test_every_event_survives_the_round_trip(event):
    """Through JSON and back: what a peer reconstructs must equal what was recorded, or
    it projects a different frame than the origin did."""
    descriptor = de.to_descriptor(event)

    assert de.from_descriptor(pg_bus.decode(pg_bus.encode(descriptor))) == event


def test_every_event_has_a_wire_name():
    """An event with no name cannot cross the bus, so it would project on the recording
    replica and silently nowhere else."""
    declared = {
        cls
        for cls in vars(de).values()
        if isinstance(cls, type) and issubclass(cls, de.DomainEvent) and cls is not de.DomainEvent
    }

    assert declared == set(de.EVENT_NAMES)


def test_wire_names_are_unique():
    assert len(set(de.EVENT_NAMES.values())) == len(de.EVENT_NAMES)


def test_an_unknown_event_name_is_skipped():
    """A newer replica publishing an event this build has never heard of."""
    assert de.from_descriptor(pg_bus.envelope(event="teleportation_completed")) is None


def test_a_descriptor_missing_fields_is_skipped():
    assert de.from_descriptor(pg_bus.envelope(event="inject_released")) is None


# ── The relay ─────────────────────────────────────────────────────────────────


async def test_a_peers_event_is_projected_here(relay_session, monkeypatch):
    seen: list[de.DomainEvent] = []
    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [_collect(seen)])
    event = de.SummaryGenerated(exercise_id=7, summary_id=9)

    await ws_relay.handle(_foreign(de.to_descriptor(event)))

    assert seen == [event]


async def test_our_own_event_is_not_projected_twice(relay_session, monkeypatch):
    """``dispatch`` already ran it on this replica, at the end of the unit of work that
    recorded it. Relaying it back would double every frame a client receives."""
    seen: list[de.DomainEvent] = []
    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [_collect(seen)])

    await ws_relay.handle(de.to_descriptor(de.SummaryGenerated(exercise_id=7, summary_id=9)))

    assert seen == []


async def test_handlers_that_act_do_not_run_on_a_peer(relay_session, monkeypatch):
    """The distinction ``subscribe_local`` exists for: rendering a frame on every replica
    is the point, but arming a timer on every replica fires it once per replica."""
    projected: list[de.DomainEvent] = []
    acted: list[de.DomainEvent] = []
    projection = _collect(projected)
    action = _collect(acted)
    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [projection, action])
    monkeypatch.setattr(de, "_local_only", {action})

    event = de.SummaryGenerated(exercise_id=7, summary_id=9)

    await ws_relay.handle(_foreign(de.to_descriptor(event)))

    assert len(projected) == 1
    assert acted == []


async def test_the_recording_replica_runs_both(session, monkeypatch):
    projected: list[de.DomainEvent] = []
    acted: list[de.DomainEvent] = []
    projection = _collect(projected)
    action = _collect(acted)
    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [projection, action])
    monkeypatch.setattr(de, "_local_only", {action})

    await de.project(session, de.SummaryGenerated(exercise_id=7, summary_id=9), local=True)

    assert len(projected) == 1
    assert len(acted) == 1


async def test_a_failing_projection_does_not_stop_its_siblings(relay_session, monkeypatch):
    seen: list[de.DomainEvent] = []

    async def boom(_session, _ev):
        raise RuntimeError("projection failed")

    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [boom, _collect(seen)])

    event = de.SummaryGenerated(exercise_id=7, summary_id=9)

    await ws_relay.handle(_foreign(de.to_descriptor(event)))

    assert len(seen) == 1


async def test_a_peers_role_downgrade_closes_this_replicas_sockets(monkeypatch):
    """Authorization is per-connection state on whichever replica holds the socket, so a
    downgrade has to reach all of them (#25)."""
    from app.services import ws_manager

    closed: list[int] = []

    async def spy(user_id, code=4003):
        closed.append(user_id)

    monkeypatch.setattr(ws_manager.manager, "close_user_connections", spy)

    await ws_relay.handle(
        _foreign(pg_bus.envelope(event=ws_relay.EVENT_USER_CONNECTIONS_CLOSE, user_id=42))
    )

    assert closed == [42]


async def test_our_own_downgrade_is_not_relayed_back(monkeypatch):
    """The originating replica closed its own sockets inline, before the announcement."""
    from app.services import ws_manager

    closed: list[int] = []

    async def spy(user_id, code=4003):
        closed.append(user_id)

    monkeypatch.setattr(ws_manager.manager, "close_user_connections", spy)

    await ws_relay.handle(
        pg_bus.envelope(event=ws_relay.EVENT_USER_CONNECTIONS_CLOSE, user_id=42)
    )

    assert closed == []


# ── The transactional guarantee, end to end ───────────────────────────────────


async def test_an_event_reaches_the_bus_only_if_its_transaction_commits():
    """The whole reason the publication is issued from ``before_commit``.

    Against real commits: the per-test savepoint session never truly commits, so it can
    never deliver a NOTIFY — which is exactly why the origin replica keeps projecting
    in-band rather than round-tripping through the bus.
    """
    received: asyncio.Queue[str] = asyncio.Queue()
    connection = await asyncpg.connect(make_asyncpg_dsn(os.environ["DATABASE_URL"]))
    doomed = uuid4().int % 100000
    survivor = doomed + 1
    try:
        await connection.add_listener(
            pg_bus.CHANNEL_EVENTS, lambda c, pid, ch, payload: received.put_nowait(payload)
        )

        async with AsyncSession(engine, expire_on_commit=False) as rolled_back:
            await rolled_back.begin()
            de.record(rolled_back, de.SummaryGenerated(exercise_id=1, summary_id=doomed))
            await rolled_back.rollback()

        async with AsyncSession(engine, expire_on_commit=False) as committed:
            await committed.begin()
            de.record(committed, de.SummaryGenerated(exercise_id=1, summary_id=survivor))
            await committed.commit()
            # Nothing dispatches these; drain so the buffer does not look leaked.
            de.buffer_for(committed).committed.clear()

        delivered = de.from_descriptor(
            pg_bus.decode(await asyncio.wait_for(received.get(), timeout=5))
        )
        assert delivered == de.SummaryGenerated(exercise_id=1, summary_id=survivor)
        assert received.empty()
    finally:
        await connection.close(timeout=5)


def _collect(sink: list):
    async def handler(_session, ev):
        sink.append(ev)

    return handler
