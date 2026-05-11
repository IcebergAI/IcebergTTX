import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.exercise import Exercise
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ws_url(exercise_id: int, token: str) -> str:
    return f"/ws/exercises/{exercise_id}?token={token}"


# ── Connection ────────────────────────────────────────────────────────────────

def test_ws_connect_valid_token(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    with client.websocket_connect(_ws_url(active_exercise.id, facilitator_token)) as ws:
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
    assert msg["type"] == "pong"
    assert msg["exercise_id"] == active_exercise.id


def test_ws_connect_invalid_token(client: TestClient, active_exercise: Exercise):
    with pytest.raises(Exception):
        with client.websocket_connect(_ws_url(active_exercise.id, "bad.token.here")) as ws:
            ws.receive_json()


def test_ws_connect_participant(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    with client.websocket_connect(_ws_url(active_exercise.id, participant_token)) as ws:
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
    assert msg["type"] == "pong"


# ── Heartbeat ─────────────────────────────────────────────────────────────────

def test_ws_ping_pong(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    with client.websocket_connect(_ws_url(active_exercise.id, facilitator_token)) as ws:
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg["type"] == "pong"
        assert "timestamp" in msg
        assert "payload" in msg


# ── Inject released event ─────────────────────────────────────────────────────

def test_ws_receives_inject_released(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    # Create an inject first
    create_r = client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={"title": "WS Test Inject", "content": "What do you do?"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    inject_id = create_r.json()["id"]

    with client.websocket_connect(_ws_url(active_exercise.id, participant_token)) as ws:
        client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        msg = ws.receive_json()

    assert msg["type"] == "inject_released"
    assert msg["payload"]["id"] == inject_id
    assert msg["payload"]["state"] == "released"


def test_ws_team_targeted_inject_reaches_team_member(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    participant: User,
):
    """Participant on it_ops team receives inject targeted to it_ops."""
    assert participant.team == "it_ops"

    create_r = client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={
            "title": "IT Ops Only",
            "content": "For IT only",
            "target_teams": ["it_ops"],
        },
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    inject_id = create_r.json()["id"]

    with client.websocket_connect(_ws_url(active_exercise.id, participant_token)) as ws:
        client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        msg = ws.receive_json()

    assert msg["type"] == "inject_released"
    assert msg["payload"]["target_teams"] == ["it_ops"]


def test_ws_facilitator_always_receives_team_targeted(
    client: TestClient,
    facilitator_token: str,
    active_exercise: Exercise,
):
    """Facilitator receives team-targeted injects even though they have no team."""
    create_r = client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        json={
            "title": "Legal Only",
            "content": "For legal only",
            "target_teams": ["legal"],
        },
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    inject_id = create_r.json()["id"]

    with client.websocket_connect(_ws_url(active_exercise.id, facilitator_token)) as ws:
        client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
        msg = ws.receive_json()

    assert msg["type"] == "inject_released"


def test_ws_inactive_user_rejected(
    client: TestClient,
    session: Session,
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
    session.commit()
    session.refresh(inactive)

    token = create_access_token(subject=inactive.email, role=inactive.role.value)
    with pytest.raises(Exception):
        with client.websocket_connect(_ws_url(active_exercise.id, token)) as ws:
            ws.receive_json()
