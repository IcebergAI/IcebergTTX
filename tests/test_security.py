"""Tests for the P0/P1 security hardening: #9, #10, #11, #23."""

import json
import logging

import pytest
from httpx import AsyncClient

from app.config import DEFAULT_SECRET_KEY, Settings, validate_settings
from app.models.user import User

# ── #9: SECRET_KEY startup validation ─────────────────────────────────────────

async def test_validate_settings_rejects_default_key_in_production():
    s = Settings(dev_mode=False, secret_key=DEFAULT_SECRET_KEY)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        validate_settings(s)


async def test_validate_settings_rejects_short_key_in_production():
    s = Settings(dev_mode=False, secret_key="too-short")
    with pytest.raises(RuntimeError):
        validate_settings(s)


async def test_validate_settings_allows_default_in_dev_mode():
    s = Settings(dev_mode=True, secret_key=DEFAULT_SECRET_KEY)
    validate_settings(s)  # must not raise


async def test_validate_settings_allows_strong_key_in_production():
    s = Settings(dev_mode=False, secret_key="a" * 48)
    validate_settings(s)  # must not raise


# ── #11: login rate limiting ──────────────────────────────────────────────────

async def test_login_rate_limited_after_repeated_failures(client: AsyncClient, facilitator: User):
    creds = {"email": facilitator.email, "password": "wrongpassword"}
    for _ in range(5):
        assert (await client.post("/api/auth/login", json=creds)).status_code == 401
    # Sixth attempt is locked out, even with the correct password.
    resp = await client.post(
        "/api/auth/login", json={"email": facilitator.email, "password": "password1234"}
    )
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


async def test_login_success_resets_rate_counter(client: AsyncClient, facilitator: User):
    for _ in range(3):
        await client.post("/api/auth/login", json={"email": facilitator.email, "password": "nope"})
    ok = await client.post(
        "/api/auth/login", json={"email": facilitator.email, "password": "password1234"}
    )
    assert ok.status_code == 200
    # Counter reset → further failures start fresh and are not immediately locked.
    assert (await client.post(
        "/api/auth/login", json={"email": facilitator.email, "password": "nope"}
    )).status_code == 401


# ── #10: Secure cookie + CSRF origin check ────────────────────────────────────

async def test_csrf_blocks_cookie_mutation_without_origin(client: AsyncClient, facilitator: User):
    login = await client.post(
        "/api/auth/login", json={"email": facilitator.email, "password": "password1234"}
    )
    assert login.status_code == 200  # cookie now stored on the client
    # Cookie-authenticated mutation with no Authorization header and no Origin.
    resp = await client.post("/api/exercises", json={"scenario_id": 1, "title": "x"})
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


async def test_csrf_allows_same_origin_cookie_mutation(client: AsyncClient, facilitator: User):
    await client.post(
        "/api/auth/login", json={"email": facilitator.email, "password": "password1234"}
    )
    resp = await client.post(
        "/api/exercises",
        json={"scenario_id": 999, "title": "x"},
        headers={"Origin": "http://testserver"},
    )
    # Passes the CSRF gate (not a 403 CSRF error); fails later on a bad scenario id.
    assert resp.status_code != 403


async def test_csrf_exempts_bearer_authenticated_mutation(
    client: AsyncClient, facilitator_token: str
):
    resp = await client.post(
        "/api/exercises",
        json={"scenario_id": 999, "title": "x"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert resp.status_code != 403


# ── #23: audit logging ────────────────────────────────────────────────────────

def _audit_events(caplog) -> list[dict]:
    events = []
    for rec in caplog.records:
        if rec.name == "iceberg_ttx.audit":
            try:
                events.append(json.loads(rec.getMessage()))
            except ValueError:
                pass
    return events


async def test_audit_logs_login_failure(client: AsyncClient, facilitator: User, caplog):
    with caplog.at_level(logging.INFO, logger="iceberg_ttx.audit"):
        await client.post(
            "/api/auth/login", json={"email": facilitator.email, "password": "wrong"}
        )
    events = _audit_events(caplog)
    assert any(e["action"] == "auth.login" and e["result"] == "fail" for e in events)


async def test_audit_logs_authorization_denial(client: AsyncClient, participant_token: str, caplog):
    with caplog.at_level(logging.INFO, logger="iceberg_ttx.audit"):
        resp = await client.get(
            "/api/users", headers={"Authorization": f"Bearer {participant_token}"}
        )
    assert resp.status_code == 403
    events = _audit_events(caplog)
    assert any(e["action"] == "authz.denied" and e["result"] == "deny" for e in events)


async def test_audit_attributes_real_identity_under_role_preview(
    client: AsyncClient, facilitator_token: str, facilitator: User, caplog
):
    client.cookies.set("dt_view_role", "participant")
    with caplog.at_level(logging.INFO, logger="iceberg_ttx.audit"):
        await client.get("/api/users", headers={"Authorization": f"Bearer {facilitator_token}"})
    # Previewing participant, a facilitator hitting users is allowed (real role used),
    # so no denial is logged for them.
    denials = [e for e in _audit_events(caplog) if e["action"] == "authz.denied"]
    assert all(e["actor_role"] == "facilitator" for e in denials)


async def test_audit_logs_inject_release(
    client: AsyncClient, facilitator_token: str, active_exercise, caplog
):
    created = (await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={"title": "T", "content": "C", "sequence_order": 0},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    with caplog.at_level(logging.INFO, logger="iceberg_ttx.audit"):
        resp = await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    assert resp.status_code == 200
    events = _audit_events(caplog)
    assert any(
        e["action"] == "inject.release" and str(e["target_id"]) == str(created["id"])
        for e in events
    )


async def test_audit_logs_export(
    client: AsyncClient, facilitator_token: str, draft_exercise, caplog
):
    with caplog.at_level(logging.INFO, logger="iceberg_ttx.audit"):
        resp = await client.get(
            f"/api/exercises/{draft_exercise.id}/export",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    assert resp.status_code == 200
    events = _audit_events(caplog)
    assert any(e["action"] == "exercise.export" for e in events)


async def test_audit_sanitizes_log_injection(caplog):
    from app.services import audit_service

    with caplog.at_level(logging.INFO, logger="iceberg_ttx.audit"):
        audit_service.emit(
            "auth.login",
            result="fail",
            actor_email="evil\r\nFAKE action=admin.takeover",
            reason="line1\nline2",
        )
    events = _audit_events(caplog)
    assert events
    e = events[-1]
    assert "\n" not in e["actor_email"] and "\r" not in e["actor_email"]
    assert "\n" not in e["reason"]
