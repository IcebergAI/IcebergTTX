"""Global outbound-proxy resolution for httpx calls (#97).

One pure helper, :func:`resolve`, turns the admin-managed ``ProxySettings`` row (or
its cached snapshot) into the httpx keyword arguments (``proxy`` / ``trust_env``)
for a given target URL. Three modes:

- ``SYSTEM``   — ``trust_env=True``: httpx honours the environment proxy vars
  (``HTTP(S)_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY``). The default, and what the app
  already did implicitly before this feature (httpx defaults ``trust_env=True``),
  so an existing deployment relying on an ambient ``HTTPS_PROXY`` keeps working.
- ``NONE``     — ``trust_env=False``, no proxy: always a direct connection.
- ``EXPLICIT`` — ``trust_env=False``, route through the configured ``proxy_url``
  unless the target host matches the no-proxy exclusion list (standard ``NO_PROXY``
  semantics), in which case go direct.

The bypass decision is made here, per target URL, because httpx clients take a
single ``proxy`` (no per-host ``mounts``). Proxy credentials are a secret: they live
only in the environment and are injected into the proxy URL at call time, never
persisted on the DB row and never rendered back to an admin.

Routing is read from an in-memory snapshot (``get_config``/``set_config``) refreshed
at startup and whenever an admin saves — the sync ``audit_service.emit`` path has no
DB session, exactly as with ``siem_service`` (single-process, like ``ws_manager``).

Callers must treat a ``None`` config as "feature not loaded" and pass no kwargs at
all, so behaviour is byte-for-byte what it was before this feature:

    pk = proxy.resolve(cfg, url) if cfg is not None else {}
    httpx.AsyncClient(..., **pk)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import ip_address, ip_network
from urllib.parse import quote, urlsplit, urlunsplit

from app.config import settings


class ProxyMode(StrEnum):
    """How outbound HTTP connections are routed."""

    NONE = "NONE"
    SYSTEM = "SYSTEM"
    EXPLICIT = "EXPLICIT"


@dataclass(frozen=True)
class ProxyConfig:
    """Immutable snapshot of the routing config (no secret — creds are env-only)."""

    mode: str = ProxyMode.SYSTEM.value
    proxy_url: str = ""
    no_proxy: str = ""


# In-memory routing snapshot. ``None`` means "not loaded" — callers then pass no
# httpx kwargs, preserving pre-feature behaviour.
_config: ProxyConfig | None = None


def get_config() -> ProxyConfig | None:
    return _config


def set_config(cfg: ProxyConfig | None) -> None:
    global _config
    _config = cfg


def resolve(cfg: ProxyConfig, url: str) -> dict:
    """httpx kwargs (``proxy`` / ``trust_env``) for an outbound request to ``url``.

    ``cfg`` may be a :class:`ProxyConfig` snapshot or a ``ProxySettings`` row — only
    ``.mode`` / ``.proxy_url`` / ``.no_proxy`` are read.
    """
    direct = {"trust_env": False, "proxy": None}
    try:
        mode = ProxyMode(str(cfg.mode).upper())
    except ValueError:
        mode = ProxyMode.SYSTEM
    match mode:
        case ProxyMode.SYSTEM:
            # Only trust_env — adding `proxy: None` here would *override* the
            # environment proxy that this mode exists to honour.
            return {"trust_env": True}
        case ProxyMode.EXPLICIT if cfg.proxy_url:
            host = urlsplit(url).hostname
            if _should_bypass(host, _parse_no_proxy(cfg.no_proxy)):
                return direct
            return {"trust_env": False, "proxy": _with_credentials(cfg.proxy_url)}
        case ProxyMode.NONE | ProxyMode.EXPLICIT:
            # NONE, or EXPLICIT with no proxy URL configured → direct connection.
            return direct
    return direct  # pragma: no cover — exhaustive above


def resolve_kwargs(url: str) -> dict:
    """``resolve`` against the cached snapshot; ``{}`` when the cache is unloaded."""
    cfg = get_config()
    return resolve(cfg, url) if cfg is not None else {}


def _parse_no_proxy(value: str) -> list[str]:
    return [t.strip() for t in (value or "").split(",") if t.strip()]


def _should_bypass(host: str | None, entries: list[str]) -> bool:
    """Standard NO_PROXY match: ``*`` bypasses all; a CIDR matches an IP host in
    range; a domain matches the host and its subdomains; an IP/host matches
    exactly. An unknown host goes direct."""
    if not host:
        return True
    host = host.lower()
    try:
        ip = ip_address(host)
    except ValueError:
        ip = None
    for entry in entries:
        if entry == "*":
            return True
        if "/" in entry and ip is not None:
            try:
                if ip in ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
            continue
        target = entry.lower().lstrip(".")
        if host == target or host.endswith("." + target):
            return True
    return False


def _with_credentials(proxy_url: str) -> str:
    """Inject the env-only proxy credentials into the proxy URL's userinfo."""
    if not settings.proxy_username:
        return proxy_url
    parsed = urlsplit(proxy_url)
    if not parsed.hostname:
        return proxy_url
    userinfo = quote(settings.proxy_username, safe="")
    if settings.proxy_password:
        userinfo += ":" + quote(settings.proxy_password, safe="")
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{userinfo}@{parsed.hostname}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
