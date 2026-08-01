"""Project the domain events *other* replicas recorded (#213).

The listener hands this module descriptors off the ``ttx_events`` channel; it rebuilds
the event and runs the same ``ws_projector`` handlers the recording replica ran, against
its own sockets. Nothing here knows what a frame looks like — that stays in one place.

Descriptors this replica published are skipped. The recording replica has already
projected them in-band, at the end of the unit of work that produced them, through
``dispatch``. Relaying them back would double every local frame; more importantly,
keeping the local path in-band is what lets the savepoint-isolated test suite exercise
the real projection end to end, since a transaction that never truly commits can never
deliver a NOTIFY.

So the two paths differ only in who invokes them:

    origin replica:  commit -> dispatch(session)      -> project(local=True)
    peer replica:    NOTIFY -> handle(descriptor)     -> project(local=False)

``local=False`` is what keeps handlers that *act* — arming a timer — from running once
per replica. See ``subscribe_local`` in ``domain_events``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import domain_events, pg_bus

logger = logging.getLogger(__name__)

EVENT_USER_CONNECTIONS_CLOSE = "user_connections_close"
"""Not a domain event: a direct instruction to drop one user's sockets.

Authorization is per-connection state held by whichever replica terminates the socket,
so a role downgrade has to reach all of them (#25). The originating replica closes its
own connections inline, which is why this is skipped by origin like everything else.
"""


async def handle(descriptor: dict[str, Any]) -> None:
    """Bus handler for ``ttx_events``."""
    if pg_bus.is_own(descriptor):
        return
    if descriptor.get("event") == EVENT_USER_CONNECTIONS_CLOSE:
        await _close_user_connections(descriptor)
        return
    ev = domain_events.from_descriptor(descriptor)
    if ev is None:
        return
    async with AsyncSession(engine_for_session()) as session:
        await domain_events.project(session, ev, local=False)


async def _close_user_connections(descriptor: dict[str, Any]) -> None:
    from app.services.ws_manager import manager

    user_id = descriptor.get("user_id")
    if not isinstance(user_id, int):
        logger.warning("user_connections_close without a user id: %r", descriptor)
        return
    await manager.close_user_connections(user_id)


async def publish_user_connections_close(session: AsyncSession, user_id: int) -> None:
    """Tell every other replica to drop this user's sockets, if this commit lands."""
    await pg_bus.publish(
        session,
        pg_bus.CHANNEL_EVENTS,
        pg_bus.envelope(event=EVENT_USER_CONNECTIONS_CLOSE, user_id=user_id),
    )


def engine_for_session() -> Any:
    """Resolve the engine at call time — the test suite rebinds it after import."""
    from app.database import engine

    return engine


_installed = False


def install() -> None:
    """Wire this module into the bus. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    from app.services.pg_listener import listener

    listener.register(pg_bus.CHANNEL_EVENTS, handle)
