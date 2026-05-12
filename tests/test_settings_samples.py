from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.exercise import Exercise, ExerciseMember, ExerciseState
from app.models.inject import Inject, InjectState
from app.models.scenario import Scenario


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_list_sample_scenarios_requires_actual_facilitator(
    client: TestClient, participant_token: str
):
    resp = client.get(
        "/api/settings/samples/scenarios",
        headers=_headers(participant_token),
    )
    assert resp.status_code == 403


def test_list_sample_scenarios(client: TestClient, facilitator_token: str):
    resp = client.get(
        "/api/settings/samples/scenarios",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert {sample["id"] for sample in data} >= {"ransomware_response", "vendor_outage"}
    assert all(sample["inject_count"] > 0 for sample in data)


def test_load_sample_scenario_is_idempotent(
    client: TestClient, facilitator_token: str, session: Session
):
    url = "/api/settings/samples/scenarios/ransomware_response/load"
    first = client.post(url, headers=_headers(facilitator_token))
    second = client.post(url, headers=_headers(facilitator_token))
    assert first.status_code == 201
    assert first.json()["created"] is True
    assert second.status_code == 201
    assert second.json()["created"] is False

    scenarios = session.exec(select(Scenario)).all()
    assert len(scenarios) == 1
    assert scenarios[0].title == "Ransomware Response Quick Start"


def test_actual_facilitator_can_load_samples_while_previewing_participant(
    client: TestClient, facilitator_token: str
):
    client.cookies.set("dt_view_role", "participant")
    resp = client.post(
        "/api/settings/samples/scenarios/vendor_outage/load",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 201
    assert resp.json()["scenario"]["title"] == "Critical Vendor Outage"


def test_create_demo_exercise_enrolls_facilitator_and_releases_start(
    client: TestClient,
    facilitator_token: str,
    facilitator,
    session: Session,
):
    resp = client.post(
        "/api/settings/samples/scenarios/ransomware_response/demo-exercise",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 201
    exercise = resp.json()["exercise"]
    assert exercise["state"] == ExerciseState.active

    member = session.exec(
        select(ExerciseMember)
        .where(ExerciseMember.exercise_id == exercise["id"])
        .where(ExerciseMember.user_id == facilitator.id)
    ).first()
    assert member is not None

    released = session.exec(
        select(Inject)
        .where(Inject.exercise_id == exercise["id"])
        .where(Inject.state == InjectState.released)
    ).all()
    assert [inject.scenario_node_id for inject in released] == ["initial_alert"]

    stored = session.get(Exercise, exercise["id"])
    assert stored is not None
    assert stored.current_node_id == "initial_alert"
