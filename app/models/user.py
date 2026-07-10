from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel

# Local (email + password) accounts carry this as their auth_provider. OIDC
# identities store the provider key (e.g. "entra", "authentik") instead (#25).
LOCAL_AUTH_PROVIDER = "local"


class UserRole(StrEnum):
    facilitator = "facilitator"
    participant = "participant"
    observer = "observer"


class User(SQLModel, table=True):
    # An external identity is unique per (auth_provider, subject). Postgres treats
    # NULL subjects as distinct, so local rows (subject=NULL) never collide (#25).
    __table_args__ = (
        UniqueConstraint("auth_provider", "subject", name="uq_user_provider_subject"),
    )

    id: int | None = Field(default=None, primary_key=True)
    email: str = Field(unique=True, index=True)
    display_name: str
    # Nullable: OIDC-provisioned accounts have no local password (#25). A local
    # account that later links an OIDC identity keeps its hash.
    hashed_password: str | None = Field(default=None)
    role: UserRole = Field(default=UserRole.participant)
    team: str | None = None
    is_active: bool = Field(default=True)
    # Global-admin flag (#12): retains cross-facilitator access to every exercise
    # for oversight/support, bypassing per-exercise ownership scoping. Assigned
    # out-of-band (seeded / DB), like the facilitator role — never via registration.
    is_admin: bool = Field(default=False)
    # Temporary-password flag (#66): set when an admin resets this user's password,
    # cleared when the user next changes it via PUT /auth/me. Enforced UI-side at
    # login (the frontend redirects to /settings until the password is changed).
    must_change_password: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), sa_type=DateTime(timezone=True)
    )
    # Token revocation cutoff (#14): tokens issued (iat) before this instant are
    # rejected in get_current_user. Bumped on password change to invalidate all
    # previously-issued tokens ("change password to kick out an attacker").
    token_valid_after: datetime | None = Field(
        default=None, sa_type=DateTime(timezone=True)
    )
    # External-identity provenance (#25). auth_provider is "local" for
    # email+password accounts, or an OIDC provider key ("entra"/"authentik") once
    # a provider identity is linked/provisioned. subject is the IdP's stable `sub`
    # claim (NULL for local-only accounts); the pair is unique (see __table_args__).
    auth_provider: str = Field(default=LOCAL_AUTH_PROVIDER, index=True)
    subject: str | None = Field(default=None)
