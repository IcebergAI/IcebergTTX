"""Runtime OIDC settings, lockout prevention, and secret boundaries (#191)."""

from httpx import AsyncClient

from app.config import settings
from app.services import oidc_settings_service
from app.services.oidc import service as oidc_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_oidc_settings_requires_admin(
    client: AsyncClient, facilitator_token: str
):
    response = await client.get(
        "/api/oidc/settings", headers=_bearer(facilitator_token)
    )
    assert response.status_code == 403


async def test_oidc_settings_expose_only_secret_status(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "oidc_authentik_client_secret", "env-secret-value")
    response = await client.get(
        "/api/oidc/settings", headers=_bearer(admin_token)
    )
    assert response.status_code == 200
    assert response.json()["client_secrets_set"]["authentik"] is True
    assert "client_secret" not in response.text
    assert "env-secret-value" not in response.text


async def test_oidc_api_rejects_client_secret_fields_without_reflection(
    client: AsyncClient, admin_token: str
):
    response = await client.put(
        "/api/oidc/settings",
        json={"oidc_authentik_client_secret": "must-not-enter-runtime"},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422
    assert "must-not-enter-runtime" not in response.text


async def test_oidc_only_lockout_save_is_refused_and_runtime_unchanged(
    client: AsyncClient, admin_token: str
):
    before = oidc_settings_service.get_config()
    response = await client.put(
        "/api/oidc/settings",
        json={
            "auth_mode": "oidc",
            "oidc_entra_enabled": False,
            "oidc_authentik_enabled": False,
            "oidc_auth0_enabled": False,
            "oidc_okta_enabled": False,
        },
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422
    assert "OIDC-only mode requires" in response.json()["detail"]
    assert oidc_settings_service.get_config().auth_mode == before.auth_mode


async def test_role_map_save_rebuilds_registry_for_next_login(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "oidc_authentik_client_secret", "env-secret")
    oidc_service.ensure_registered()
    previous = oidc_service.get_provider("authentik")
    assert previous is not None
    assert previous.role_map == {}

    response = await client.put(
        "/api/oidc/settings",
        json={
            "auth_mode": "both",
            "oidc_authentik_enabled": True,
            "oidc_authentik_client_id": "runtime-client",
            "oidc_authentik_base_url": "https://identity.runtime.test",
            "oidc_authentik_app_slug": "iceberg",
            "oidc_authentik_role_claim": "groups",
            "oidc_authentik_role_map": "responders=facilitator",
        },
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200, response.text
    assert oidc_service.get_provider("authentik") is None

    oidc_service.ensure_registered()
    current = oidc_service.get_provider("authentik")
    assert current is not None
    assert current.client_id == "runtime-client"
    assert current.role_map == {"responders": "facilitator"}


async def test_invalid_role_map_is_refused(
    client: AsyncClient, admin_token: str
):
    response = await client.put(
        "/api/oidc/settings",
        json={"oidc_authentik_role_map": "admins=superuser"},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422
    assert "group=participant|observer|facilitator" in response.json()["detail"]


async def test_admin_oidc_page_requires_admin(client: AsyncClient):
    response = await client.get("/admin/oidc", follow_redirects=False)
    assert response.status_code in (302, 307)
