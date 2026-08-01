"""Keep every replica's runtime-config caches honest (#213).

Six settings singletons (proxy, SIEM, email, general policy, LLM routing, OIDC) are
cached in process memory so hot paths — ``audit_service.emit`` fanning out to SIEM, an
LLM call resolving its provider — never touch the database. Before multi-replica that
cache was invalidated by the very request that wrote the row, which was sufficient
because there was only ever one process. With more than one, an admin's save leaves
every *other* replica serving the old config indefinitely.

So a save publishes its scope on the bus and each replica re-reads the row. Scopes name
the settings service rather than the changed fields deliberately: shipping a diff would
put config values (some sensitive) on the wire and let a lost message leave a replica
permanently skewed. Re-reading is idempotent, so the publishing replica handling its own
notification is harmless — and one less branch than skipping it.

Missed invalidations, the unavoidable cost of at-most-once delivery, are recovered by
``refresh_all`` on every (re)connect.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import pg_bus

logger = logging.getLogger(__name__)

Refresher = Callable[[AsyncSession], Awaitable[None]]


async def _refresh_proxy(session: AsyncSession) -> None:
    from app.services import proxy_settings_service

    await proxy_settings_service.refresh_cache_and_dependents(session)


async def _refresh_siem(session: AsyncSession) -> None:
    from app.services import audit_settings_service

    await audit_settings_service.refresh_cache(session)


async def _refresh_email(session: AsyncSession) -> None:
    from app.services import email_settings_service

    await email_settings_service.refresh_cache(session)


async def _refresh_general(session: AsyncSession) -> None:
    # Flows on into rate_limit.apply_config via set_config, so limiter thresholds
    # follow the same path as the policy they belong to.
    from app.services import general_settings_service

    await general_settings_service.refresh_cache(session)


async def _refresh_llm(session: AsyncSession) -> None:
    from app.services import llm_settings_service

    await llm_settings_service.refresh_cache(session)


async def _refresh_oidc(session: AsyncSession) -> None:
    from app.services import oidc_settings_service

    await oidc_settings_service.refresh_cache(session)


# Ordered, and the order is the lifespan's: proxy must be reloaded before OIDC, because
# OIDC registration bakes the resolved proxy into each Authlib client (#97). Python dicts
# preserve insertion order, so refresh_all inherits that guarantee for free.
SCOPES: dict[str, Refresher] = {
    "proxy": _refresh_proxy,
    "siem": _refresh_siem,
    "email": _refresh_email,
    "general": _refresh_general,
    "llm": _refresh_llm,
    "oidc": _refresh_oidc,
}


def engine_for_session() -> Any:
    """Resolve the engine at call time.

    The test suite rebinds ``app.database.engine`` after import, so capturing it in a
    module global here would leave handlers pointed at the un-patched engine.
    """
    from app.database import engine

    return engine


async def refresh_scope(scope: str) -> None:
    """Re-read one settings singleton into its cache."""
    refresher = SCOPES.get(scope)
    if refresher is None:
        # A newer replica publishing a scope this build does not know about, during a
        # rolling deploy. Nothing here can act on it; the peer that owns it will.
        logger.debug("ignoring config invalidation for unknown scope %r", scope)
        return
    async with AsyncSession(engine_for_session()) as session:
        await refresher(session)
    logger.info("refreshed %s config from the bus", scope)


async def refresh_all() -> None:
    """Re-read every scope. The reconnect recovery path: notifications published while
    this replica was disconnected are gone, so assume all of them were."""
    for scope in SCOPES:
        try:
            await refresh_scope(scope)
        except Exception:
            # Best-effort, exactly like the startup loaders: one unreadable singleton
            # must not stop the other five from being refreshed.
            logger.exception("failed to refresh %s config on bus reconnect", scope)


_first_connect = True


async def on_bus_connected() -> None:
    """Recover the invalidations missed while this replica was disconnected.

    Skipped on the *first* connect: the startup loaders have just read every scope, so
    there is nothing to recover, and refreshing OIDC here would drop the Authlib
    registration the lifespan performed moments earlier.
    """
    global _first_connect
    if _first_connect:
        _first_connect = False
        return
    logger.info("bus reconnected; re-reading every config scope")
    await refresh_all()


async def handle(descriptor: dict[str, Any]) -> None:
    """Bus handler for ``ttx_config``."""
    scope = descriptor.get("scope")
    if not isinstance(scope, str):
        logger.warning("config invalidation without a scope: %r", descriptor)
        return
    await refresh_scope(scope)


_installed = False


def install() -> None:
    """Wire this module into the bus. Idempotent — the lifespan may run more than once
    in a process (the test client mounts the app repeatedly)."""
    global _installed
    if _installed:
        return
    _installed = True
    from app.services.pg_listener import listener

    listener.register(pg_bus.CHANNEL_CONFIG, handle)
    listener.on_reconnect(pg_bus.CHANNEL_CONFIG, on_bus_connected)
