"""OIDC / SSO tests (#25).

The Authlib client is always stubbed — no discovery/JWKS/token network calls. The
Authentik provider is enabled for the whole suite in conftest, so the routes are
registered and ``get_provider("authentik")`` resolves.
"""

import pytest
from authlib.integrations.starlette_client import OAuthError

from app.config import OIDCProviderConfig
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
    assert user.hashed_password is None
    assert any(a == "auth.jit_provision" for a, _ in audit_events)


async def test_role_map_elevates(session):
    cfg = _cfg(role_map={"ttx-facilitators": "facilitator"})
    user, _ = await oidc_service.provision_oidc_user(
        session, cfg=cfg, identity=_identity(groups=["ttx-facilitators"])
    )
    assert user.role == UserRole.facilitator


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


async def test_links_verified_email_to_local_account(session, audit_events):
    local = User(
        email="link@sso.test",
        display_name="Local",
        hashed_password="hashed",
        role=UserRole.facilitator,
    )
    session.add(local)
    await session.commit()

    user, created = await oidc_service.provision_oidc_user(
        session, cfg=_cfg(), identity=_identity(subject="sub-x", email="link@sso.test")
    )
    assert created is False
    assert user.id == local.id
    assert user.auth_provider == "authentik"
    assert user.subject == "sub-x"
    assert user.role == UserRole.facilitator  # preserved, not downgraded
    assert user.hashed_password == "hashed"  # local password retained
    assert any(a == "auth.oidc_link" for a, _ in audit_events)


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
    assert exc.value.reason == "unverified email collision"


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
    assert exc.value.reason == "identity conflict"


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


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


def test_entra_email_verified_defaults_true_without_claim():
    ident = EntraAdapter().extract_identity({"sub": "s", "email": "e@x.test", "name": "E"}, "roles")
    assert ident.email_verified is True


def test_entra_email_verified_honours_xms_edov_false():
    ident = EntraAdapter().extract_identity(
        {"sub": "s", "email": "e@x.test", "xms_edov": False}, "roles"
    )
    assert ident.email_verified is False


def test_entra_falls_back_to_preferred_username():
    ident = EntraAdapter().extract_identity({"sub": "s", "preferred_username": "u@x.test"}, "")
    assert ident.email == "u@x.test"
    assert ident.groups == []


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
