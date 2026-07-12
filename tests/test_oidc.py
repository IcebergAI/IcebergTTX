"""OIDC / SSO tests (#25).

The Authlib client is always stubbed — no discovery/JWKS/token network calls. The
Authentik provider is enabled for the whole suite in conftest, so the routes are
registered and ``get_provider("authentik")`` resolves.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from authlib.integrations.starlette_client import OAuthError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.bootstrap_admin import upsert_admin
from app.config import OIDCProviderConfig, settings
from app.dependencies import resolve_user_from_token
from app.models.user import User, UserRole
from app.services import audit_service
from app.services.oidc import service as oidc_service
from app.services.oidc.base import OIDCIdentity, get_adapter
from app.services.oidc.entra import EntraAdapter


def _cfg(role_map: dict[str, str] | None = None) -> OIDCProviderConfig:
    return OIDCProviderConfig(
        key="authentik",
        display_name="Authentik",
        client_id="c",
        client_secret="s",
        metadata_url="https://authentik.test/application/o/ttx/.well-known/openid-configuration",
        role_claim="groups",
        role_map=role_map or {},
    )


def _entra_cfg(role_map: dict[str, str] | None = None) -> OIDCProviderConfig:
    return OIDCProviderConfig(
        key="entra",
        display_name="Microsoft Entra ID",
        client_id="c",
        client_secret="s",
        metadata_url=(
            "https://login.microsoftonline.com/tenant-a/v2.0/"
            ".well-known/openid-configuration"
        ),
        role_claim="roles",
        role_map=role_map or {},
    )


def _identity(**overrides) -> OIDCIdentity:
    base = {
        "subject": "sub-1",
        "email": "new@sso.test",
        "email_verified": True,
        "display_name": "New User",
        "groups": [],
    }
    base.update(overrides)
    return OIDCIdentity(**base)


def _backdated_token(user: User) -> str:
    """Mint a pre-sync session whose second-precision iat is deterministic."""
    issued_at = datetime.now(UTC) - timedelta(seconds=5)
    return jwt.encode(
        {
            "sub": user.email,
            "role": user.role.value,
            "is_admin": user.is_admin,
            "iat": issued_at,
            "exp": issued_at + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm=settings.algorithm,
    )


class _FakeOIDCClient:
    """Stands in for an Authlib StarletteOAuth2App — no network."""

    def __init__(self, claims=None, error=None, redirect="https://authentik.test/authorize?x=1"):
        self._claims = claims
        self._error = error
        self._redirect = redirect

    async def authorize_redirect(self, request, redirect_uri):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(self._redirect)

    async def authorize_access_token(self, request):
        if self._error:
            raise self._error
        return {"userinfo": self._claims}


@pytest.fixture(name="patch_oidc")
def patch_oidc_fixture(monkeypatch):
    """Install a stub Authlib client for every provider lookup."""

    def _install(client: _FakeOIDCClient) -> None:
        monkeypatch.setattr(oidc_service.oauth, "create_client", lambda name: client)

    return _install


@pytest.fixture(name="audit_events")
def audit_events_fixture(monkeypatch):
    """Capture audit_service.emit calls as (action, kwargs)."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(audit_service, "emit", lambda action, **kw: events.append((action, kw)))
    return events


# --------------------------------------------------------------------------- #
# Provisioning policy (service-level)
# --------------------------------------------------------------------------- #


async def test_jit_creates_participant(session, audit_events):
    user, created = await oidc_service.provision_oidc_user(
        session, cfg=_cfg(), identity=_identity()
    )
    assert created is True
    assert user.role == UserRole.participant
    assert user.auth_provider == "authentik"
    assert user.subject == "sub-1"
    assert user.auth_tenant is None
    assert user.role_managed_by_idp is True
    assert user.hashed_password is None
    assert any(a == "auth.jit_provision" for a, _ in audit_events)


async def test_role_map_elevates(session):
    cfg = _cfg(role_map={"ttx-facilitators": "facilitator"})
    user, _ = await oidc_service.provision_oidc_user(
        session, cfg=cfg, identity=_identity(groups=["ttx-facilitators"])
    )
    assert user.role == UserRole.facilitator


async def test_returning_idp_managed_role_downgrades_after_group_removal(
    session, audit_events, monkeypatch
):
    cfg = _cfg(role_map={"ttx-facilitators": "facilitator"})
    identity = _identity(
        subject="managed-sub",
        email="managed@sso.test",
        groups=["ttx-facilitators"],
    )
    user, _ = await oidc_service.provision_oidc_user(
        session, cfg=cfg, identity=identity
    )
    assert user.role == UserRole.facilitator
    assert user.role_managed_by_idp is True
    stale_token = _backdated_token(user)

    commit_completed = False
    closed_user_ids: list[int] = []
    original_commit = session.commit

    async def tracked_commit():
        nonlocal commit_completed
        await original_commit()
        commit_completed = True

    async def close_user_connections(user_id: int):
        # Socket invalidation must happen only after the role and token cutoff
        # transaction is durable.
        assert commit_completed is True
        assert user.role == UserRole.participant
        assert user.token_valid_after is not None
        closed_user_ids.append(user_id)

    monkeypatch.setattr(session, "commit", tracked_commit)
    monkeypatch.setattr(
        oidc_service.ws_manager.manager,
        "close_user_connections",
        close_user_connections,
    )

    returned, created = await oidc_service.provision_oidc_user(
        session,
        cfg=cfg,
        identity=_identity(
            subject="managed-sub", email="managed@sso.test", groups=[]
        ),
    )
    assert created is False
    assert returned.id == user.id
    assert returned.role == UserRole.participant
    assert returned.token_valid_after is not None
    assert closed_user_ids == [user.id]
    assert await resolve_user_from_token(stale_token, session) is None
    assert any(a == "auth.oidc_role_sync" for a, _ in audit_events)


async def test_returning_current_group_member_remains_facilitator(session):
    cfg = _cfg(role_map={"ttx-facilitators": "facilitator"})
    identity = _identity(
        subject="current-member-sub",
        email="current-member@sso.test",
        groups=["ttx-facilitators"],
    )
    user, _ = await oidc_service.provision_oidc_user(
        session, cfg=cfg, identity=identity
    )

    returned, created = await oidc_service.provision_oidc_user(
        session, cfg=cfg, identity=identity
    )
    assert created is False
    assert returned.id == user.id
    assert returned.role == UserRole.facilitator
    assert returned.token_valid_after is None


async def test_returning_local_role_override_is_preserved(session):
    overridden = User(
        email="override@sso.test",
        display_name="Override",
        hashed_password=None,
        role=UserRole.facilitator,
        auth_provider="authentik",
        subject="override-sub",
        role_managed_by_idp=False,
    )
    session.add(overridden)
    await session.commit()

    returned, _ = await oidc_service.provision_oidc_user(
        session,
        cfg=_cfg(role_map={"ttx-facilitators": "facilitator"}),
        identity=_identity(
            subject="override-sub", email="override@sso.test", groups=[]
        ),
    )
    assert returned.role == UserRole.facilitator
    assert returned.role_managed_by_idp is False


async def test_returning_admin_role_is_preserved(session):
    admin = User(
        email="admin-sso@sso.test",
        display_name="Admin",
        hashed_password=None,
        role=UserRole.facilitator,
        is_admin=True,
        auth_provider="authentik",
        subject="admin-sub",
        role_managed_by_idp=True,
    )
    session.add(admin)
    await session.commit()

    returned, _ = await oidc_service.provision_oidc_user(
        session,
        cfg=_cfg(role_map={"ttx-facilitators": "facilitator"}),
        identity=_identity(subject="admin-sub", email="admin-sso@sso.test", groups=[]),
    )
    assert returned.role == UserRole.facilitator
    assert returned.is_admin is True


async def test_concurrent_local_override_cannot_be_reverted_by_oidc(monkeypatch):
    """The returning-identity lock serializes against an operator role override."""
    from app.database import engine

    suffix = uuid4().hex
    email = f"concurrent-{suffix}@sso.test"
    subject = f"concurrent-{suffix}"
    async with AsyncSession(engine, expire_on_commit=False) as seed_session:
        user = User(
            email=email,
            display_name="Concurrent Override",
            hashed_password=None,
            role=UserRole.facilitator,
            auth_provider="authentik",
            subject=subject,
            role_managed_by_idp=True,
        )
        seed_session.add(user)
        await seed_session.commit()
        await seed_session.refresh(user)
        assert user.id is not None
        user_id = user.id

    release_override = asyncio.Event()
    override_ready = asyncio.Event()
    async with (
        AsyncSession(engine, expire_on_commit=False) as override_session,
        AsyncSession(engine, expire_on_commit=False) as oidc_session,
    ):
        original_commit = override_session.commit

        async def paused_override_commit():
            # upsert_admin has acquired the row lock and marked provenance local.
            override_ready.set()
            await release_override.wait()
            await original_commit()

        monkeypatch.setattr(override_session, "commit", paused_override_commit)
        override_task = asyncio.create_task(
            upsert_admin(
                override_session,
                email=email,
                display_name=None,
                password=None,
                role=UserRole.facilitator,
                is_admin=False,
            )
        )
        await asyncio.wait_for(override_ready.wait(), timeout=2)

        oidc_task = asyncio.create_task(
            oidc_service.provision_oidc_user(
                oidc_session,
                cfg=_cfg(role_map={"ttx-facilitators": "facilitator"}),
                identity=_identity(subject=subject, email=email, groups=[]),
            )
        )
        await asyncio.sleep(0.1)
        assert oidc_task.done() is False

        release_override.set()
        await asyncio.gather(override_task, oidc_task)

    async with AsyncSession(engine, expire_on_commit=False) as verify_session:
        final = await verify_session.get(User, user_id)
        assert final is not None
        assert final.role == UserRole.facilitator
        assert final.role_managed_by_idp is False
        await verify_session.delete(final)
        await verify_session.commit()


async def test_no_self_elevation_without_map(session):
    # An unmapped provider ignores IdP-supplied "facilitator"/"admin" groups (#8).
    user, _ = await oidc_service.provision_oidc_user(
        session, cfg=_cfg(), identity=_identity(groups=["facilitator", "admin"])
    )
    assert user.role == UserRole.participant


async def test_unknown_mapped_role_is_ignored(session):
    cfg = _cfg(role_map={"g": "superuser"})
    user, _ = await oidc_service.provision_oidc_user(
        session, cfg=cfg, identity=_identity(groups=["g"])
    )
    assert user.role == UserRole.participant


async def test_verified_email_cannot_auto_link_local_privileged_account(
    session, audit_events
):
    local = User(
        email="link@sso.test",
        display_name="Local",
        hashed_password="hashed",
        role=UserRole.facilitator,
    )
    session.add(local)
    await session.commit()

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session,
            cfg=_cfg(),
            identity=_identity(subject="sub-x", email="LINK@SSO.TEST"),
        )
    assert exc.value.reason == "account linking required"
    await session.refresh(local)
    assert local.auth_provider == "local"
    assert local.subject is None
    assert local.role == UserRole.facilitator
    assert local.hashed_password == "hashed"
    assert not any(a == "auth.oidc_link" for a, _ in audit_events)


async def test_entra_preferred_username_cannot_claim_local_admin(session):
    local_admin = User(
        email="entra-admin@sso.test",
        display_name="Entra Admin",
        hashed_password="hashed",
        role=UserRole.facilitator,
        is_admin=True,
    )
    session.add(local_admin)
    await session.commit()
    identity = EntraAdapter().extract_identity(
        {
            "sub": "attacker-sub",
            "tid": "tenant-a",
            "preferred_username": "entra-admin@sso.test",
        },
        "roles",
    )

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session, cfg=_entra_cfg(), identity=identity
        )
    assert exc.value.reason == "account linking required"
    await session.refresh(local_admin)
    assert local_admin.auth_provider == "local"
    assert local_admin.subject is None
    assert local_admin.is_admin is True


async def test_entra_jit_persists_stable_subject_and_tenant(session):
    identity = EntraAdapter().extract_identity(
        {
            "sub": "entra-sub",
            "tid": "tenant-a",
            "email": "new-entra@sso.test",
        },
        "roles",
    )
    assert identity.email_verified is False

    user, created = await oidc_service.provision_oidc_user(
        session, cfg=_entra_cfg(), identity=identity
    )
    assert created is True
    assert user.auth_provider == "entra"
    assert user.subject == "entra-sub"
    assert user.auth_tenant == "tenant-a"
    assert user.role_managed_by_idp is True


async def test_entra_verified_email_does_not_link_local_account(session):
    local = User(
        email="entra-link@sso.test",
        display_name="Local",
        hashed_password="hashed",
        role=UserRole.facilitator,
    )
    session.add(local)
    await session.commit()
    from dataclasses import replace

    cfg = replace(_cfg(), key="entra")

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session, cfg=cfg, identity=_identity(subject="entra-sub", email=local.email)
        )
    assert exc.value.reason == "account linking required"


async def test_unverified_email_collision_denied(session):
    session.add(
        User(
            email="dup@sso.test",
            display_name="Local",
            hashed_password="h",
            role=UserRole.participant,
        )
    )
    await session.commit()

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session,
            cfg=_cfg(),
            identity=_identity(subject="sub-y", email="dup@sso.test", email_verified=False),
        )
    assert exc.value.reason == "account linking required"


async def test_disabled_local_account_denied(session):
    session.add(
        User(
            email="off@sso.test",
            display_name="Local",
            hashed_password="h",
            role=UserRole.participant,
            is_active=False,
        )
    )
    await session.commit()

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session, cfg=_cfg(), identity=_identity(subject="sub-z", email="off@sso.test")
        )
    assert exc.value.reason == "account disabled"


async def test_disabled_returning_oidc_user_denied(session):
    session.add(
        User(
            email="ret@sso.test",
            display_name="Ret",
            hashed_password=None,
            role=UserRole.participant,
            auth_provider="authentik",
            subject="ret-sub",
            is_active=False,
        )
    )
    await session.commit()

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session, cfg=_cfg(), identity=_identity(subject="ret-sub", email="ret@sso.test")
        )
    assert exc.value.reason == "account disabled"


async def test_cross_provider_takeover_refused(session):
    # An account already linked to provider "entra" must not be re-linked by a
    # different provider ("authentik") that asserts the same verified email.
    session.add(
        User(
            email="shared@sso.test",
            display_name="Linked",
            hashed_password=None,
            role=UserRole.facilitator,
            auth_provider="entra",
            subject="entra-sub",
        )
    )
    await session.commit()

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session,
            cfg=_cfg(),  # authentik
            identity=_identity(subject="authentik-sub", email="shared@sso.test"),
        )
    assert exc.value.reason == "account linking required"


async def test_returning_user_is_not_duplicated(session):
    from sqlmodel import func, select

    ident = _identity(subject="stable-sub", email="stable@sso.test")
    first, created_1 = await oidc_service.provision_oidc_user(session, cfg=_cfg(), identity=ident)
    second, created_2 = await oidc_service.provision_oidc_user(session, cfg=_cfg(), identity=ident)
    assert created_1 is True
    assert created_2 is False
    assert first.id == second.id
    count = (
        await session.exec(
            select(func.count()).select_from(User).where(User.subject == "stable-sub")
        )
    ).one()
    assert count == 1


async def test_returning_identity_backfills_and_enforces_tenant(session):
    legacy = User(
        email="tenant@sso.test",
        display_name="Tenant User",
        hashed_password=None,
        role=UserRole.participant,
        auth_provider="authentik",
        subject="tenant-sub",
        auth_tenant=None,
        role_managed_by_idp=True,
    )
    session.add(legacy)
    await session.commit()

    returned, _ = await oidc_service.provision_oidc_user(
        session,
        cfg=_cfg(),
        identity=_identity(
            subject="tenant-sub", email="tenant@sso.test", tenant_id="tenant-a"
        ),
    )
    assert returned.auth_tenant == "tenant-a"

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session,
            cfg=_cfg(),
            identity=_identity(
                subject="tenant-sub", email="tenant@sso.test", tenant_id="tenant-b"
            ),
        )
    assert exc.value.reason == "identity conflict"

    with pytest.raises(oidc_service.OIDCProvisionError) as exc:
        await oidc_service.provision_oidc_user(
            session,
            cfg=_cfg(),
            identity=_identity(
                subject="tenant-sub", email="tenant@sso.test", tenant_id=None
            ),
        )
    assert exc.value.reason == "identity conflict"


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


def test_entra_email_unverified_without_explicit_claim():
    ident = EntraAdapter().extract_identity(
        {"sub": "s", "tid": "tenant-a", "email": "e@x.test", "name": "E"},
        "roles",
    )
    assert ident.email_verified is False
    assert ident.subject == "s"
    assert ident.tenant_id == "tenant-a"


def test_entra_email_verified_honours_xms_edov_false():
    ident = EntraAdapter().extract_identity(
        {
            "sub": "s",
            "tid": "tenant-a",
            "email": "e@x.test",
            "xms_edov": False,
            "email_verified": True,
        },
        "roles",
    )
    assert ident.email_verified is False


def test_entra_email_verified_honours_explicit_standard_claim():
    ident = EntraAdapter().extract_identity(
        {
            "sub": "s",
            "tid": "tenant-a",
            "email": "e@x.test",
            "email_verified": True,
        },
        "roles",
    )
    assert ident.email_verified is True


def test_entra_email_verified_honours_xms_edov_true():
    ident = EntraAdapter().extract_identity(
        {
            "sub": "s",
            "tid": "tenant-a",
            "email": "e@x.test",
            "xms_edov": True,
        },
        "roles",
    )
    assert ident.email_verified is True


def test_entra_falls_back_to_preferred_username():
    ident = EntraAdapter().extract_identity(
        {
            "sub": "s",
            "tid": "tenant-a",
            "preferred_username": "u@x.test",
            "xms_edov": True,
        },
        "",
    )
    assert ident.email == "u@x.test"
    assert ident.email_verified is False
    assert ident.groups == []


def test_entra_missing_tenant_raises():
    with pytest.raises(ValueError, match="tid"):
        EntraAdapter().extract_identity(
            {"sub": "s", "email": "e@x.test", "xms_edov": True}, ""
        )


def test_missing_or_overage_group_claim_fails_closed():
    standard = get_adapter("authentik")
    assert standard is not None
    standard_identity = standard.extract_identity(
        {
            "sub": "s",
            "email": "e@x.test",
            "email_verified": True,
        },
        "groups",
    )
    assert standard_identity.groups == []
    assert (
        oidc_service.map_role(
            _cfg(role_map={"ttx-facilitators": "facilitator"}), standard_identity
        )
        == UserRole.participant
    )

    entra_identity = EntraAdapter().extract_identity(
        {
            "sub": "s",
            "tid": "tenant-a",
            "email": "e@x.test",
            "xms_edov": True,
            # Entra signals group overage out-of-band and omits the configured
            # claim. Until membership is resolved, the safe result is no roles.
            "_claim_names": {"groups": "src1"},
        },
        "groups",
    )
    assert entra_identity.groups == []
    assert (
        oidc_service.map_role(
            _entra_cfg(role_map={"ttx-facilitators": "facilitator"}),
            entra_identity,
        )
        == UserRole.participant
    )


def test_authentik_missing_email_raises():
    adapter = get_adapter("authentik")
    with pytest.raises(ValueError):
        adapter.extract_identity({"sub": "s"}, "groups")


@pytest.mark.parametrize("key", ["authentik", "auth0", "okta"])
def test_standard_adapters_share_claim_mapping(key):
    adapter = get_adapter(key)
    ident = adapter.extract_identity(
        {"sub": "s", "email": "e@x.test", "email_verified": True, "name": "N", "roles": ["r"]},
        "roles",
    )
    assert (ident.subject, ident.email, ident.email_verified, ident.groups) == (
        "s",
        "e@x.test",
        True,
        ["r"],
    )


@pytest.mark.parametrize("key", ["auth0", "okta"])
def test_standard_adapter_email_unverified_defaults_false(key):
    ident = get_adapter(key).extract_identity({"sub": "s", "email": "e@x.test"}, "")
    assert ident.email_verified is False


def test_config_builds_auth0_and_okta_metadata_urls(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "oidc_auth0_enabled", True)
    monkeypatch.setattr(settings, "oidc_auth0_domain", "t.us.auth0.com")
    monkeypatch.setattr(settings, "oidc_auth0_client_id", "a")
    monkeypatch.setattr(settings, "oidc_auth0_client_secret", "b")
    monkeypatch.setattr(settings, "oidc_okta_enabled", True)
    monkeypatch.setattr(settings, "oidc_okta_domain", "dev-1.okta.com")
    monkeypatch.setattr(settings, "oidc_okta_auth_server", "default")
    monkeypatch.setattr(settings, "oidc_okta_client_id", "c")
    monkeypatch.setattr(settings, "oidc_okta_client_secret", "d")

    by_key = {p.key: p for p in settings.enabled_oidc_providers()}
    assert by_key["auth0"].metadata_url == "https://t.us.auth0.com/.well-known/openid-configuration"
    assert (
        by_key["okta"].metadata_url
        == "https://dev-1.okta.com/oauth2/default/.well-known/openid-configuration"
    )


def test_config_okta_org_server_omits_oauth2_path(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "oidc_okta_enabled", True)
    monkeypatch.setattr(settings, "oidc_okta_domain", "dev-1.okta.com")
    monkeypatch.setattr(settings, "oidc_okta_auth_server", "")  # org server
    monkeypatch.setattr(settings, "oidc_okta_client_id", "c")
    monkeypatch.setattr(settings, "oidc_okta_client_secret", "d")

    okta = next(p for p in settings.enabled_oidc_providers() if p.key == "okta")
    assert okta.metadata_url == "https://dev-1.okta.com/.well-known/openid-configuration"


# --------------------------------------------------------------------------- #
# Routes (HTTP, stubbed IdP)
# --------------------------------------------------------------------------- #


async def test_login_redirects_to_idp(client, patch_oidc):
    patch_oidc(_FakeOIDCClient())
    r = await client.get("/api/auth/oidc/authentik/login", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("https://authentik.test/authorize")


async def test_login_unknown_provider_404(client):
    r = await client.get("/api/auth/oidc/nope/login", follow_redirects=False)
    assert r.status_code == 404


async def test_callback_provisions_and_starts_session(client, patch_oidc, audit_events):
    claims = {"sub": "cb-1", "email": "cb@sso.test", "email_verified": True, "name": "CB User"}
    patch_oidc(_FakeOIDCClient(claims=claims))

    r = await client.get(
        "/api/auth/oidc/authentik/callback?code=x&state=y", follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert "access_token" in client.cookies

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "cb@sso.test"
    assert body["role"] == "participant"

    actions = [a for a, _ in audit_events]
    assert "auth.jit_provision" in actions
    assert "auth.oidc_login" in actions


async def test_callback_invalid_token_denies(client, patch_oidc, audit_events):
    patch_oidc(_FakeOIDCClient(error=OAuthError(error="invalid_grant")))
    r = await client.get(
        "/api/auth/oidc/authentik/callback?code=x&state=bad", follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=sso"
    assert "access_token" not in client.cookies
    assert any(a == "auth.oidc_login" and kw.get("result") == "fail" for a, kw in audit_events)


async def test_callback_unverified_collision_denies(client, patch_oidc, session):
    session.add(
        User(
            email="clash@sso.test",
            display_name="Local",
            hashed_password="h",
            role=UserRole.participant,
        )
    )
    await session.commit()
    patch_oidc(
        _FakeOIDCClient(
            claims={"sub": "clash-sub", "email": "clash@sso.test", "email_verified": False}
        )
    )
    r = await client.get(
        "/api/auth/oidc/authentik/callback?code=x&state=y", follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?error=sso"
    assert "access_token" not in client.cookies


async def test_callback_verified_collision_cannot_claim_local_admin(
    client, patch_oidc, session
):
    local_admin = User(
        email="local-admin@sso.test",
        display_name="Local Admin",
        hashed_password="h",
        role=UserRole.facilitator,
        is_admin=True,
    )
    session.add(local_admin)
    await session.commit()
    patch_oidc(
        _FakeOIDCClient(
            claims={
                "sub": "attacker-sub",
                "email": "local-admin@sso.test",
                "email_verified": True,
            }
        )
    )

    response = await client.get(
        "/api/auth/oidc/authentik/callback?code=x&state=y",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?error=sso"
    assert "access_token" not in client.cookies
    await session.refresh(local_admin)
    assert local_admin.auth_provider == "local"
    assert local_admin.subject is None
    assert local_admin.role == UserRole.facilitator
    assert local_admin.is_admin is True


async def test_callback_role_downgrade_issues_working_participant_session(
    client, patch_oidc, session
):
    existing = User(
        email="callback-managed@sso.test",
        display_name="Callback Managed",
        hashed_password=None,
        role=UserRole.facilitator,
        auth_provider="authentik",
        subject="callback-managed-sub",
        role_managed_by_idp=True,
    )
    session.add(existing)
    await session.commit()
    patch_oidc(
        _FakeOIDCClient(
            claims={
                "sub": "callback-managed-sub",
                "email": "callback-managed@sso.test",
                "email_verified": True,
                # The configured groups claim is absent: fail closed to participant.
            }
        )
    )

    response = await client.get(
        "/api/auth/oidc/authentik/callback?code=x&state=y",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    await session.refresh(existing)
    assert existing.role == UserRole.participant
    assert existing.token_valid_after is not None

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == UserRole.participant.value


async def test_auth_mode_oidc_disables_local_login(client, monkeypatch):
    from app.routers import auth as auth_router

    monkeypatch.setattr(auth_router.settings, "auth_mode", "oidc")
    r = await client.post(
        "/api/auth/login", json={"email": "x@y.test", "password": "password1234"}
    )
    assert r.status_code == 403


async def test_oidc_only_account_cannot_local_login(client, session):
    # A passwordless (SSO-provisioned) account must not authenticate via the local
    # password endpoint (#25).
    session.add(
        User(
            email="nopw@sso.test",
            display_name="No Password",
            hashed_password=None,
            role=UserRole.participant,
            auth_provider="authentik",
            subject="nopw-sub",
        )
    )
    await session.commit()
    r = await client.post(
        "/api/auth/login", json={"email": "nopw@sso.test", "password": "password1234"}
    )
    assert r.status_code == 401


async def test_login_page_shows_sso_button(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "Sign in with Authentik" in r.text
    assert "/api/auth/oidc/authentik/login" in r.text
