# pyright: reportArgumentType=false
from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlmodel import Field, SQLModel


class OIDCSettings(SQLModel, table=True):
    """Admin-editable OIDC configuration; client secrets are never persisted."""

    id: int | None = Field(default=None, primary_key=True)
    auth_mode: str = "both"
    oidc_redirect_base_url: str = ""

    oidc_entra_enabled: bool = False
    oidc_entra_client_id: str = ""
    oidc_entra_tenant_id: str = ""
    oidc_entra_scopes: str = "openid email profile"
    oidc_entra_role_claim: str = ""
    oidc_entra_role_map: str = ""

    oidc_authentik_enabled: bool = False
    oidc_authentik_client_id: str = ""
    oidc_authentik_base_url: str = ""
    oidc_authentik_app_slug: str = ""
    oidc_authentik_scopes: str = "openid email profile"
    oidc_authentik_role_claim: str = "groups"
    oidc_authentik_role_map: str = ""

    oidc_auth0_enabled: bool = False
    oidc_auth0_client_id: str = ""
    oidc_auth0_domain: str = ""
    oidc_auth0_scopes: str = "openid email profile"
    oidc_auth0_role_claim: str = ""
    oidc_auth0_role_map: str = ""

    oidc_okta_enabled: bool = False
    oidc_okta_client_id: str = ""
    oidc_okta_domain: str = ""
    oidc_okta_auth_server: str = ""
    oidc_okta_scopes: str = "openid email profile"
    oidc_okta_role_claim: str = "groups"
    oidc_okta_role_map: str = ""

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
