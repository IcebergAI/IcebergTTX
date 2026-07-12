"""OIDC flow wiring + user provisioning (#25).

Holds the process-wide Authlib ``OAuth`` registry (built from the enabled provider
configs at startup) and implements the match/link/JIT-provision policy that turns a
validated ``OIDCIdentity`` into a local ``User``. Everything downstream (session
token, role checks) is unchanged: we mint the same local token the password flow
mints, keyed on the user's email.
"""

from __future__ import annotations

from datetime import UTC, datetime

from authlib.integrations.starlette_client import OAuth
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import OIDCProviderConfig, settings
from app.models.user import User, UserRole
from app.services import audit_service, proxy, ws_manager
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
        # Authlib's AsyncOAuth2Client subclasses httpx.AsyncClient, so the resolved
        # proxy kwargs reach httpx through client_kwargs (#97). The decision is baked
        # in here, at registration, against the IdP's discovery host — which is why
        # the proxy cache must be loaded before this runs, and why a proxy change
        # calls reset_registration().
        client_kwargs = {
            "scope": cfg.scopes,
            "code_challenge_method": "S256",
        }
        client_kwargs.update(proxy.resolve_kwargs(cfg.metadata_url))
        oauth.register(
            name=cfg.key,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            server_metadata_url=cfg.metadata_url,
            client_kwargs=client_kwargs,
        )
        _registered[cfg.key] = cfg
    _registration_done = True
    return configs


def reset_registration() -> None:
    """Force providers to re-register on next use (e.g. after a proxy change).

    Authlib's ``register()`` overwrites its ``_registry`` entry but ``create_client()``
    returns the **cached** ``_clients[name]``, so re-registering alone would silently
    keep serving the old client — with the old proxy. Rebinding ``oauth`` to a fresh
    registry sidesteps Authlib's internals entirely; the routers reach it as
    ``oidc_service.oauth``, so they pick up the new object.
    """
    global oauth, _registration_done
    oauth = OAuth()
    _registered.clear()
    _registration_done = False


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


async def _sync_returning_user(
    session: AsyncSession,
    *,
    cfg: OIDCProviderConfig,
    identity: OIDCIdentity,
    user: User,
) -> None:
    """Validate immutable identity scope and synchronize an IdP-managed role."""
    dirty = False
    if user.auth_tenant is not None and user.auth_tenant != identity.tenant_id:
        raise OIDCProvisionError("identity conflict")
    if user.auth_tenant is None and identity.tenant_id is not None:
        # Lazy provenance backfill for an identity created before auth_tenant was
        # added. The provider/subject match was already validated by Authlib.
        user.auth_tenant = identity.tenant_id
        dirty = True
    elif user.auth_tenant is not None and identity.tenant_id is None:
        raise OIDCProvisionError("identity conflict")

    previous_role = user.role
    if user.role_managed_by_idp and not user.is_admin:
        mapped_role = map_role(cfg, identity)
        if mapped_role != user.role:
            user.role = mapped_role
            # Invalidate sessions minted with the previous authorization state.
            # The callback issues the returning user a fresh token after this
            # transaction commits.
            user.token_valid_after = datetime.now(UTC).replace(microsecond=0)
            dirty = True

    if dirty:
        session.add(user)
        await session.commit()
        await session.refresh(user)

    if user.role != previous_role:
        audit_service.emit(
            "auth.oidc_role_sync",
            actor_id=user.id,
            actor_email=user.email,
            actor_role=user.role.value,
            target_type="user",
            target_id=user.id,
            reason=(
                f"provider={cfg.key} from={previous_role.value} to={user.role.value}"
            ),
            severity="warning",
        )
        # The role and revocation cutoff are durable before any established
        # socket is acted on. Otherwise a disconnected client could race a stale
        # HTTP session against an uncommitted downgrade.
        if user.id is not None:
            await ws_manager.manager.close_user_connections(user.id)


async def provision_oidc_user(
    session: AsyncSession, *, cfg: OIDCProviderConfig, identity: OIDCIdentity
) -> tuple[User, bool]:
    """Resolve or create the local User for a validated OIDC identity.

    Returns ``(user, created)``. Raises OIDCProvisionError for disabled accounts,
    identity conflicts, and email collisions that require an explicit link flow.

    Match order:
      1. (auth_provider, stable subject) — returning OIDC user; validate tenant
         provenance and synchronize an IdP-managed role.
      2. any email collision → deny; mutable human-readable claims never link.
      3. otherwise JIT-create an identity bound to the stable provider subject.
    """
    # 1. Returning OIDC identity.
    existing = (
        await session.exec(
            select(User)
            .where(User.auth_provider == cfg.key, User.subject == identity.subject)
            .with_for_update()
        )
    ).first()
    if existing is not None:
        if not existing.is_active:
            raise OIDCProvisionError("account disabled")
        await _sync_returning_user(
            session, cfg=cfg, identity=identity, user=existing
        )
        return existing, False

    # 2. Email is a mutable display/contact attribute, never an identity key.
    normalized_email = identity.email.strip().lower()
    by_email = (
        await session.exec(select(User).where(User.email == normalized_email))
    ).first()
    if by_email is not None:
        if not by_email.is_active:
            raise OIDCProvisionError("account disabled")
        raise OIDCProvisionError("account linking required")

    # 3. JIT create. The stable provider subject authenticates this account; an
    # unverified email may be retained as contact/display data but cannot claim an
    # existing row because every collision above is denied.
    user = User(
        email=normalized_email,
        display_name=identity.display_name or normalized_email,
        hashed_password=None,
        role=map_role(cfg, identity),
        auth_provider=cfg.key,
        subject=identity.subject,
        auth_tenant=identity.tenant_id,
        role_managed_by_idp=True,
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
