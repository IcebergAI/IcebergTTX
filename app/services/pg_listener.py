"""This replica's subscription to the Postgres bus (#213).

One dedicated connection per process, held open for the lifetime of the app, LISTENing
on every channel something has registered a handler for. It is deliberately *not* a
connection from the SQLAlchemy pool: LISTEN is connection-state, so a pooled checkout
would either be pinned out of the pool forever or be recycled out from under the
subscription — a failure mode that presents as "frames stopped arriving hours later".

Two tasks, not one:

* ``_maintain_connection`` owns the socket — connect, subscribe, keep alive, and on any
  failure reconnect with capped exponential backoff.
* ``_consume`` owns the work — drain the queue and run handlers.

Splitting them is what keeps a slow handler from blocking asyncpg's protocol callbacks
(the callback itself only does a ``put_nowait``), and what lets a handler crash without
taking the subscription down with it.

Delivery is at-most-once. Notifications published while this replica was disconnected
are gone — nothing replays them. Anything that cannot tolerate that registers an
``on_reconnect`` hook and re-reads authoritative state instead, which is what config
invalidation does; WebSocket frames simply rely on clients refetching after their own
reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

from app.services import pg_bus

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
ReconnectHook = Callable[[], Awaitable[None]]

# Backoff bounds for a database that has gone away (failover, restart, network blip).
_BACKOFF_INITIAL_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 30.0
# A silent LISTEN connection is indistinguishable from a wedged one, and a NAT or proxy
# will happily drop an idle socket without telling either end. Probe periodically so a
# dead connection is discovered in bounded time rather than at the next publication.
_KEEPALIVE_SECONDS = 30.0
# Far above the real rate (a busy exercise produces tens of messages a minute). Reaching
# this means the consumer is wedged, so say so rather than growing without bound.
_QUEUE_MAXSIZE = 1000


class Listener:
    """Owns the LISTEN connection and dispatches what arrives on it."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = {}
        self._hooks: dict[str, list[ReconnectHook]] = {}
        self._queue: asyncio.Queue[tuple[str, str]] | None = None
        self._connection: Any = None
        self._connection_task: asyncio.Task | None = None
        self._consumer_task: asyncio.Task | None = None
        self._connected = False

    # ── Registration (before start) ───────────────────────────────────────────

    def register(self, channel: str, handler: Handler) -> None:
        """Subscribe ``handler`` to ``channel``. Registering a channel makes the
        connection LISTEN on it at its next (re)connect."""
        self._handlers.setdefault(channel, []).append(handler)

    def on_reconnect(self, channel: str, hook: ReconnectHook) -> None:
        """Run ``hook`` whenever the subscription to ``channel`` is (re)established.

        The recovery seam for at-most-once delivery: anything missed while
        disconnected has to be recovered by re-reading state, not by replay.
        """
        self._hooks.setdefault(channel, []).append(hook)

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin listening. Returns immediately; the first connect happens in the task.

        Startup never blocks on the bus: a database that is briefly unreachable must not
        stop this replica from serving requests it can already serve.
        """
        if self._connection_task is not None:
            return
        if not self._handlers:
            logger.debug("no bus handlers registered; listener not started")
            return
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._consumer_task = asyncio.create_task(self._consume())
        self._connection_task = asyncio.create_task(self._maintain_connection())

    async def stop(self) -> None:
        """Stop listening and close the connection.

        Must run before ``engine.dispose()`` in teardown — not because they share a
        connection (they do not), but because a handler this cancels mid-flight is
        holding a pooled one.
        """
        for task in (self._connection_task, self._consumer_task):
            if task is not None:
                task.cancel()
        for task in (self._connection_task, self._consumer_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._connection_task = None
        self._consumer_task = None
        await self._close_connection()
        self._queue = None

    # ── The connection ────────────────────────────────────────────────────────

    async def _maintain_connection(self) -> None:
        delay = _BACKOFF_INITIAL_SECONDS
        while True:
            try:
                await self._connect_and_subscribe()
                delay = _BACKOFF_INITIAL_SECONDS
                await self._hold_open()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._connected = False
                logger.exception("bus listener connection failed; reconnecting in %.1fs", delay)
                await self._close_connection()
                await self._sleep_before_retry(delay)
                delay = min(delay * 2, _BACKOFF_MAX_SECONDS)

    async def _sleep_before_retry(self, delay: float) -> None:
        # Jitter so a fleet of replicas that lost the database together does not
        # reconnect in lockstep and stampede it as it comes back.
        await asyncio.sleep(delay * (0.5 + random.random()))  # nosec B311 - spreads retries, not a secret

    async def _connect_and_subscribe(self) -> None:
        from app.config import settings
        from app.database import make_asyncpg_dsn

        self._connection = await asyncpg.connect(make_asyncpg_dsn(settings.database_url))
        for channel in self._handlers:
            await self._connection.add_listener(channel, self._on_notify)
        self._connected = True
        logger.info("bus listener subscribed to %s", ", ".join(sorted(self._handlers)))
        await self._run_reconnect_hooks()

    async def _run_reconnect_hooks(self) -> None:
        for channel, hooks in self._hooks.items():
            for hook in hooks:
                try:
                    await hook()
                except Exception:
                    logger.exception("bus reconnect hook for %s failed", channel)

    async def _hold_open(self) -> None:
        """Keep the subscription alive until the connection dies."""
        while True:
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            # Raises if the connection has gone away, which returns control to the
            # reconnect loop. asyncpg notices some drops on its own; this notices the
            # rest (silently dropped sockets) without waiting for a publication.
            await self._connection.execute("SELECT 1")

    async def _close_connection(self) -> None:
        connection, self._connection = self._connection, None
        self._connected = False
        if connection is None:
            return
        with contextlib.suppress(Exception):
            await connection.close(timeout=5)

    # ── Delivery ──────────────────────────────────────────────────────────────

    def _on_notify(self, connection: Any, pid: int, channel: str, payload: str) -> None:
        """asyncpg's callback. Sync and non-blocking on purpose — it runs inside the
        protocol's read loop, so the only thing it may do is hand work off."""
        if self._queue is None:
            return
        try:
            self._queue.put_nowait((channel, payload))
        except asyncio.QueueFull:
            logger.error(
                "bus queue full (%d messages); dropping a %s notification. The consumer "
                "is not keeping up or is wedged.",
                _QUEUE_MAXSIZE,
                channel,
            )

    async def _consume(self) -> None:
        assert self._queue is not None
        while True:
            channel, payload = await self._queue.get()
            try:
                await self._dispatch(channel, payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let one bad message end the consumer: this task is the only
                # thing delivering cross-replica work, and it has to outlive any
                # individual handler's bad day.
                logger.exception("bus consumer failed on a %s notification", channel)

    async def _dispatch(self, channel: str, payload: str) -> None:
        descriptor = pg_bus.decode(payload)
        if descriptor is None:
            return
        for handler in self._handlers.get(channel, []):
            try:
                await handler(descriptor)
            except Exception:
                logger.exception(
                    "bus handler %s failed for a %s notification",
                    getattr(handler, "__qualname__", handler),
                    channel,
                )


listener = Listener()
"""The process-wide listener. Register handlers at import; start it in the lifespan."""
