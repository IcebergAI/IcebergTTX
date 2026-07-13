"""Admin runtime OIDC configuration API; client secrets remain environment-only."""

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session
from app.dependencies import require_admin
from app.models.user import User
from app.services import audit_service, oidc_settings_service

router = APIRouter(prefix="/oidc/settings", tags=["oidc settings"])
AdminDep = Annotated[User, Depends(require_admin)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


class OIDCSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auth_mode: str | None = None
    oidc_redirect_base_url: str | None = None
    oidc_entra_enabled: bool | None = None
    oidc_entra_client_id: str | None = None
    oidc_entra_tenant_id: str | None = None
    oidc_entra_scopes: str | None = None
    oidc_entra_role_claim: str | None = None
    oidc_entra_role_map: str | None = None
    oidc_authentik_enabled: bool | None = None
    oidc_authentik_client_id: str | None = None
    oidc_authentik_base_url: str | None = None
    oidc_authentik_app_slug: str | None = None
    oidc_authentik_scopes: str | None = None
    oidc_authentik_role_claim: str | None = None
    oidc_authentik_role_map: str | None = None
    oidc_auth0_enabled: bool | None = None
    oidc_auth0_client_id: str | None = None
    oidc_auth0_domain: str | None = None
    oidc_auth0_scopes: str | None = None
    oidc_auth0_role_claim: str | None = None
    oidc_auth0_role_map: str | None = None
    oidc_okta_enabled: bool | None = None
    oidc_okta_client_id: str | None = None
    oidc_okta_domain: str | None = None
    oidc_okta_auth_server: str | None = None
    oidc_okta_scopes: str | None = None
    oidc_okta_role_claim: str | None = None
    oidc_okta_role_map: str | None = None

    @field_validator("*")
    @classmethod
    def _strip_strings(cls, value):  # noqa: ANN001
        return value.strip() if isinstance(value, str) else value

    @field_validator("auth_mode")
    @classmethod
    def _normalize_mode(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @field_validator("oidc_redirect_base_url", "oidc_authentik_base_url")
    @classmethod
    def _absolute_url(cls, value: str | None) -> str | None:
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("URL must be an absolute http(s) URL")
        return value.rstrip("/")

    @field_validator("oidc_auth0_domain", "oidc_okta_domain")
    @classmethod
    def _domain_only(cls, value: str | None) -> str | None:
        if not value:
            return value
        if "://" in value or "/" in value or not urlsplit(f"https://{value}").hostname:
            raise ValueError("Domain must contain a hostname only")
        return value.rstrip("/")


async def _public_settings(session: AsyncSession) -> dict:
    row = await oidc_settings_service.get_settings(session)
    return {
        **{
            field: getattr(row, field)
            for field in oidc_settings_service.EDITABLE_FIELDS
        },
        "client_secrets_set": oidc_settings_service.client_secret_status(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.get("")
async def get_oidc_settings(_: AdminDep, session: SessionDep) -> dict:
    return await _public_settings(session)


@router.put("")
async def update_oidc_settings(
    body: OIDCSettingsUpdate, current_user: AdminDep, session: SessionDep
) -> dict:
    changes = body.model_dump(exclude_unset=True)
    try:
        row = await oidc_settings_service.update_settings(session, changes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    audit_service.emit(
        "oidc.settings_updated",
        actor=current_user,
        target_type="oidc_settings",
        target_id=row.id,
        reason="fields=" + ",".join(sorted(changes)),
        severity="warning",
    )
    return await _public_settings(session)
