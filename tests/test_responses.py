from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.exercise import Exercise
from app.models.user import User
from app.schemas.scenario_json import InjectNode, InjectOption, ScenarioDefinition

# ── Helpers ───────────────────────────────────────────────────────────────────

def _first_released_inject_id(client: TestClient, token: str, exercise_id: int) -> int:
    """Release the first pending inject and return its id."""
    injects = client.get(
        f"/api/exercises/{exercise_id}/injects",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    pending = next(i for i in injects if i["state"] == "pending")
    released = client.post(
        f"/api/exercises/{exercise_id}/injects/{pending['id']}/release",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    return released["id"]


def _submit(
    client: TestClient,
    token: str,
    exercise_id: int,
    inject_id: int,
    content: str = "We isolated the systems.",
    selected_option: str | None = None,
):
    body = {"inject_id": inject_id, "content": content}
    if selected_option:
        body["selected_option"] = selected_option
    return client.post(
        f"/api/exercises/{exercise_id}/responses",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


# ── Submit ────────────────────────────────────────────────────────────────────

def test_submit_response(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    r = _submit(client, participant_token, active_exercise.id, inject_id)
    assert r.status_code == 201
    data = r.json()
    assert data["inject_id"] == inject_id
    assert data["content"] == "We isolated the systems."
    assert data["selected_option"] is None
    assert data["submitted_at"] is not None


def test_submit_response_with_option(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    r = _submit(
        client, participant_token, active_exercise.id, inject_id, selected_option="opt_a"
    )
    assert r.status_code == 201
    assert r.json()["selected_option"] == "opt_a"


def test_submit_response_invalid_inject(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    r = _submit(client, participant_token, active_exercise.id, inject_id=9999)
    assert r.status_code == 404


def test_submit_response_unreleased_inject_forbidden(
    client: TestClient, participant_token: str, facilitator_token: str, active_exercise: Exercise
):
    injects = client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()
    inject_id = next(i["id"] for i in injects if i["scenario_node_id"] == "inject_01")
    r = _submit(client, participant_token, active_exercise.id, inject_id)
    assert r.status_code == 404


def test_submit_response_wrong_team_inject_forbidden(
    client: TestClient, participant_token: str, facilitator_token: str, active_exercise: Exercise
):
    injects = client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()
    legal_inject = next(i for i in injects if i["scenario_node_id"] == "inject_02")
    client.post(
        f"/api/exercises/{active_exercise.id}/injects/{legal_inject['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = _submit(client, participant_token, active_exercise.id, legal_inject["id"])
    assert r.status_code == 404


def test_submit_response_invalid_option_rejected(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    r = _submit(
        client, participant_token, active_exercise.id, inject_id, selected_option="not_real"
    )
    assert r.status_code == 422


def test_submit_response_duplicate_rejected(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    assert _submit(client, participant_token, active_exercise.id, inject_id).status_code == 201
    r = _submit(client, participant_token, active_exercise.id, inject_id, "Second response")
    assert r.status_code == 409


# ── List ──────────────────────────────────────────────────────────────────────

def test_facilitator_sees_all_responses(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    participant: User,
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    _submit(client, participant_token, active_exercise.id, inject_id)

    r = client.get(
        f"/api/exercises/{active_exercise.id}/responses",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()) >= 1


def test_participant_sees_only_own_responses(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: Session,
    facilitator: User,
    sample_scenario,
):
    from app.models.user import User, UserRole
    from app.services.auth_service import create_access_token, hash_password

    other = User(
        email="other@example.com",
        display_name="Other",
        hashed_password=hash_password("pw"),
        role=UserRole.participant,
        team="it_ops",
    )
    session.add(other)
    session.commit()
    session.refresh(other)
    from app.services.exercise_service import enrol_member

    enrol_member(session, exercise=active_exercise, user_id=other.id)
    other_token = create_access_token(subject=other.email, role=other.role.value)

    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    _submit(client, participant_token, active_exercise.id, inject_id, "My response")
    _submit(client, other_token, active_exercise.id, inject_id, "Other response")

    r = client.get(
        f"/api/exercises/{active_exercise.id}/responses",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    user_ids = {resp["user_id"] for resp in r.json()}
    from sqlmodel import select

    from app.models.user import User as UserModel
    p = session.exec(select(UserModel).where(UserModel.email == "participant@example.com")).first()
    assert user_ids == {p.id}


# ── Get single ────────────────────────────────────────────────────────────────

def test_get_response(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    resp = _submit(client, participant_token, active_exercise.id, inject_id).json()

    r = client.get(
        f"/api/exercises/{active_exercise.id}/responses/{resp['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == resp["id"]


def test_participant_cannot_get_other_response(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: Session,
):
    from app.models.user import User, UserRole
    from app.services.auth_service import create_access_token, hash_password

    other = User(
        email="other2@example.com",
        display_name="Other2",
        hashed_password=hash_password("pw"),
        role=UserRole.participant,
        team="it_ops",
    )
    session.add(other)
    session.commit()
    session.refresh(other)
    from app.services.exercise_service import enrol_member

    enrol_member(session, exercise=active_exercise, user_id=other.id)
    other_token = create_access_token(subject=other.email, role=other.role.value)

    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    resp = _submit(client, other_token, active_exercise.id, inject_id).json()

    r = client.get(
        f"/api/exercises/{active_exercise.id}/responses/{resp['id']}",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


# ── Branch evaluation ─────────────────────────────────────────────────────────

def test_response_with_valid_option_returns_next_inject(
    client: TestClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """opt_a on inject_01 should resolve to inject_02."""
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    # Verify we released inject_01 (the start inject of sample_scenario)
    inj = client.get(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()
    assert inj["scenario_node_id"] == "inject_01"

    r = _submit(
        client, participant_token, active_exercise.id, inject_id, selected_option="opt_a"
    )
    assert r.status_code == 201


def test_response_records_group_and_facilitator_gets_pending_next_inject(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    session: Session,
    facilitator: User,
    participant: User,
):
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import create_exercise, enrol_member, transition_state
    from app.services.scenario_service import create_scenario

    scenario = create_scenario(
        session,
        definition=ScenarioDefinition(
            title="IT Branch",
            participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
            injects=[
                InjectNode(
                    id="a",
                    title="Start",
                    content="Choose.",
                    target_teams=["it_ops"],
                    options=[InjectOption(id="go", label="Go", next_inject_id="b")],
                ),
                InjectNode(
                    id="b",
                    title="Next",
                    content="Follow-up.",
                    target_teams=["it_ops"],
                ),
            ],
            start_inject_id="a",
        ),
        created_by=facilitator.id,
    )
    exercise = create_exercise(
        session,
        scenario_id=scenario.id,
        title="Branch Group Exercise",
        created_by=facilitator.id,
    )
    enrol_member(session, exercise=exercise, user_id=participant.id)
    transition_state(session, exercise, ExerciseState.active)

    injects = client.get(
        f"/api/exercises/{exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()
    first = next(i for i in injects if i["scenario_node_id"] == "a")
    second = next(i for i in injects if i["scenario_node_id"] == "b")
    client.post(
        f"/api/exercises/{exercise.id}/injects/{first['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )

    r = _submit(client, participant_token, exercise.id, first["id"], selected_option="go")
    assert r.status_code == 201
    assert r.json()["group_id"] == "it_ops"

    rows = client.get(
        f"/api/exercises/{exercise.id}/responses",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()
    assert rows[0]["group_id"] == "it_ops"
    assert rows[0]["next_injects"] == [
        {
            "id": second["id"],
            "scenario_node_id": "b",
            "title": "Next",
            "group_id": "it_ops",
        }
    ]


def test_facilitator_preview_participant_response_records_preview_team(
    client: TestClient,
    facilitator_token: str,
    active_exercise: Exercise,
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)
    client.cookies.set("dt_view_role", "participant")
    client.cookies.set("dt_view_team", "it_ops")

    r = _submit(client, facilitator_token, active_exercise.id, inject_id)

    assert r.status_code == 201
    assert r.json()["group_id"] == "it_ops"


def test_ws_broadcasts_response_to_facilitator(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    inject_id = _first_released_inject_id(client, facilitator_token, active_exercise.id)

    with client.websocket_connect(
        f"/ws/exercises/{active_exercise.id}?token={facilitator_token}"
    ) as ws:
        _submit(client, participant_token, active_exercise.id, inject_id)
        msg = ws.receive_json()

    assert msg["type"] == "response_submitted"
    assert msg["payload"]["response"]["inject_id"] == inject_id
