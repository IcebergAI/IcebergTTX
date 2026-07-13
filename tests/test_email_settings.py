"""Runtime email settings, delivery safety, and feature-gate wiring (#186)."""

import pytest
from httpx import AsyncClient

from app.config import settings
from app.services import mail_service


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_unloaded_cache_preserves_environment_behaviour(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@test")
    mail_service.set_config(None)
    assert mail_service.smtp_enabled() is True


async def test_email_settings_requires_admin(client: AsyncClient, facilitator_token: str):
    response = await client.get("/api/email/settings", headers=_bearer(facilitator_token))
    assert response.status_code == 403


async def test_email_settings_seed_from_environment(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "smtp_host", "smtp.seed.test")
    monkeypatch.setattr(settings, "smtp_from", "seed@test")
    response = await client.get("/api/email/settings", headers=_bearer(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["smtp_host"] == "smtp.seed.test"
    assert "smtp_password" not in body


async def test_email_save_refreshes_feature_cache(client: AsyncClient, admin_token: str):
    response = await client.put(
        "/api/email/settings",
        json={
            "enabled": True,
            "smtp_host": "smtp.runtime.test",
            "smtp_port": 2525,
            "smtp_from": "noreply@runtime.test",
            "smtp_username": "relay-user",
            "smtp_starttls": True,
            "smtp_tls": False,
            "public_base_url": "https://ttx.runtime.test",
        },
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200, response.text
    cached = mail_service.get_config()
    assert cached.smtp_enabled is True
    assert cached.smtp_host == "smtp.runtime.test"
    assert cached.public_base_url == "https://ttx.runtime.test"


async def test_email_api_rejects_password(client: AsyncClient, admin_token: str):
    response = await client.put(
        "/api/email/settings",
        json={"smtp_password": "must-not-enter-the-app"},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422
    assert "must-not-enter-the-app" not in response.text


async def test_email_api_rejects_conflicting_tls(client: AsyncClient, admin_token: str):
    response = await client.put(
        "/api/email/settings",
        json={"smtp_starttls": True, "smtp_tls": True},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422


async def test_email_test_only_uses_requesting_admin(
    client: AsyncClient, admin_token: str, monkeypatch
):
    recipients: list[str] = []

    async def fake_send_test(to: str) -> None:
        recipients.append(to)

    monkeypatch.setattr(mail_service, "send_test", fake_send_test)
    response = await client.post("/api/email/test", headers=_bearer(admin_token))
    assert response.status_code == 200
    assert response.json() == {"result": "ok"}
    assert recipients == ["admin@example.com"]

    rejected = await client.post(
        "/api/email/test?recipient=attacker@example.com",
        json={"recipient": "attacker@example.com"},
        headers=_bearer(admin_token),
    )
    assert rejected.status_code == 200
    assert recipients == ["admin@example.com", "admin@example.com"]


async def test_email_test_never_leaks_exception_message(
    client: AsyncClient, admin_token: str, monkeypatch
):
    async def fail(_: str) -> None:
        raise RuntimeError("smtp.internal secret-password")

    monkeypatch.setattr(mail_service, "send_test", fail)
    response = await client.post("/api/email/test", headers=_bearer(admin_token))
    assert response.status_code == 200
    assert response.json() == {"result": "error: RuntimeError"}
    assert "smtp.internal" not in response.text
    assert "secret-password" not in response.text


async def test_disabled_cache_keeps_email_endpoints_hidden(
    client: AsyncClient, admin_token: str
):
    response = await client.put(
        "/api/email/settings",
        json={"enabled": False, "smtp_host": "", "smtp_from": ""},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200
    reset = await client.post(
        "/api/auth/password-reset/request", json={"email": "someone@example.com"}
    )
    assert reset.status_code == 404


async def test_admin_email_page_requires_admin(client: AsyncClient):
    response = await client.get("/admin/email", follow_redirects=False)
    assert response.status_code in (302, 307)


@pytest.mark.parametrize("port", [0, 65536])
async def test_email_api_rejects_invalid_port(
    client: AsyncClient, admin_token: str, port: int
):
    response = await client.put(
        "/api/email/settings", json={"smtp_port": port}, headers=_bearer(admin_token)
    )
    assert response.status_code == 422
