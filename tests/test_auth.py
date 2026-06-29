from httpx import AsyncClient

from app.models.user import User


async def test_register_success(client: AsyncClient):
    resp = await client.post("/api/auth/register", json={
        "email": "new@example.com",
        "display_name": "New User",
        "password": "secret123",
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
        "password": "secret123",
    })
    assert resp.status_code == 409


async def test_register_ignores_elevated_role(client: AsyncClient):
    """Regression for #8: self-registration must never grant a privileged role."""
    resp = await client.post("/api/auth/register", json={
        "email": "fac2@example.com",
        "display_name": "Fac 2",
        "password": "secret123",
        "role": "facilitator",
    })
    assert resp.status_code == 201
    assert resp.json()["role"] == "participant"


async def test_register_ignores_observer_role(client: AsyncClient):
    resp = await client.post("/api/auth/register", json={
        "email": "obs@example.com",
        "display_name": "Obs",
        "password": "secret123",
        "role": "observer",
    })
    assert resp.status_code == 201
    assert resp.json()["role"] == "participant"


async def test_login_success(client: AsyncClient, facilitator: User):
    resp = await client.post("/api/auth/login", json={
        "email": facilitator.email,
        "password": "password123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


async def test_logout_clears_cookie_backed_session(client: AsyncClient, facilitator: User):
    login_resp = await client.post("/api/auth/login", json={
        "email": facilitator.email,
        "password": "password123",
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
        "password": "password123",
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
