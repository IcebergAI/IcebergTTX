import pytest
from httpx import AsyncClient
from httpx_ws import WebSocketDisconnect, aconnect_ws
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ws_url(exercise_id: int, token: str) -> str:
    return f"/ws/exercises/{exercise_id}?token={token}"


# ── Connection ────────────────────────────────────────────────────────────────

async def test_ws_connect_valid_token(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    async with aconnect_ws(_ws_url(active_exercise.id, facilitator_token), client) as ws:
        await ws.send_json({"type": "ping"})
        msg = await ws.receive_json()
    assert msg["type"] == "pong"
    assert msg["exercise_id"] == active_exercise.id


async def test_ws_connect_invalid_token(client: AsyncClient, active_exercise: Exercise):
    with pytest.raises(WebSocketDisconnect):
        async with aconnect_ws(_ws_url(active_exercise.id, "bad.token.here"), client) as ws:
            await ws.receive_json()


async def test_ws_connect_participant(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    async with aconnect_ws(_ws_url(active_exercise.id, participant_token), client) as ws:
        await ws.send_json({"type": "ping"})
        msg = await ws.receive_json()
    assert msg["type"] == "pong"


async def test_ws_connect_facilitator_preview_participant(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    url = f"{_ws_url(active_exercise.id, facilitator_token)}&view_role=participant&view_team=it_ops"
    async with aconnect_ws(url, client) as ws:
        await ws.send_json({"type": "ping"})
        msg = await ws.receive_json()
    assert msg["type"] == "pong"


async def test_ws_connect_nonmember_rejected(
    client: AsyncClient, session: AsyncSession, active_exercise: Exercise
):
    other = User(
        email="ws-nonmember@example.com",
        display_name="Nonmember",
        hashed_password=hash_password("pw"),
        role=UserRole.participant,
        team="it_ops",
    )
    session.add(other)
    await session.commit()
    await session.refresh(other)
    token = create_access_token(subject=other.email, role=other.role.value)

    with pytest.raises(WebSocketDisconnect):
        async with aconnect_ws(_ws_url(active_exercise.id, token), client) as ws:
            await ws.receive_json()


# ── Heartbeat ─────────────────────────────────────────────────────────────────

async def test_ws_ping_pong(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    async with aconnect_ws(_ws_url(active_exercise.id, facilitator_token), client) as ws:
        await ws.send_json({"type": "ping"})
        msg = await ws.receive_json()
        assert msg["type"] == "pong"
        assert "timestamp" in msg
        assert "payload" in msg


# ── Inject released event ─────────────────────────────────────────────────────

async def test_ws_receives_inject_released(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    # Create an inject first
    create_r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={"title": "WS Test Inject", "content": "What do you do?"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    inject_id = create_r.json()["id"]

    async with aconnect_ws(_ws_url(active_exercise.id, participant_token), client) as ws:
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        msg = await ws.receive_json()

    assert msg["type"] == "inject_released"
    assert msg["payload"]["id"] == inject_id
    assert msg["payload"]["state"] == "released"
    assert "options" in msg["payload"]


async def test_ws_team_targeted_inject_reaches_team_member(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    participant: User,
):
    """Participant on it_ops team receives inject targeted to it_ops."""
    assert participant.team == "it_ops"

    create_r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={
            "title": "IT Ops Only",
            "content": "For IT only",
            "target_teams": ["it_ops"],
        },
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    inject_id = create_r.json()["id"]

    async with aconnect_ws(_ws_url(active_exercise.id, participant_token), client) as ws:
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        msg = await ws.receive_json()

    assert msg["type"] == "inject_released"
    assert msg["payload"]["target_teams"] == ["it_ops"]


async def test_ws_facilitator_always_receives_team_targeted(
    client: AsyncClient,
    facilitator_token: str,
    active_exercise: Exercise,
):
    """Facilitator receives team-targeted injects even though they have no team."""
    create_r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={
            "title": "Legal Only",
            "content": "For legal only",
            "target_teams": ["legal"],
        },
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    inject_id = create_r.json()["id"]

    async with aconnect_ws(_ws_url(active_exercise.id, facilitator_token), client) as ws:
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        msg = await ws.receive_json()

    assert msg["type"] == "inject_released"


async def test_ws_inactive_user_rejected(
    client: AsyncClient,
    session: AsyncSession,
    active_exercise: Exercise,
):
    inactive = User(
        email="inactive@example.com",
        display_name="Inactive",
        hashed_password=hash_password("pw"),
        role=UserRole.participant,
        is_active=False,
    )
    session.add(inactive)
    await session.commit()
    await session.refresh(inactive)

    token = create_access_token(subject=inactive.email, role=inactive.role.value)
    with pytest.raises(WebSocketDisconnect):
        async with aconnect_ws(_ws_url(active_exercise.id, token), client) as ws:
            await ws.receive_json()
