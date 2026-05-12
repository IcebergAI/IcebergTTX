from fastapi.testclient import TestClient

from app.models.exercise import Exercise


def test_participant_cannot_load_facilitator_console(
    client: TestClient, participant_token: str, active_exercise: Exercise
):
    client.cookies.set("access_token", participant_token)
    r = client.get(
        f"/exercises/{active_exercise.id}/facilitate",
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


def test_participant_cannot_load_scenario_editor(client: TestClient, participant_token: str):
    client.cookies.set("access_token", participant_token)
    r = client.get(
        "/scenarios/new",
        follow_redirects=False,
    )
    assert r.status_code == 307
    assert r.headers["location"] == "/dashboard"


def test_authenticated_user_can_load_settings(client: TestClient, participant_token: str):
    client.cookies.set("access_token", participant_token)
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text


def test_facilitator_preview_participant_can_load_scenarios_for_testing(
    client: TestClient, facilitator_token: str
):
    client.cookies.set("access_token", facilitator_token)
    client.cookies.set("dt_view_role", "participant")
    r = client.get("/scenarios", follow_redirects=False)
    assert r.status_code == 200
    assert "Scenarios" in r.text


def test_facilitator_preview_participant_can_still_load_settings(
    client: TestClient, facilitator_token: str
):
    client.cookies.set("access_token", facilitator_token)
    client.cookies.set("dt_view_role", "participant")
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Role preview" in r.text
