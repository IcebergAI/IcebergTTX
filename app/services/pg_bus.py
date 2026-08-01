"""The cross-replica message bus, carried by Postgres LISTEN/NOTIFY (#213).

Everything this app has to tell its *other* replicas goes through here. There is no
Redis and no broker: the database we already run is the bus, which keeps the hardened
single-container deployment story intact.

The reason for choosing NOTIFY over a broker is the transactional boundary. ``NOTIFY``
issued inside a transaction is delivered only if that transaction COMMITs, and is
discarded if it rolls back — natively, by Postgres. That is precisely the guarantee
``domain_events`` hand-builds for the local process, so publishing from *inside* the
unit of work extends it across replicas for free. Publish after the commit instead and
you reintroduce the window where the state changed but the announcement was lost.

So the rule for every publisher here is the same as ``record``'s:

    **Publish inside the transaction, never after it.**

The payload is a *descriptor*, not a rendered message: NOTIFY caps a payload at
8000 bytes, and inject/communication content would blow past that. A descriptor names
what happened and the ids to read it back with; each replica re-reads the committed
rows and builds its own frames. Handlers already read the database by design, so this
costs nothing architecturally — and it sidesteps the fact that domain events carry ORM
objects, which cannot cross a process boundary at all.

Delivery is at-most-once and reaches only currently-connected subscribers. That is the
same guarantee Redis pub/sub offers, and it is the one the frontend already copes with:
clients refetch on WebSocket reconnect.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)


# ── Channels ──────────────────────────────────────────────────────────────────

CHANNEL_EVENTS = "ttx_events"
"""Domain-event descriptors and WebSocket control messages."""

CHANNEL_CONFIG = "ttx_config"
"""Runtime-config invalidation, by scope."""


# ── This process ──────────────────────────────────────────────────────────────

ORIGIN = uuid4().hex
"""Identifies this replica for the lifetime of the process.

Stamped on every descriptor so a listener can tell its own publications apart from a
peer's. The WebSocket relay uses it to skip descriptors this process already projected
in-band (see ``ws_relay``); config invalidation deliberately does not skip, because a
re-read of a row it just wrote is idempotent and one less branch to get wrong.
"""

# Postgres rejects a NOTIFY payload over 8000 bytes with an error that would abort the
# publishing transaction — i.e. a payload bug would roll back the state change that
# occasioned it. Descriptors are ids and short strings and land nowhere near this, so
# tripping it means someone started putting content on the bus; fail loudly and locally.
PAYLOAD_LIMIT_BYTES = 7000


def envelope(**fields: Any) -> dict[str, Any]:
    """Stamp a descriptor with the bus version and this replica's origin."""
    return {"v": 1, "origin": ORIGIN, **fields}


def is_own(descriptor: dict[str, Any]) -> bool:
    """True if this process published the descriptor."""
    return descriptor.get("origin") == ORIGIN


def encode(descriptor: dict[str, Any]) -> str:
    body = json.dumps(descriptor, separators=(",", ":"))
    size = len(body.encode("utf-8"))
    if size > PAYLOAD_LIMIT_BYTES:
        raise ValueError(
            f"NOTIFY payload is {size} bytes, over the {PAYLOAD_LIMIT_BYTES}-byte bus "
            "limit. Descriptors carry ids, not content — put the content in a row and "
            "let each replica read it back."
        )
    return body


def decode(payload: str) -> dict[str, Any] | None:
    """Parse a received payload, or None if it is not a descriptor we understand."""
    try:
        descriptor = json.loads(payload)
    except ValueError:
        logger.warning("discarding malformed bus payload (%d bytes)", len(payload))
        return None
    if not isinstance(descriptor, dict):
        logger.warning("discarding non-object bus payload of type %s", type(descriptor).__name__)
        return None
    if descriptor.get("v") != 1:
        # A rolling deploy briefly runs two versions against one database. An unknown
        # version is a newer replica talking, not corruption: skip it quietly rather
        # than crashing the listener that still has to serve this replica's clients.
        logger.debug("skipping bus payload with unsupported version %r", descriptor.get("v"))
        return None
    return descriptor


# ── Publishing ────────────────────────────────────────────────────────────────


async def publish(session: AsyncSession, channel: str, descriptor: dict[str, Any]) -> None:
    """Queue a NOTIFY on the caller's transaction.

    Nothing is sent here: Postgres holds the notification until the surrounding
    transaction commits, and drops it if that transaction rolls back. Call this
    *before* the commit that makes the announcement true.
    """
    # Issued on the session's own connection, which is what binds the notification to
    # the caller's transaction rather than to some other pooled one.
    connection = await session.connection()
    await connection.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": channel, "payload": encode(descriptor)},
    )


async def publish_config_changed(session: AsyncSession, scope: str) -> None:
    """Tell every replica that a runtime-config scope was rewritten.

    Scopes name a settings service (``proxy``, ``siem``, ``email``, ``general``,
    ``llm``, ``oidc``) rather than the changed fields: the receiver re-reads the
    singleton row, so the diff is never in flight and can never go stale.
    """
    await publish(session, CHANNEL_CONFIG, envelope(scope=scope))
