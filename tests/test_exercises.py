from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.exercise import Exercise, ExerciseState
from app.models.user import User
from app.services.exercise_service import transition_state

# ── CRUD ──────────────────────────────────────────────────────────────────────

def test_create_exercise(client: TestClient, facilitator_token: str, sample_scenario):
    r = client.post(
        "/api/exercises",
        json={"scenario_id": sample_scenario.id, "title": "My Exercise"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "My Exercise"
    assert data["state"] == "draft"
    assert data["current_node_id"] == "inject_01"
    assert data["llm_enabled"] is False


def test_create_exercise_with_llm(client: TestClient, facilitator_token: str, sample_scenario):
    r = client.post(
        "/api/exercises",
        json={"scenario_id": sample_scenario.id, "title": "LLM Exercise", "llm_enabled": True},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    assert r.json()["llm_enabled"] is True


def test_create_exercise_missing_scenario(client: TestClient, facilitator_token: str):
    r = client.post(
        "/api/exercises",
        json={"scenario_id": 9999, "title": "Ghost"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


def test_create_exercise_participant_forbidden(
    client: TestClient, participant_token: str, sample_scenario
):
    r = client.post(
        "/api/exercises",
        json={"scenario_id": sample_scenario.id, "title": "No"},
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


def test_list_exercises(client: TestClient, facilitator_token: str, draft_exercise):
    r = client.get("/api/exercises", headers={"Authorization": f"Bearer {facilitator_token}"})
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()]
    assert draft_exercise.id in ids


def test_list_exercises_participant_allowed(
    client: TestClient, participant_token: str, draft_exercise
):
    r = client.get("/api/exercises", headers={"Authorization": f"Bearer {participant_token}"})
    assert r.status_code == 200
    assert r.json() == []


def test_participant_sees_enrolled_exercise(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    r = client.get("/api/exercises", headers={"Authorization": f"Bearer {participant_token}"})
    assert r.status_code == 200
    assert [ex["id"] for ex in r.json()] == [active_exercise.id]


def test_facilitator_preview_participant_still_lists_exercises_for_testing(
    client: TestClient,
    facilitator_token: str,
    draft_exercise: Exercise,
    active_exercise: Exercise,
):
    assert draft_exercise.id is not None
    assert active_exercise.id is not None
    client.cookies.set("dt_view_role", "participant")
    r = client.get("/api/exercises", headers={"Authorization": f"Bearer {facilitator_token}"})
    assert r.status_code == 200
    ids = {ex["id"] for ex in r.json()}
    assert draft_exercise.id in ids
    assert active_exercise.id in ids


def test_get_exercise(client: TestClient, facilitator_token: str, draft_exercise):
    r = client.get(
        f"/api/exercises/{draft_exercise.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == draft_exercise.id


def test_get_exercise_participant_not_enrolled_forbidden(
    client: TestClient, participant_token: str, draft_exercise
):
    r = client.get(
        f"/api/exercises/{draft_exercise.id}",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


def test_get_exercise_not_found(client: TestClient, facilitator_token: str):
    r = client.get("/api/exercises/9999", headers={"Authorization": f"Bearer {facilitator_token}"})
    assert r.status_code == 404


def test_update_exercise(client: TestClient, facilitator_token: str, draft_exercise):
    r = client.put(
        f"/api/exercises/{draft_exercise.id}",
        json={"title": "Renamed", "llm_enabled": True},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Renamed"
    assert data["llm_enabled"] is True


def test_delete_draft_exercise(
    client: TestClient, facilitator_token: str, session: Session, sample_scenario, facilitator: User
):
    from app.services.exercise_service import create_exercise

    ex = create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="To Delete",
        created_by=facilitator.id,
    )
    r = client.delete(
        f"/api/exercises/{ex.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 204
    assert session.get(Exercise, ex.id) is None


def test_delete_active_exercise_forbidden(
    client: TestClient, facilitator_token: str, active_exercise
):
    r = client.delete(
        f"/api/exercises/{active_exercise.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def test_start_exercise(client: TestClient, facilitator_token: str, draft_exercise):
    r = client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "active"
    assert data["started_at"] is not None


def test_pause_exercise(client: TestClient, facilitator_token: str, active_exercise):
    r = client.post(
        f"/api/exercises/{active_exercise.id}/pause",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "paused"


def test_resume_exercise(
    client: TestClient, facilitator_token: str, session: Session, active_exercise
):
    paused = transition_state(session, active_exercise, ExerciseState.paused)
    r = client.post(
        f"/api/exercises/{paused.id}/resume",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "active"


def test_complete_active_exercise(client: TestClient, facilitator_token: str, active_exercise):
    r = client.post(
        f"/api/exercises/{active_exercise.id}/complete",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "completed"
    assert data["ended_at"] is not None


def test_complete_paused_exercise(
    client: TestClient, facilitator_token: str, session: Session, active_exercise
):
    paused = transition_state(session, active_exercise, ExerciseState.paused)
    r = client.post(
        f"/api/exercises/{paused.id}/complete",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "completed"


def test_invalid_transition_draft_to_completed(
    client: TestClient, facilitator_token: str, draft_exercise
):
    r = client.post(
        f"/api/exercises/{draft_exercise.id}/complete",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


def test_invalid_transition_completed_to_active(
    client: TestClient, facilitator_token: str, session: Session, active_exercise
):
    transition_state(session, active_exercise, ExerciseState.completed)
    r = client.post(
        f"/api/exercises/{active_exercise.id}/start",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


def test_start_sets_started_at_only_once(
    client: TestClient, facilitator_token: str, session: Session, draft_exercise
):
    client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    first_start = session.get(Exercise, draft_exercise.id).started_at

    client.post(
        f"/api/exercises/{draft_exercise.id}/pause",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    client.post(
        f"/api/exercises/{draft_exercise.id}/resume",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    second_start = session.get(Exercise, draft_exercise.id).started_at
    assert first_start == second_start


def test_lifecycle_participant_forbidden(
    client: TestClient, participant_token: str, draft_exercise
):
    r = client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


# ── Members ───────────────────────────────────────────────────────────────────

def test_enrol_member(
    client: TestClient, facilitator_token: str, draft_exercise, participant: User
):
    r = client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    assert r.json()["user_id"] == participant.id


def test_enrol_member_idempotent(
    client: TestClient, facilitator_token: str, draft_exercise, participant: User
):
    client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201

    members = client.get(
        f"/api/exercises/{draft_exercise.id}/members",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()
    assert sum(1 for m in members if m["user_id"] == participant.id) == 1


def test_list_members(
    client: TestClient, facilitator_token: str, draft_exercise, participant: User
):
    client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = client.get(
        f"/api/exercises/{draft_exercise.id}/members",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert any(m["user_id"] == participant.id for m in r.json())


def test_remove_member(
    client: TestClient, facilitator_token: str, draft_exercise, participant: User
):
    client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = client.delete(
        f"/api/exercises/{draft_exercise.id}/members/{participant.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 204

    members = client.get(
        f"/api/exercises/{draft_exercise.id}/members",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()
    assert not any(m["user_id"] == participant.id for m in members)


def test_remove_member_not_found(
    client: TestClient, facilitator_token: str, draft_exercise, participant: User
):
    r = client.delete(
        f"/api/exercises/{draft_exercise.id}/members/{participant.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


def test_enrol_member_participant_forbidden(
    client: TestClient, participant_token: str, draft_exercise, participant: User
):
    r = client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403
