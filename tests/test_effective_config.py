"""Read-only effective configuration, provenance, and redaction (#192)."""

from httpx import AsyncClient

from app.config import Settings, settings
from app.services import general_settings_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_effective_config_requires_admin(
    client: AsyncClient, facilitator_token: str
):
    response = await client.get(
        "/api/config/effective", headers=_bearer(facilitator_token)
    )
    assert response.status_code == 403


async def test_effective_config_lists_every_setting_and_never_secret_values(
    client: AsyncClient, admin_token: str, monkeypatch
):
    secrets = {
        "anthropic_api_key": "never-render-anthropic",
        "smtp_password": "never-render-smtp",
        "proxy_url": "http://embedded-user:embedded-password@proxy.example:3128",
        "proxy_password": "never-render-proxy",
        "oidc_authentik_client_secret": "never-render-oidc",
    }
    for field, value in secrets.items():
        monkeypatch.setattr(settings, field, value)

    response = await client.get(
        "/api/config/effective", headers=_bearer(admin_token)
    )
    assert response.status_code == 200
    body = response.json()
    rows = {row["name"]: row for row in body["settings"]}
    assert set(Settings.model_fields).issubset(rows)
    assert rows["secret_key"]["secret"] is True
    assert rows["secret_key"]["value"] is True
    assert settings.secret_key not in response.text
    for field, value in secrets.items():
        assert rows[field]["secret"] is True
        assert rows[field]["value"] is True
        assert value not in response.text
    assert rows["database_url"]["secret"] is True


async def test_database_runtime_value_reports_database_provenance(
    client: AsyncClient, admin_token: str, session
):
    await general_settings_service.update_settings(
        session, {"access_token_expire_minutes": 137}
    )
    response = await client.get(
        "/api/config/effective", headers=_bearer(admin_token)
    )
    rows = {row["name"]: row for row in response.json()["settings"]}
    assert rows["access_token_expire_minutes"]["value"] == 137
    assert rows["access_token_expire_minutes"]["provenance"] == "database"


async def test_effective_config_reports_validation_and_feature_flags(
    client: AsyncClient, admin_token: str
):
    response = await client.get(
        "/api/config/effective", headers=_bearer(admin_token)
    )
    body = response.json()
    assert set(body["features"]) == {
        "smtp_enabled",
        "local_auth_enabled",
        "registration_enabled",
        "active_llm_provider",
        "oidc_providers",
    }
    assert isinstance(body["validation"]["ok"], bool)
    assert isinstance(body["validation"]["errors"], list)


async def test_admin_effective_config_page_requires_admin(client: AsyncClient):
    response = await client.get("/admin/config", follow_redirects=False)
    assert response.status_code in (302, 307)
