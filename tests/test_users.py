from datetime import UTC, datetime, timedelta

import jwt
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.user import User, UserRole


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _backdated_token(email: str, role: str) -> str:
    """A token issued 5s ago, so a token_valid_after cutoff set 'now' revokes it
    deterministically (iat is second-precision) — avoids same-second flakiness."""
    now = datetime.now(UTC) - timedelta(seconds=5)
    payload = {
        "sub": email, "role": role, "is_admin": False,
        "iat": now, "exp": now + timedelta(hours=1),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


async def test_admin_create_user(client: AsyncClient, admin_token: str):
    """#67: an admin provisions an account with a chosen role (the invite path)."""
    resp = await client.post(
        "/api/users",
        headers=_headers(admin_token),
        json={
            "email": "invited@example.com",
            "display_name": "Invited",
            "password": "secret123456",
            "role": "facilitator",
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["email"] == "invited@example.com"
    assert data["role"] == "facilitator"
    assert "hashed_password" not in data

    # The new user is enrollable via the facilitator picker.
    listing = await client.get("/api/users", headers=_headers(admin_token))
    assert any(u["email"] == "invited@example.com" for u in listing.json())


async def test_list_users_exposes_is_admin(
    client: AsyncClient, admin_token: str, admin: User, participant: User
):
    """The admin console needs is_admin to flag admins (a facilitator may also be
    an admin), so the listing must carry it."""
    listing = (await client.get("/api/users", headers=_headers(admin_token))).json()
    by_email = {u["email"]: u for u in listing}
    assert by_email[admin.email]["is_admin"] is True
    assert by_email[participant.email]["is_admin"] is False


async def test_admin_create_user_duplicate_email(
    client: AsyncClient, admin_token: str, facilitator: User
):
    resp = await client.post(
        "/api/users",
        headers=_headers(admin_token),
        json={
            "email": facilitator.email,
            "display_name": "Dup",
            "password": "secret123456",
        },
    )
    assert resp.status_code == 409


async def test_create_user_requires_admin(client: AsyncClient, facilitator_token: str):
    """A facilitator (non-admin) cannot provision accounts."""
    resp = await client.post(
        "/api/users",
        headers=_headers(facilitator_token),
        json={
            "email": "blocked@example.com",
            "display_name": "Blocked",
            "password": "secret123456",
        },
    )
    assert resp.status_code == 403


async def test_create_user_rejects_weak_password(client: AsyncClient, admin_token: str):
    resp = await client.post(
        "/api/users",
        headers=_headers(admin_token),
        json={
            "email": "weak@example.com",
            "display_name": "Weak",
            "password": "short",
        },
    )
    assert resp.status_code == 422


# ── #66: admin-driven password reset ──────────────────────────────────────────


async def test_admin_reset_password(
    client: AsyncClient, session: AsyncSession, admin_token: str, participant: User
):
    """An admin sets a temporary password: sessions are revoked, the flag is set,
    and the user can sign in with the new password."""
    old_token = _backdated_token(participant.email, participant.role.value)
    # The old session works before the reset.
    assert (await client.get("/api/auth/me", headers=_headers(old_token))).status_code == 200

    resp = await client.post(
        f"/api/users/{participant.id}/reset-password",
        headers=_headers(admin_token),
        json={"password": "temp-passw0rd-123"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["must_change_password"] is True
    assert "hashed_password" not in data and "password" not in data

    # The pre-reset session is now revoked.
    assert (await client.get("/api/auth/me", headers=_headers(old_token))).status_code == 401

    await session.refresh(participant)
    assert participant.token_valid_after is not None
    assert participant.must_change_password is True

    # The participant can sign in with the temporary password, and /auth/me reports
    # the must-change flag so the frontend can hold them on /settings.
    login = await client.post(
        "/api/auth/login",
        json={"email": participant.email, "password": "temp-passw0rd-123"},
    )
    assert login.status_code == 200, login.text
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True


async def test_admin_reset_password_without_force_change(
    client: AsyncClient, session: AsyncSession, admin_token: str, participant: User
):
    resp = await client.post(
        f"/api/users/{participant.id}/reset-password",
        headers=_headers(admin_token),
        json={"password": "temp-passw0rd-123", "must_change_password": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_password"] is False
    await session.refresh(participant)
    assert participant.must_change_password is False


async def test_reset_password_requires_admin(
    client: AsyncClient, facilitator_token: str, participant: User
):
    resp = await client.post(
        f"/api/users/{participant.id}/reset-password",
        headers=_headers(facilitator_token),
        json={"password": "temp-passw0rd-123"},
    )
    assert resp.status_code == 403


async def test_reset_password_rejects_weak_password(
    client: AsyncClient, admin_token: str, participant: User
):
    resp = await client.post(
        f"/api/users/{participant.id}/reset-password",
        headers=_headers(admin_token),
        json={"password": "short"},
    )
    assert resp.status_code == 422


async def test_reset_password_unknown_user(client: AsyncClient, admin_token: str):
    resp = await client.post(
        "/api/users/999999/reset-password",
        headers=_headers(admin_token),
        json={"password": "temp-passw0rd-123"},
    )
    assert resp.status_code == 404


async def test_reset_password_rejects_sso_account(
    client: AsyncClient, session: AsyncSession, admin_token: str
):
    """An OIDC-provisioned account has no local password and signs in via SSO."""
    sso_user = User(
        email="sso@example.com",
        display_name="SSO User",
        hashed_password=None,
        role=UserRole.participant,
        auth_provider="entra",
        subject="ext-subject-1",
    )
    session.add(sso_user)
    await session.commit()
    await session.refresh(sso_user)

    resp = await client.post(
        f"/api/users/{sso_user.id}/reset-password",
        headers=_headers(admin_token),
        json={"password": "temp-passw0rd-123"},
    )
    assert resp.status_code == 400
