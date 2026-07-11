from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.services.auth_service import create_access_token


async def test_register_success(client: AsyncClient):
    resp = await client.post("/api/auth/register", json={
        "email": "new@example.com",
        "display_name": "New User",
        "password": "secret123456",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "new@example.com"
    assert data["role"] == "participant"
    assert "id" in data
    assert "hashed_password" not in data


async def test_register_duplicate_email(client: AsyncClient, facilitator: User):
    resp = await client.post("/api/auth/register", json={
        "email": facilitator.email,
        "display_name": "Dup",
        "password": "secret123456",
    })
    assert resp.status_code == 409


async def test_register_ignores_elevated_role(client: AsyncClient):
    """Regression for #8: self-registration must never grant a privileged role."""
    resp = await client.post("/api/auth/register", json={
        "email": "fac2@example.com",
        "display_name": "Fac 2",
        "password": "secret123456",
        "role": "facilitator",
    })
    assert resp.status_code == 201
    assert resp.json()["role"] == "participant"


async def test_register_ignores_observer_role(client: AsyncClient):
    resp = await client.post("/api/auth/register", json={
        "email": "obs@example.com",
        "display_name": "Obs",
        "password": "secret123456",
        "role": "observer",
    })
    assert resp.status_code == 201
    assert resp.json()["role"] == "participant"


async def test_register_disabled_returns_403(client: AsyncClient, monkeypatch):
    """#67: REGISTRATION_ENABLED=false closes self-service registration."""
    from app.config import settings

    monkeypatch.setattr(settings, "registration_enabled", False)
    resp = await client.post("/api/auth/register", json={
        "email": "nope@example.com",
        "display_name": "Nope",
        "password": "secret123456",
    })
    assert resp.status_code == 403


async def test_register_rate_limited(client: AsyncClient):
    """#67: over REGISTRATION_MAX_ATTEMPTS from one IP returns 429 + Retry-After."""
    from app.config import settings

    for i in range(settings.registration_max_attempts):
        resp = await client.post("/api/auth/register", json={
            "email": f"user{i}@example.com",
            "display_name": f"User {i}",
            "password": "secret123456",
        })
        assert resp.status_code == 201, resp.text
    resp = await client.post("/api/auth/register", json={
        "email": "overflow@example.com",
        "display_name": "Overflow",
        "password": "secret123456",
    })
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0


async def test_login_success(client: AsyncClient, facilitator: User):
    resp = await client.post("/api/auth/login", json={
        "email": facilitator.email,
        "password": "password1234",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


async def test_logout_clears_cookie_backed_session(client: AsyncClient, facilitator: User):
    login_resp = await client.post("/api/auth/login", json={
        "email": facilitator.email,
        "password": "password1234",
    })
    assert login_resp.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200

    logout_resp = await client.post("/api/auth/logout")

    assert logout_resp.status_code == 200
    assert logout_resp.json() == {"ok": True}
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_login_wrong_password(client: AsyncClient, facilitator: User):
    resp = await client.post("/api/auth/login", json={
        "email": facilitator.email,
        "password": "wrongpassword",
    })
    assert resp.status_code == 401


async def test_login_unknown_email(client: AsyncClient):
    resp = await client.post("/api/auth/login", json={
        "email": "nobody@example.com",
        "password": "password1234",
    })
    assert resp.status_code == 401


async def test_get_me(client: AsyncClient, facilitator_token: str, facilitator: User):
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {facilitator_token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == facilitator.email
    assert data["role"] == "facilitator"
    assert data["actual_role"] == "facilitator"
    assert data["can_switch_roles"] is True


async def test_facilitator_can_preview_participant_role(
    client: AsyncClient, facilitator_token: str
):
    client.cookies.set("dt_view_role", "participant")
    client.cookies.set("dt_view_team", "it_ops")
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {facilitator_token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "participant"
    assert data["actual_role"] == "facilitator"
    assert data["team"] == "it_ops"
    assert data["can_switch_roles"] is True


async def test_facilitator_can_preview_observer_role(client: AsyncClient, facilitator_token: str):
    client.cookies.set("dt_view_role", "observer")
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {facilitator_token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "observer"
    assert data["actual_role"] == "facilitator"
    assert data["can_switch_roles"] is True


async def test_participant_cannot_preview_facilitator_role(
    client: AsyncClient, participant_token: str
):
    client.cookies.set("dt_view_role", "facilitator")
    resp = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {participant_token}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["role"] == "participant"
    assert data["actual_role"] == "participant"
    assert data["can_switch_roles"] is False


async def test_get_me_no_token(client: AsyncClient):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_get_me_invalid_token(client: AsyncClient):
    resp = await client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-valid-token"})
    assert resp.status_code == 401


async def test_update_me_display_name(client: AsyncClient, facilitator_token: str):
    resp = await client.put(
        "/api/auth/me",
        json={"display_name": "Updated Name"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Updated Name"


async def test_update_me_password_and_relogin(
    client: AsyncClient, facilitator: User, facilitator_token: str
):
    await client.put(
        "/api/auth/me",
        json={"password": "newpassword456"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    resp = await client.post(
        "/api/auth/login", json={"email": facilitator.email, "password": "newpassword456"}
    )
    assert resp.status_code == 200


async def test_role_in_token(client: AsyncClient, facilitator_token: str, participant_token: str):
    from app.services.auth_service import decode_access_token

    fac_payload = decode_access_token(facilitator_token)
    assert fac_payload["role"] == "facilitator"

    par_payload = decode_access_token(participant_token)
    assert par_payload["role"] == "participant"


# ── #13: password strength validation ─────────────────────────────────────────


@pytest.mark.parametrize("password", ["", "   ", "short", "elevenchars", "x" * 129])
async def test_register_rejects_weak_password(client: AsyncClient, password: str):
    resp = await client.post("/api/auth/register", json={
        "email": "weak@example.com",
        "display_name": "Weak",
        "password": password,
    })
    assert resp.status_code == 422


async def test_register_accepts_minimum_length_password(client: AsyncClient):
    resp = await client.post("/api/auth/register", json={
        "email": "twelvechars@example.com",
        "display_name": "Twelve",
        "password": "a" * 12,
    })
    assert resp.status_code == 201


async def test_register_rejects_password_over_bcrypt_limit(client: AsyncClient):
    # bcrypt truncates input at 72 bytes; the policy must reject anything longer
    # so the whole password stays significant (a mistyped tail can't authenticate).
    resp = await client.post("/api/auth/register", json={
        "email": "toolong@example.com",
        "display_name": "TooLong",
        "password": "a" * 73,
    })
    assert resp.status_code == 422


async def test_register_accepts_password_at_bcrypt_limit(client: AsyncClient):
    resp = await client.post("/api/auth/register", json={
        "email": "seventytwo@example.com",
        "display_name": "AtLimit",
        "password": "a" * 72,
    })
    assert resp.status_code == 201


@pytest.mark.parametrize("password", ["", "   ", "elevenchars"])
async def test_update_me_rejects_weak_password(
    client: AsyncClient, facilitator_token: str, password: str
):
    resp = await client.put(
        "/api/auth/me",
        json={"password": password},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert resp.status_code == 422


# ── #14: token revocation via token_valid_after ───────────────────────────────


async def test_token_valid_after_revokes_earlier_token(
    client: AsyncClient, session: AsyncSession, facilitator: User, facilitator_token: str
):
    auth = {"Authorization": f"Bearer {facilitator_token}"}
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 200
    # Cutoff strictly after the token's issue time → the existing token is revoked.
    facilitator.token_valid_after = datetime.now(UTC) + timedelta(seconds=2)
    session.add(facilitator)
    await session.commit()
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 401


async def test_token_issued_after_cutoff_is_accepted(
    client: AsyncClient, session: AsyncSession, facilitator: User
):
    facilitator.token_valid_after = datetime.now(UTC) - timedelta(seconds=2)
    session.add(facilitator)
    await session.commit()
    token = create_access_token(subject=facilitator.email, role=facilitator.role.value)
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


async def test_password_change_sets_token_valid_after(
    client: AsyncClient, session: AsyncSession, facilitator: User, facilitator_token: str
):
    resp = await client.put(
        "/api/auth/me",
        json={"password": "brandnewpass1234"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert resp.status_code == 200
    await session.refresh(facilitator)
    assert facilitator.token_valid_after is not None
    # The re-issued token must ride ONLY in the httpOnly cookie, never the body,
    # so page-context JS can't read it (#66 security review).
    assert "access_token" not in resp.json()


async def test_password_change_clears_must_change_password(
    client: AsyncClient, session: AsyncSession, facilitator: User, facilitator_token: str
):
    """#66: changing your own password satisfies an admin-set temp-password flag."""
    facilitator.must_change_password = True
    session.add(facilitator)
    await session.commit()
    resp = await client.put(
        "/api/auth/me",
        json={"password": "brandnewpass1234"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False
    await session.refresh(facilitator)
    assert facilitator.must_change_password is False


async def test_must_change_password_gate_blocks_writes(
    client: AsyncClient, session: AsyncSession, facilitator: User, facilitator_token: str
):
    """#66 server-side backstop: while the temp-password flag is set, app writes
    are 403'd, but reads and the password-change endpoint stay reachable so the
    user can satisfy the requirement rather than driving the app via the API."""
    facilitator.must_change_password = True
    session.add(facilitator)
    await session.commit()
    auth = {"Authorization": f"Bearer {facilitator_token}"}

    # Reads still work — the shell needs them to funnel the user to /settings.
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 200
    assert (await client.get("/api/exercises", headers=auth)).status_code == 200

    # A state-changing app request is blocked by the gate (before any role/body check).
    blocked = await client.post(
        "/api/exercises", headers=auth, json={"title": "Blocked", "scenario_id": 1}
    )
    assert blocked.status_code == 403

    # The auth namespace stays open, so the user can still change their password.
    ok = await client.put("/api/auth/me", headers=auth, json={"password": "brandnewpass1234"})
    assert ok.status_code == 200
    await session.refresh(facilitator)
    assert facilitator.must_change_password is False
