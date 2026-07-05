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


async def test_ws_observer_receives_group_scoped_inject(
    client: AsyncClient,
    session: AsyncSession,
    facilitator_token: str,
    active_exercise: Exercise,
):
    """An observer (global read-visibility, no team) receives the live
    inject_released frame for a group-scoped inject, matching HTTP visibility (#38)."""
    from app.services.exercise_service import enrol_member

    observer = User(
        email="ws-observer@example.com",
        display_name="Observer",
        hashed_password=hash_password("pw"),
        role=UserRole.observer,
    )
    session.add(observer)
    await session.commit()
    await session.refresh(observer)
    await enrol_member(session, exercise=active_exercise, user_id=observer.id)
    token = create_access_token(subject=observer.email, role=observer.role.value)

    create_r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={"title": "Legal Only", "content": "For legal", "target_teams": ["legal"]},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    inject_id = create_r.json()["id"]

    async with aconnect_ws(_ws_url(active_exercise.id, token), client) as ws:
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        msg = await ws.receive_json()

    assert msg["type"] == "inject_released"
    assert msg["payload"]["id"] == inject_id


async def test_ws_handshake_releases_db_session(
    client: AsyncClient,
    facilitator_token: str,
    active_exercise: Exercise,
):
    """Several concurrent sockets plus a concurrent HTTP request all succeed — the
    handshake no longer holds a pooled connection for the socket lifetime (#35).
    (Cannot reproduce true pool exhaustion here: the test engine uses NullPool.)"""
    import contextlib

    async with contextlib.AsyncExitStack() as stack:
        for _ in range(3):
            ws = await stack.enter_async_context(
                aconnect_ws(_ws_url(active_exercise.id, facilitator_token), client)
            )
            await ws.send_json({"type": "ping"})
            assert (await ws.receive_json())["type"] == "pong"
        # With sockets open, a normal HTTP request still obtains a DB connection.
        r = await client.get("/api/health")
        assert r.status_code == 200


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


async def test_ws_participant_cannot_spoof_team_via_view_team(
    client: AsyncClient,
    participant: User,
    participant_token: str,
    active_exercise: Exercise,
):
    """A genuine participant passing a foreign view_team is still bucketed to
    their enrolled group, so they cannot subscribe to another team's broadcasts (#30)."""
    from app.services.ws_manager import manager

    url = f"{_ws_url(active_exercise.id, participant_token)}&view_team=legal"
    async with aconnect_ws(url, client) as ws:
        await ws.send_json({"type": "ping"})
        await ws.receive_json()
        mine = [
            c for c in manager._rooms.get(active_exercise.id, [])
            if c.user_id == participant.id
        ]
        assert mine, "participant connection should be registered"
        assert all(c.group_id == "it_ops" for c in mine)
        assert all(c.group_id != "legal" for c in mine)


async def test_ws_facilitator_preview_derives_group_from_view_team(
    client: AsyncClient,
    facilitator: User,
    facilitator_token: str,
    active_exercise: Exercise,
):
    """A real facilitator previewing as a participant *does* take the preview team (#30)."""
    from app.services.ws_manager import manager

    url = (
        f"{_ws_url(active_exercise.id, facilitator_token)}"
        "&view_role=participant&view_team=legal"
    )
    async with aconnect_ws(url, client) as ws:
        await ws.send_json({"type": "ping"})
        await ws.receive_json()
        mine = [
            c for c in manager._rooms.get(active_exercise.id, [])
            if c.user_id == facilitator.id
        ]
        assert mine and all(c.group_id == "legal" for c in mine)
