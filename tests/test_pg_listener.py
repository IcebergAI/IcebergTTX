"""The per-replica LISTEN connection: routing, resilience, reconnect (#213).

Driven against a fresh ``Listener`` with a stubbed connection rather than the process
singleton — the singleton is never started under the test transport (the lifespan does
not run), and these assertions are about the machinery, not a live subscription.
"""

import asyncio

import pytest

from app.services import pg_bus, pg_listener
from app.services.pg_listener import Listener


class _FakeConnection:
    """Stands in for an asyncpg connection."""

    def __init__(self, *, fail_on_keepalive: bool = False):
        self.channels: list[str] = []
        self.closed = False
        self.fail_on_keepalive = fail_on_keepalive
        self.callback = None

    async def add_listener(self, channel, callback):
        self.channels.append(channel)
        self.callback = callback

    async def execute(self, _sql):
        if self.fail_on_keepalive:
            raise ConnectionResetError("connection went away")

    async def close(self, timeout=None):
        self.closed = True


async def test_descriptors_are_routed_to_their_channels_handlers():
    listener = Listener()
    events: list[dict] = []
    config: list[dict] = []
    listener.register(pg_bus.CHANNEL_EVENTS, lambda d: _append(events, d))
    listener.register(pg_bus.CHANNEL_CONFIG, lambda d: _append(config, d))

    await listener._dispatch(pg_bus.CHANNEL_CONFIG, pg_bus.encode(pg_bus.envelope(scope="llm")))

    assert config == [pg_bus.envelope(scope="llm")]
    assert events == []


async def test_every_handler_on_a_channel_receives_the_descriptor():
    listener = Listener()
    first: list[dict] = []
    second: list[dict] = []
    listener.register(pg_bus.CHANNEL_EVENTS, lambda d: _append(first, d))
    listener.register(pg_bus.CHANNEL_EVENTS, lambda d: _append(second, d))

    await listener._dispatch(pg_bus.CHANNEL_EVENTS, pg_bus.encode(pg_bus.envelope(a=1)))

    assert len(first) == len(second) == 1


async def test_a_failing_handler_neither_propagates_nor_blocks_its_siblings():
    """One projection's bad day must not cost the others their message — the same rule
    ``domain_events.dispatch`` applies locally."""
    listener = Listener()
    survivors: list[dict] = []

    async def explode(_descriptor):
        raise RuntimeError("handler is broken")

    listener.register(pg_bus.CHANNEL_EVENTS, explode)
    listener.register(pg_bus.CHANNEL_EVENTS, lambda d: _append(survivors, d))

    await listener._dispatch(pg_bus.CHANNEL_EVENTS, pg_bus.encode(pg_bus.envelope(a=1)))

    assert len(survivors) == 1


async def test_malformed_payload_reaches_no_handler():
    listener = Listener()
    seen: list[dict] = []
    listener.register(pg_bus.CHANNEL_EVENTS, lambda d: _append(seen, d))

    await listener._dispatch(pg_bus.CHANNEL_EVENTS, "{not json")

    assert seen == []


async def test_notify_callback_hands_off_without_blocking():
    """asyncpg calls this inside its protocol read loop, so it may only enqueue."""
    listener = Listener()
    listener._queue = asyncio.Queue(maxsize=10)

    listener._on_notify(None, 1234, pg_bus.CHANNEL_EVENTS, "payload")

    assert listener._queue.get_nowait() == (pg_bus.CHANNEL_EVENTS, "payload")


async def test_a_full_queue_drops_rather_than_grows(caplog):
    listener = Listener()
    listener._queue = asyncio.Queue(maxsize=1)
    listener._on_notify(None, 1, pg_bus.CHANNEL_EVENTS, "first")

    listener._on_notify(None, 1, pg_bus.CHANNEL_EVENTS, "second")

    assert listener._queue.qsize() == 1
    assert "bus queue full" in caplog.text


async def test_subscribes_every_registered_channel_and_runs_reconnect_hooks(monkeypatch):
    listener = Listener()
    listener.register(pg_bus.CHANNEL_EVENTS, lambda d: _append([], d))
    listener.register(pg_bus.CHANNEL_CONFIG, lambda d: _append([], d))
    connected: list[str] = []
    listener.on_reconnect(pg_bus.CHANNEL_CONFIG, lambda: _record(connected, "hook"))
    connection = _FakeConnection()
    monkeypatch.setattr(pg_listener, "asyncpg", _FakeAsyncpg(connection), raising=False)

    await listener._connect_and_subscribe()

    assert sorted(connection.channels) == sorted([pg_bus.CHANNEL_EVENTS, pg_bus.CHANNEL_CONFIG])
    assert connected == ["hook"]
    assert listener.is_connected


async def test_a_failing_reconnect_hook_does_not_abort_the_subscription(monkeypatch):
    listener = Listener()
    listener.register(pg_bus.CHANNEL_CONFIG, lambda d: _append([], d))

    async def explode():
        raise RuntimeError("refresh failed")

    listener.on_reconnect(pg_bus.CHANNEL_CONFIG, explode)
    monkeypatch.setattr(pg_listener, "asyncpg", _FakeAsyncpg(_FakeConnection()), raising=False)

    await listener._connect_and_subscribe()

    assert listener.is_connected


async def test_reconnect_backoff_grows_exponentially_and_is_capped(monkeypatch):
    """A database that stays away must not be hammered — and a fleet that lost it
    together must not come back in lockstep, hence the jitter around each delay."""
    listener = Listener()
    delays: list[float] = []

    async def fail_to_connect():
        raise ConnectionRefusedError("no database")

    async def record(delay):
        delays.append(delay)
        if len(delays) >= 8:
            raise asyncio.CancelledError

    monkeypatch.setattr(listener, "_connect_and_subscribe", fail_to_connect)
    monkeypatch.setattr(listener, "_sleep_before_retry", record)

    with pytest.raises(asyncio.CancelledError):
        await listener._maintain_connection()

    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert delays[-1] == pg_listener._BACKOFF_MAX_SECONDS
    assert max(delays) <= pg_listener._BACKOFF_MAX_SECONDS


async def test_backoff_resets_after_a_successful_connect(monkeypatch):
    """Otherwise a replica that reconnects after a long outage keeps a 30s penalty for
    the next unrelated blip."""
    listener = Listener()
    delays: list[float] = []
    attempts = {"n": 0}

    async def connect_second_time_only():
        attempts["n"] += 1
        if attempts["n"] != 3:
            raise ConnectionRefusedError("no database")

    async def die_immediately():
        raise ConnectionResetError("dropped again")

    async def record(delay):
        delays.append(delay)
        if len(delays) >= 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(listener, "_connect_and_subscribe", connect_second_time_only)
    monkeypatch.setattr(listener, "_hold_open", die_immediately)
    monkeypatch.setattr(listener, "_sleep_before_retry", record)

    with pytest.raises(asyncio.CancelledError):
        await listener._maintain_connection()

    # Two failures (1s, 2s), then a connect, then the delay starts from 1s again.
    assert delays == [1.0, 2.0, 1.0, 2.0]


async def test_a_dead_connection_returns_control_to_the_reconnect_loop(monkeypatch):
    """A silently dropped socket is indistinguishable from an idle one until something
    is asked of it, so the keepalive is what bounds how long a replica stays deaf."""
    listener = Listener()
    monkeypatch.setattr(pg_listener, "_KEEPALIVE_SECONDS", 0.001)
    listener._connection = _FakeConnection(fail_on_keepalive=True)

    with pytest.raises(ConnectionResetError):
        await listener._hold_open()


async def test_stop_closes_the_connection_and_clears_the_tasks(monkeypatch):
    listener = Listener()
    listener.register(pg_bus.CHANNEL_CONFIG, lambda d: _append([], d))
    connection = _FakeConnection()
    monkeypatch.setattr(pg_listener, "asyncpg", _FakeAsyncpg(connection), raising=False)
    monkeypatch.setattr(pg_listener, "_KEEPALIVE_SECONDS", 3600)

    await listener.start()
    await asyncio.sleep(0)  # let the connection task reach its first await
    await listener.stop()

    assert connection.closed
    assert not listener.is_connected
    assert listener._connection_task is None
    assert listener._consumer_task is None


async def test_start_is_a_no_op_without_handlers():
    """Nothing registered means nothing to deliver; holding a connection open to say so
    would just be a connection Postgres could have had back."""
    listener = Listener()

    await listener.start()

    assert listener._connection_task is None


class _FakeAsyncpg:
    def __init__(self, connection):
        self._connection = connection

    async def connect(self, _dsn):
        return self._connection


async def _append(sink: list, descriptor: dict) -> None:
    sink.append(descriptor)


async def _record(sink: list, value: str) -> None:
    sink.append(value)
