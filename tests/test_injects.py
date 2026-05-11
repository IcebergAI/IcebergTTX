from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.exercise import Exercise
from app.models.user import User

# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_inject(
    client: TestClient,
    token: str,
    exercise_id: int,
    title: str = "Test Inject",
    content: str = "What do you do?",
    target_teams: list[str] | None = None,
    sequence_order: int = 0,
):
    body = {"title": title, "content": content, "sequence_order": sequence_order}
    if target_teams is not None:
        body["target_teams"] = target_teams
    return client.post(
        f"/api/exercises/{exercise_id}/injects",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────

def test_create_inject(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    r = _create_inject(client, facilitator_token, active_exercise.id)
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Test Inject"
    assert data["state"] == "pending"
    assert data["target_teams"] is None


def test_create_inject_with_teams(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    r = _create_inject(
        client, facilitator_token, active_exercise.id, target_teams=["it_ops", "legal"]
    )
    assert r.status_code == 201
    assert r.json()["target_teams"] == ["it_ops", "legal"]


def test_create_inject_participant_forbidden(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    r = _create_inject(client, participant_token, active_exercise.id)
    assert r.status_code == 403


def test_list_injects(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    # Exercise is pre-seeded from the scenario; add two more at higher sequence_order
    _create_inject(client, facilitator_token, active_exercise.id, title="A", sequence_order=10)
    _create_inject(client, facilitator_token, active_exercise.id, title="B", sequence_order=11)
    r = client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    titles = [i["title"] for i in r.json()]
    # Verify A comes before B and both are present
    assert "A" in titles and "B" in titles
    assert titles.index("A") < titles.index("B")


def test_list_injects_participant_allowed(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    r = client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200


def test_get_inject(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    created = _create_inject(client, facilitator_token, active_exercise.id).json()
    r = client.get(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]


def test_get_inject_wrong_exercise(
    client: TestClient,
    facilitator_token: str,
    active_exercise: Exercise,
    session: Session,
    facilitator: User,
    sample_scenario,
):
    from app.services.exercise_service import create_exercise

    other = create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Other",
        created_by=facilitator.id,
    )
    created = _create_inject(client, facilitator_token, active_exercise.id).json()
    r = client.get(
        f"/api/exercises/{other.id}/injects/{created['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


def test_delete_inject(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    created = _create_inject(client, facilitator_token, active_exercise.id).json()
    r = client.delete(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 204


# ── Release ───────────────────────────────────────────────────────────────────

def test_release_inject(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    created = _create_inject(client, facilitator_token, active_exercise.id).json()
    r = client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "released"
    assert data["released_at"] is not None
    assert data["released_by"] is not None


def test_release_already_released(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    created = _create_inject(client, facilitator_token, active_exercise.id).json()
    client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


def test_release_inject_participant_forbidden(
    client: TestClient, participant_token: str, facilitator_token: str, active_exercise: Exercise
):
    created = _create_inject(client, facilitator_token, active_exercise.id).json()
    r = client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


def test_release_broadcast_all(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    """Broadcast inject (no target_teams) is released to all."""
    created = _create_inject(
        client, facilitator_token, active_exercise.id, target_teams=None
    ).json()
    r = client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["target_teams"] is None


def test_release_broadcast_team_targeted(
    client: TestClient, facilitator_token: str, active_exercise: Exercise
):
    """Team-targeted inject is released only to named teams."""
    created = _create_inject(
        client, facilitator_token, active_exercise.id, target_teams=["it_ops"]
    ).json()
    r = client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["target_teams"] == ["it_ops"]
