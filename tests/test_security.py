"""Tests for the P0/P1 security hardening: #9, #10, #11, #23."""

import json
import logging

import pytest
from httpx import AsyncClient

from app.config import DEFAULT_SECRET_KEY, Settings, validate_settings
from app.models.user import User
from app.services.auth_service import hash_password, verify_password

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
    # llm_provider="none" keeps this focused on the SECRET_KEY concern; the default
    # provider (anthropic) would otherwise require an API key in production (#26).
    s = Settings(dev_mode=False, secret_key="a" * 48, llm_provider="none")
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


async def test_spoofed_forwarded_for_does_not_bypass_rate_limit(
    client: AsyncClient, facilitator: User
):
    """#36: a rotating X-Forwarded-For must not mint distinct rate-limit keys.

    client_ip() now derives from request.client.host (set by uvicorn's trusted
    proxy handling), so the client-supplied header is ignored and all attempts
    share one key — the lockout still fires despite a different spoofed IP each
    time. With the old leftmost-XFF parse each request keyed a fresh bucket and
    the limiter never tripped.
    """
    creds = {"email": facilitator.email, "password": "wrongpassword"}
    for i in range(5):
        resp = await client.post(
            "/api/auth/login", json=creds, headers={"X-Forwarded-For": f"203.0.113.{i}"}
        )
        assert resp.status_code == 401
    locked = await client.post(
        "/api/auth/login",
        json={"email": facilitator.email, "password": "password1234"},
        headers={"X-Forwarded-For": "203.0.113.250"},
    )
    assert locked.status_code == 429


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


async def test_oversized_bcrypt_password_is_counted_as_invalid_login(
    client: AsyncClient, facilitator: User
):
    # 37 two-byte characters exceed bcrypt's 72-byte boundary without relying on
    # character count. These used to raise before record_failure() and return 500.
    oversized = "é" * 37
    for _ in range(5):
        response = await client.post(
            "/api/auth/login",
            json={"email": facilitator.email, "password": oversized},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid credentials"}

    locked = await client.post(
        "/api/auth/login",
        json={"email": facilitator.email, "password": "password1234"},
    )
    assert locked.status_code == 429


async def test_oversized_password_has_uniform_unknown_account_response(client: AsyncClient):
    response = await client.post(
        "/api/auth/login",
        json={"email": "unknown@example.com", "password": "x" * 73},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid credentials"}


def test_password_verifier_never_accepts_a_truncated_bcrypt_tail():
    accepted_prefix = "x" * 72
    hashed = hash_password(accepted_prefix)
    assert verify_password(accepted_prefix, hashed) is True
    assert verify_password(accepted_prefix + "ignored-tail", hashed) is False
    assert verify_password("\ud800", hashed) is False


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
