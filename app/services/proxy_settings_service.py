"""Manage the singleton ProxySettings row + the in-memory proxy config cache (#97).

The row (``id == 1``) is the admin-editable routing config; on first read it is
seeded from the ``PROXY_*`` env defaults. Every update pushes a ``ProxyConfig``
snapshot into ``proxy`` so the sync ``audit_service.emit`` → SIEM path resolves
routing without touching the DB.

A save must also invalidate the two caches that captured the *old* proxy at
construction time:

- the LLM adapters build a long-lived SDK client (with its own ``httpx.AsyncClient``)
  and ``active_provider()`` caches the provider → ``reset_provider_cache()``;
- Authlib registers each OIDC provider once, baking ``client_kwargs`` (and hence the
  proxy) into a cached client → ``reset_registration()``.

Both are lazy-imported to avoid an import cycle (llm/oidc import ``config``, which
this module also imports).
"""

from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.proxy_settings import ProxySettings
from app.services import proxy

_SINGLETON_ID = 1

# Fields an admin may change. Credentials are intentionally absent — they are
# env-only and never reach the DB. Used to whitelist PUT payloads.
EDITABLE_FIELDS = ("mode", "proxy_url", "no_proxy")


def _to_config(row: ProxySettings) -> proxy.ProxyConfig:
    return proxy.ProxyConfig(
        mode=row.mode,
        proxy_url=row.proxy_url,
        no_proxy=row.no_proxy,
    )


def _seed_mode() -> str:
    """Env-seeded mode, normalised to the enum's spelling (SYSTEM on anything odd)."""
    try:
        return proxy.ProxyMode(settings.proxy_mode.strip().upper()).value
    except ValueError:
        return proxy.ProxyMode.SYSTEM.value


async def get_settings(session: AsyncSession) -> ProxySettings:
    """Return the singleton row, seeding it from env defaults on first read."""
    row = await session.get(ProxySettings, _SINGLETON_ID)
    if row is None:
        row = ProxySettings(
            id=_SINGLETON_ID,
            mode=_seed_mode(),
            proxy_url=settings.proxy_url,
            no_proxy=settings.proxy_no_proxy,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def _invalidate_dependent_caches() -> None:
    """Drop clients that captured the previous proxy at construction time."""
    from app.services.llm.service import reset_provider_cache
    from app.services.oidc.service import reset_registration

    reset_provider_cache()
    reset_registration()


async def update_settings(session: AsyncSession, changes: dict[str, Any]) -> ProxySettings:
    """Apply a whitelisted patch to the singleton row and refresh every cache."""
    from datetime import UTC, datetime

    row = await get_settings(session)
    for key in EDITABLE_FIELDS:
        if key in changes and changes[key] is not None:
            setattr(row, key, changes[key])
    row.updated_at = datetime.now(UTC)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    proxy.set_config(_to_config(row))
    _invalidate_dependent_caches()
    return row


async def refresh_cache(session: AsyncSession) -> None:
    """Load the singleton row into the proxy in-memory cache (startup)."""
    row = await get_settings(session)
    proxy.set_config(_to_config(row))
