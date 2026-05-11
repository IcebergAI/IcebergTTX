from fastapi.testclient import TestClient

from app.models.exercise import Exercise
from app.models.user import User

# ── Helpers ───────────────────────────────────────────────────────────────────

def _send(
    client: TestClient,
    token: str,
    exercise_id: int,
    direction: str = "outbound",
    subject: str = "Test",
    body: str = "Body text",
    external_entity: str | None = None,
    visible_to_teams: list[str] | None = None,
):
    payload = {"direction": direction, "subject": subject, "body": body}
    if external_entity:
        payload["external_entity"] = external_entity
    if visible_to_teams:
        payload["visible_to_teams"] = visible_to_teams
    return client.post(
        f"/api/exercises/{exercise_id}/communications",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def _inject_comm(
    client: TestClient,
    token: str,
    exercise_id: int,
    external_entity: str = "ICO",
    subject: str = "Advisory",
    body: str = "Please note…",
    visible_to_teams: list[str] | None = None,
):
    payload = {"external_entity": external_entity, "subject": subject, "body": body}
    if visible_to_teams:
        payload["visible_to_teams"] = visible_to_teams
    return client.post(
        f"/api/exercises/{exercise_id}/communications/inject",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


# ── Send ──────────────────────────────────────────────────────────────────────

def test_send_outbound(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    r = _send(client, participant_token, active_exercise.id)
    assert r.status_code == 201
    data = r.json()
    assert data["direction"] == "outbound"
    assert data["subject"] == "Test"


def test_inject_inbound_facilitator(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    r = _inject_comm(client, facilitator_token, active_exercise.id)
    assert r.status_code == 201
    data = r.json()
    assert data["direction"] == "inbound"
    assert data["external_entity"] == "ICO"


def test_inject_inbound_participant_forbidden(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    r = _inject_comm(client, participant_token, active_exercise.id)
    assert r.status_code == 403


# ── List ──────────────────────────────────────────────────────────────────────

def test_list_comms_all_visible(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    _send(client, participant_token, active_exercise.id, subject="Message A")
    _inject_comm(client, facilitator_token, active_exercise.id, subject="ICO Advisory")

    r = client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "Message A" in subjects
    assert "ICO Advisory" in subjects


def test_visibility_filtering(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """Comm visible only to 'legal' should NOT appear for it_ops participant."""
    _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="Legal Only", visible_to_teams=["legal"]
    )
    r = client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {participant_token}"},  # participant is it_ops
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "Legal Only" not in subjects


def test_visibility_own_team(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """Comm targeted to it_ops is visible to the it_ops participant."""
    _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="IT Ops Only", visible_to_teams=["it_ops"]
    )
    r = client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "IT Ops Only" in subjects


def test_facilitator_sees_all_regardless_of_visibility(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="Secret", visible_to_teams=["legal"]
    )
    r = client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "Secret" in subjects


# ── Mark read ─────────────────────────────────────────────────────────────────

def test_get_comm_marks_read(
    client: TestClient, participant_token: str, facilitator_token: str,
    active_exercise: Exercise, participant: User
):
    comm = _send(client, participant_token, active_exercise.id).json()

    r = client.get(
        f"/api/exercises/{active_exercise.id}/communications/{comm['id']}",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert participant.id in data["read_by"]


def test_get_comm_not_found(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    r = client.get(
        f"/api/exercises/{active_exercise.id}/communications/9999",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


# ── WS broadcast ──────────────────────────────────────────────────────────────

def test_ws_receives_communication(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    with client.websocket_connect(
        f"/ws/exercises/{active_exercise.id}?token={participant_token}"
    ) as ws:
        _inject_comm(client, facilitator_token, active_exercise.id, subject="WS Test")
        msg = ws.receive_json()

    assert msg["type"] == "communication_received"
    assert msg["payload"]["subject"] == "WS Test"


def test_ws_visibility_filtered_broadcast(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """Comm targeted to 'legal' should NOT arrive at the it_ops participant's WS."""
    with client.websocket_connect(
        f"/ws/exercises/{active_exercise.id}?token={participant_token}"
    ) as ws:
        _inject_comm(
            client, facilitator_token, active_exercise.id,
            subject="Legal Only WS", visible_to_teams=["legal"]
        )
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()

    # Should receive pong, not the communication
    assert msg["type"] == "pong"
