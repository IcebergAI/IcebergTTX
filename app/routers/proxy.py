"""Admin outbound-proxy config API (#97).

All routes are admin-only (``require_admin`` — the real ``User.is_admin`` column).
Edits the runtime routing config; the proxy credentials are env-only and are never
accepted, returned, or logged here — including by the connectivity test, whose
result string must never contain the resolved (credential-bearing) proxy URL.
"""

import logging
from typing import Annotated, cast
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.database import get_session
from app.dependencies import require_admin
from app.models.proxy_settings import ProxySettings
from app.models.user import User
from app.services import audit_service, proxy, proxy_settings_service, siem_service

logger = logging.getLogger("iceberg_ttx")

router = APIRouter(prefix="/proxy", tags=["proxy"])

AdminDep = Annotated[User, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]

_TEST_TIMEOUT = 10.0


class ProxySettingsUpdate(BaseModel):
    """Whitelisted, validated patch to the routing config (never credentials)."""

    mode: str | None = None
    proxy_url: str | None = None
    no_proxy: str | None = None

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return proxy.ProxyMode(v.strip().upper()).value
        except ValueError:
            return proxy.ProxyMode.SYSTEM.value

    @field_validator("proxy_url", "no_proxy")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v is not None else None


class ProxyTestRequest(BaseModel):
    """Names one of the server-side egress targets — never a URL.

    Accepting an arbitrary URL here would make this an SSRF primitive: the app would
    fetch whatever an admin named and return the status code, turning the endpoint
    into an oracle for internal hosts. The admin picks from the hosts the app is
    already configured to call.
    """

    target: str


def egress_targets() -> dict[str, str]:
    """The URLs this app actually dials, keyed by a stable label.

    The only URLs the connectivity test may reach. Built fresh each call so an
    admin who changes the LLM/SIEM/OIDC config sees the new targets.
    """
    targets: dict[str, str] = {}

    from app.services.llm.service import active_provider

    provider = active_provider()
    api_base = getattr(provider, "api_base", None)
    if provider is not None and callable(api_base):
        url = api_base()
        if isinstance(url, str):
            targets[f"LLM ({provider.key})"] = url

    endpoint = siem_service.get_config().http_endpoint
    if endpoint:
        targets["SIEM (http sink)"] = endpoint

    from app.services import oidc_settings_service

    for cfg in oidc_settings_service.get_config().enabled_providers():
        targets[f"OIDC ({cfg.key})"] = cfg.metadata_url

    return targets


def _scrub(text: str) -> str:
    """Redact any credential the exception text may have echoed back.

    httpx exception messages can embed the proxy URL, and the resolved URL carries
    the env-only credentials — so never let the raw string reach a log line.
    """
    for secret in (settings.proxy_password, settings.proxy_username):
        if secret:
            text = text.replace(secret, "***")
            text = text.replace(quote(secret, safe=""), "***")
    return text


@router.get("/settings")
async def get_proxy_settings(_: AdminDep, session: SessionDep) -> ProxySettings:
    return await proxy_settings_service.get_settings(session)


@router.put("/settings")
async def update_proxy_settings(
    body: ProxySettingsUpdate,
    current_user: AdminDep,
    session: SessionDep,
) -> ProxySettings:
    row = await proxy_settings_service.update_settings(session, body.model_dump(exclude_unset=True))
    audit_service.emit(
        "proxy.settings_updated",
        actor=current_user,
        target_type="proxy_settings",
        target_id=row.id,
        reason=f"mode={row.mode}",
        severity="warning",
    )
    return row


@router.get("/targets")
async def list_targets(_: AdminDep) -> dict:
    """Labels of the egress targets the connectivity test may dial (no URLs)."""
    return {"targets": sorted(egress_targets())}


@router.post("/test")
async def test_proxy(
    body: ProxyTestRequest,
    current_user: AdminDep,
    session: SessionDep,
) -> dict:
    """Dial one of the configured egress targets through the saved routing config.

    Returns ``ok: HTTP <status>`` or ``error: <ExceptionClass>``. The exception's
    *message* is logged server-side but never returned — it can name internal hosts
    and can embed the resolved proxy URL, which carries the env-only credentials.
    """
    await proxy_settings_service.refresh_cache(session)
    row = await proxy_settings_service.get_settings(session)

    # The URL is chosen from a server-built map, never taken from the request body.
    url = egress_targets().get(body.target)
    if url is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Unknown target. Configure an LLM provider, SIEM http sink, "
                "or OIDC provider first."
            ),
        )

    try:
        async with httpx.AsyncClient(
            timeout=_TEST_TIMEOUT,
            follow_redirects=True,
            **proxy.resolve(cast(proxy.ProxyConfig, row), url),
        ) as client:
            resp = await client.get(url)
        result = f"ok: HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001 - surfaced to the admin, never raised
        logger.warning("proxy connectivity test failed: %s", _scrub(str(exc)))
        result = f"error: {type(exc).__name__}"
    audit_service.emit(
        "proxy.test",
        actor=current_user,
        target_type="proxy_settings",
        reason=f"{body.target} -> {result}",
        severity="info",
    )
    return {"result": result}
