"""OIDC flow wiring + user provisioning (#25).

Holds the process-wide Authlib ``OAuth`` registry (built from the enabled provider
configs at startup) and implements the match/link/JIT-provision policy that turns a
validated ``OIDCIdentity`` into a local ``User``. Everything downstream (session
token, role checks) is unchanged: we mint the same local token the password flow
mints, keyed on the user's email.
"""

from __future__ import annotations

from authlib.integrations.starlette_client import OAuth
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import OIDCProviderConfig, settings
from app.models.user import LOCAL_AUTH_PROVIDER, User, UserRole
from app.services import audit_service
from app.services.oidc import auth0 as _auth0  # noqa: F401 - registers adapter
from app.services.oidc import authentik as _authentik  # noqa: F401 - registers adapter
from app.services.oidc import entra as _entra  # noqa: F401 - registers adapter
from app.services.oidc import okta as _okta  # noqa: F401 - registers adapter
from app.services.oidc.base import OIDCIdentity

# Process-wide registry, populated by register_providers() at startup.
oauth = OAuth()
_registered: dict[str, OIDCProviderConfig] = {}
_registration_done = False


class OIDCProvisionError(Exception):
    """A provisioning decision that denies login (disabled account, collision)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def register_providers() -> list[OIDCProviderConfig]:
    """Register every enabled provider with Authlib. Idempotent per process.

    Returns the list of registered provider configs (also used to render the
    login-page buttons).
    """
    global _registration_done
    configs = settings.enabled_oidc_providers()
    for cfg in configs:
        if cfg.key in _registered:
            continue
        oauth.register(
            name=cfg.key,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            server_metadata_url=cfg.metadata_url,
            client_kwargs={
                "scope": cfg.scopes,
                "code_challenge_method": "S256",
            },
        )
        _registered[cfg.key] = cfg
    _registration_done = True
    return configs


def ensure_registered() -> None:
    """Register providers once, lazily. Safe to call on every request — this is
    what makes the routes work whether or not the lifespan startup ran (e.g. under
    the test transport)."""
    if not _registration_done:
        register_providers()


def registered_providers() -> list[OIDCProviderConfig]:
    """Configs registered this process, in a stable order (for the login buttons)."""
    return list(_registered.values())


def get_provider(key: str) -> OIDCProviderConfig | None:
    return _registered.get(key)


def map_role(cfg: OIDCProviderConfig, identity: OIDCIdentity) -> UserRole:
    """Resolve a JIT user's role from the provider's group→role allowlist.

    Defaults to participant (no self-elevation, #8). Only groups explicitly present
    in the configured allowlist can elevate; unknown role strings are ignored.
    """
    for group in identity.groups:
        mapped = cfg.role_map.get(group)
        if mapped:
            try:
                return UserRole(mapped)
            except ValueError:
                continue
    return UserRole.participant


async def provision_oidc_user(
    session: AsyncSession, *, cfg: OIDCProviderConfig, identity: OIDCIdentity
) -> tuple[User, bool]:
    """Resolve or create the local User for a validated OIDC identity.

    Returns ``(user, created)``. Raises OIDCProvisionError for denials (disabled
    account, or an unverified email colliding with a local account).

    Match order:
      1. (auth_provider, subject) — a returning OIDC user.
      2. verified email → link the identity onto the existing local row.
      3. unverified email colliding with a local account → deny.
      4. otherwise JIT-create a participant.
    """
    # 1. Returning OIDC identity.
    existing = (
        await session.exec(
            select(User).where(User.auth_provider == cfg.key, User.subject == identity.subject)
        )
    ).first()
    if existing is not None:
        if not existing.is_active:
            raise OIDCProvisionError("account disabled")
        return existing, False

    # 2/3. Email collision with an existing account.
    by_email = (
        await session.exec(select(User).where(User.email == identity.email))
    ).first()
    if by_email is not None:
        if not by_email.is_active:
            raise OIDCProvisionError("account disabled")
        # Only ever auto-link an *unlinked local* account. If this email already
        # belongs to a different external identity (step 1 didn't match it, so the
        # (provider, subject) differs), refuse — otherwise a second IdP, or the same
        # IdP with a changed `sub`, could take over the account by asserting the same
        # email (cross-provider account takeover).
        if by_email.subject is not None or by_email.auth_provider != LOCAL_AUTH_PROVIDER:
            raise OIDCProvisionError("identity conflict")
        if not identity.email_verified:
            raise OIDCProvisionError("unverified email collision")
        # Link: attach the provider identity to the existing local row, preserving
        # its role / is_admin.
        by_email.auth_provider = cfg.key
        by_email.subject = identity.subject
        session.add(by_email)
        await session.commit()
        await session.refresh(by_email)
        audit_service.emit(
            "auth.oidc_link",
            actor_id=by_email.id,
            actor_email=by_email.email,
            actor_role=by_email.role.value,
            target_type="user",
            target_id=by_email.id,
            reason=f"provider={cfg.key}",
        )
        return by_email, False

    # 4. JIT create.
    user = User(
        email=identity.email,
        display_name=identity.display_name or identity.email,
        hashed_password=None,
        role=map_role(cfg, identity),
        auth_provider=cfg.key,
        subject=identity.subject,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    audit_service.emit(
        "auth.jit_provision",
        actor_id=user.id,
        actor_email=user.email,
        actor_role=user.role.value,
        target_type="user",
        target_id=user.id,
        reason=f"provider={cfg.key}",
    )
    return user, True
