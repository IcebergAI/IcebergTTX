"""Runtime general policy and rate-limit settings (#188, #190)."""

from datetime import UTC, datetime
from uuid import uuid4

import jwt
from httpx import AsyncClient

from app.config import settings
from app.services import audit_service, general_settings_service
from app.services.auth_service import create_access_token
from app.services.rate_limit import login_rate_limiter


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_general_settings_requires_admin(client: AsyncClient, facilitator_token: str):
    response = await client.get("/api/general/settings", headers=_bearer(facilitator_token))
    assert response.status_code == 403


async def test_general_settings_seed_from_environment(
    client: AsyncClient, admin_token: str, monkeypatch
):
    monkeypatch.setattr(settings, "access_token_expire_minutes", 777)
    response = await client.get("/api/general/settings", headers=_bearer(admin_token))
    assert response.status_code == 200
    assert response.json()["access_token_expire_minutes"] == 777


async def test_save_updates_runtime_policy_and_emits_warning_audit(
    client: AsyncClient, admin_token: str, monkeypatch
):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        audit_service,
        "emit",
        lambda action, **kwargs: events.append((action, kwargs)),
    )
    response = await client.put(
        "/api/general/settings",
        json={
            "registration_enabled": False,
            "access_token_expire_minutes": 90,
            "audit_persist": False,
        },
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200, response.text
    cached = general_settings_service.get_config()
    assert cached.registration_enabled is False
    assert cached.access_token_expire_minutes == 90
    assert events[-1][0] == "audit.settings_updated"
    assert events[-1][1]["severity"] == "warning"

    blocked = await client.post(
        "/api/auth/register",
        json={
            "email": f"blocked-{uuid4().hex}@example.test",
            "display_name": "Blocked",
            "password": "password1234",
            "role": "participant",
        },
    )
    assert blocked.status_code == 403


async def test_runtime_login_limit_causes_real_lockout(client: AsyncClient, admin_token: str):
    response = await client.put(
        "/api/general/settings",
        json={"login_max_attempts": 1, "login_lockout_seconds": 900},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200
    login_rate_limiter.clear()

    body = {"email": "nobody@example.test", "password": "wrong-password"}
    first = await client.post("/api/auth/login", json=body)
    second = await client.post("/api/auth/login", json=body)
    assert first.status_code == 401
    assert second.status_code == 429
    assert int(second.headers["retry-after"]) > 0


async def test_token_lifetime_uses_runtime_snapshot(client: AsyncClient, admin_token: str):
    response = await client.put(
        "/api/general/settings",
        json={"access_token_expire_minutes": 17},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 200
    token = create_access_token("123", "participant")
    payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    lifetime = payload["exp"] - payload["iat"]
    assert 16 * 60 <= lifetime <= 17 * 60
    assert datetime.fromtimestamp(payload["exp"], UTC) > datetime.now(UTC)


async def test_general_settings_reject_unknown_or_secret_fields(
    client: AsyncClient, admin_token: str
):
    response = await client.put(
        "/api/general/settings",
        json={"secret_key": "must-not-round-trip"},
        headers=_bearer(admin_token),
    )
    assert response.status_code == 422
    assert "must-not-round-trip" not in response.text


async def test_admin_general_page_requires_admin(client: AsyncClient):
    response = await client.get("/admin/settings", follow_redirects=False)
    assert response.status_code in (302, 307)
