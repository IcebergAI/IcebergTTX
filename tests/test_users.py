from httpx import AsyncClient

from app.models.user import User


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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
