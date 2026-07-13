"""Database-backed OIDC routing with environment-only client secrets."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import (
    AUTH_MODE_BOTH,
    AUTH_MODE_LOCAL,
    AUTH_MODE_OIDC,
    OIDCProviderConfig,
    _parse_role_map,
    settings,
)
from app.models.oidc_settings import OIDCSettings

_SINGLETON_ID = 1
AUTH_MODES = (AUTH_MODE_LOCAL, AUTH_MODE_OIDC, AUTH_MODE_BOTH)
PROVIDER_KEYS = ("entra", "authentik", "auth0", "okta")
EDITABLE_FIELDS = (
    "auth_mode",
    "oidc_redirect_base_url",
    "oidc_entra_enabled",
    "oidc_entra_client_id",
    "oidc_entra_tenant_id",
    "oidc_entra_scopes",
    "oidc_entra_role_claim",
    "oidc_entra_role_map",
    "oidc_authentik_enabled",
    "oidc_authentik_client_id",
    "oidc_authentik_base_url",
    "oidc_authentik_app_slug",
    "oidc_authentik_scopes",
    "oidc_authentik_role_claim",
    "oidc_authentik_role_map",
    "oidc_auth0_enabled",
    "oidc_auth0_client_id",
    "oidc_auth0_domain",
    "oidc_auth0_scopes",
    "oidc_auth0_role_claim",
    "oidc_auth0_role_map",
    "oidc_okta_enabled",
    "oidc_okta_client_id",
    "oidc_okta_domain",
    "oidc_okta_auth_server",
    "oidc_okta_scopes",
    "oidc_okta_role_claim",
    "oidc_okta_role_map",
)


@dataclass(frozen=True)
class OIDCRuntimeConfig:
    auth_mode: str
    oidc_redirect_base_url: str
    oidc_entra_enabled: bool
    oidc_entra_client_id: str
    oidc_entra_tenant_id: str
    oidc_entra_scopes: str
    oidc_entra_role_claim: str
    oidc_entra_role_map: str
    oidc_authentik_enabled: bool
    oidc_authentik_client_id: str
    oidc_authentik_base_url: str
    oidc_authentik_app_slug: str
    oidc_authentik_scopes: str
    oidc_authentik_role_claim: str
    oidc_authentik_role_map: str
    oidc_auth0_enabled: bool
    oidc_auth0_client_id: str
    oidc_auth0_domain: str
    oidc_auth0_scopes: str
    oidc_auth0_role_claim: str
    oidc_auth0_role_map: str
    oidc_okta_enabled: bool
    oidc_okta_client_id: str
    oidc_okta_domain: str
    oidc_okta_auth_server: str
    oidc_okta_scopes: str
    oidc_okta_role_claim: str
    oidc_okta_role_map: str

    @property
    def local_auth_enabled(self) -> bool:
        return self.auth_mode in (AUTH_MODE_LOCAL, AUTH_MODE_BOTH)

    @property
    def oidc_auth_enabled(self) -> bool:
        return self.auth_mode in (AUTH_MODE_OIDC, AUTH_MODE_BOTH)

    def enabled_providers(self) -> list[OIDCProviderConfig]:
        if not self.oidc_auth_enabled:
            return []
        providers: list[OIDCProviderConfig] = []
        if self.oidc_entra_enabled:
            authority = (
                "https://login.microsoftonline.com/"
                f"{self.oidc_entra_tenant_id}/v2.0"
            )
            providers.append(
                OIDCProviderConfig(
                    key="entra",
                    display_name="Microsoft Entra ID",
                    client_id=self.oidc_entra_client_id,
                    client_secret=settings.oidc_entra_client_secret,
                    metadata_url=f"{authority}/.well-known/openid-configuration",
                    issuer=authority,
                    scopes=self.oidc_entra_scopes,
                    role_claim=self.oidc_entra_role_claim,
                    role_map=_parse_role_map(self.oidc_entra_role_map),
                )
            )
        if self.oidc_authentik_enabled:
            base = self.oidc_authentik_base_url.rstrip("/")
            providers.append(
                OIDCProviderConfig(
                    key="authentik",
                    display_name="Authentik",
                    client_id=self.oidc_authentik_client_id,
                    client_secret=settings.oidc_authentik_client_secret,
                    metadata_url=(
                        f"{base}/application/o/{self.oidc_authentik_app_slug}/"
                        ".well-known/openid-configuration"
                    ),
                    scopes=self.oidc_authentik_scopes,
                    role_claim=self.oidc_authentik_role_claim,
                    role_map=_parse_role_map(self.oidc_authentik_role_map),
                )
            )
        if self.oidc_auth0_enabled:
            domain = self.oidc_auth0_domain.rstrip("/")
            providers.append(
                OIDCProviderConfig(
                    key="auth0",
                    display_name="Auth0",
                    client_id=self.oidc_auth0_client_id,
                    client_secret=settings.oidc_auth0_client_secret,
                    metadata_url=f"https://{domain}/.well-known/openid-configuration",
                    scopes=self.oidc_auth0_scopes,
                    role_claim=self.oidc_auth0_role_claim,
                    role_map=_parse_role_map(self.oidc_auth0_role_map),
                )
            )
        if self.oidc_okta_enabled:
            domain = self.oidc_okta_domain.rstrip("/")
            server = self.oidc_okta_auth_server.strip("/")
            path = f"/oauth2/{server}" if server else ""
            providers.append(
                OIDCProviderConfig(
                    key="okta",
                    display_name="Okta",
                    client_id=self.oidc_okta_client_id,
                    client_secret=settings.oidc_okta_client_secret,
                    metadata_url=(
                        f"https://{domain}{path}/.well-known/openid-configuration"
                    ),
                    scopes=self.oidc_okta_scopes,
                    role_claim=self.oidc_okta_role_claim,
                    role_map=_parse_role_map(self.oidc_okta_role_map),
                )
            )
        return providers


def _env_config() -> OIDCRuntimeConfig:
    return OIDCRuntimeConfig(
        **{field: getattr(settings, field) for field in EDITABLE_FIELDS}
    )


def _to_config(row: OIDCSettings) -> OIDCRuntimeConfig:
    return OIDCRuntimeConfig(**{field: getattr(row, field) for field in EDITABLE_FIELDS})


_config: OIDCRuntimeConfig | None = None


def get_config() -> OIDCRuntimeConfig:
    return _config or _env_config()


def set_config(config: OIDCRuntimeConfig | None) -> None:
    global _config
    _config = config


def client_secret_status() -> dict[str, bool]:
    return {
        key: bool(getattr(settings, f"oidc_{key}_client_secret"))
        for key in PROVIDER_KEYS
    }


def _valid_absolute_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _validate_role_map(provider: str, raw: str) -> None:
    if not raw.strip():
        return
    parsed = _parse_role_map(raw)
    pairs = [pair.strip() for pair in raw.split(",") if pair.strip()]
    allowed_roles = {"participant", "observer", "facilitator"}
    if (
        len(parsed) != len(pairs)
        or any("=" not in pair for pair in pairs)
        or any(role not in allowed_roles for role in parsed.values())
    ):
        raise ValueError(
            f"{provider} role map must use group=participant|observer|facilitator pairs"
        )


def validate_config(config: OIDCRuntimeConfig) -> None:
    if config.auth_mode not in AUTH_MODES:
        raise ValueError("Authentication mode must be local, oidc, or both")
    if config.oidc_redirect_base_url and not _valid_absolute_url(
        config.oidc_redirect_base_url
    ):
        raise ValueError("OIDC redirect base URL must be an absolute http(s) URL")
    for key in PROVIDER_KEYS:
        _validate_role_map(key, getattr(config, f"oidc_{key}_role_map"))
    if not config.oidc_auth_enabled:
        return

    secrets = client_secret_status()
    requirements = {
        "entra": (
            config.oidc_entra_client_id,
            config.oidc_entra_tenant_id,
        ),
        "authentik": (
            config.oidc_authentik_client_id,
            config.oidc_authentik_base_url,
            config.oidc_authentik_app_slug,
        ),
        "auth0": (config.oidc_auth0_client_id, config.oidc_auth0_domain),
        "okta": (config.oidc_okta_client_id, config.oidc_okta_domain),
    }
    enabled = []
    for key in PROVIDER_KEYS:
        if not getattr(config, f"oidc_{key}_enabled"):
            continue
        enabled.append(key)
        if not all(value.strip() for value in requirements[key]):
            raise ValueError(f"{key} is enabled but its non-secret configuration is incomplete")
        if not secrets[key]:
            raise ValueError(
                f"OIDC_{key.upper()}_CLIENT_SECRET is not set in the environment"
            )
    if config.auth_mode == AUTH_MODE_OIDC and not enabled:
        raise ValueError(
            "OIDC-only mode requires at least one complete enabled provider; "
            "local login was kept on"
        )


async def get_settings(session: AsyncSession) -> OIDCSettings:
    row = await session.get(OIDCSettings, _SINGLETON_ID)
    if row is None:
        candidate = _env_config()
        validate_config(candidate)
        row = OIDCSettings(
            id=_SINGLETON_ID,
            **{field: getattr(candidate, field) for field in EDITABLE_FIELDS},
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


async def update_settings(session: AsyncSession, changes: dict[str, Any]) -> OIDCSettings:
    row = await get_settings(session)
    current = _to_config(row)
    candidate = OIDCRuntimeConfig(
        **{
            field: changes.get(field, getattr(current, field))
            for field in EDITABLE_FIELDS
        }
    )
    validate_config(candidate)
    for field in EDITABLE_FIELDS:
        setattr(row, field, getattr(candidate, field))
    row.updated_at = datetime.now(UTC)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    set_config(_to_config(row))
    from app.services.oidc import service as oidc_service

    oidc_service.reset_registration()
    return row


async def refresh_cache(session: AsyncSession) -> None:
    row = await get_settings(session)
    config = _to_config(row)
    validate_config(config)
    set_config(config)
    from app.services.oidc import service as oidc_service

    oidc_service.reset_registration()
