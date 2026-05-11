import json

from fastapi.testclient import TestClient

from app.models.scenario import Scenario
from app.schemas.scenario_json import ScenarioDefinition


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── List ──────────────────────────────────────────────────────────────────────

def test_list_scenarios_empty(client: TestClient, facilitator_token: str):
    resp = client.get("/api/scenarios", headers=_headers(facilitator_token))
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_scenarios(client: TestClient, facilitator_token: str, sample_scenario: Scenario):
    resp = client.get("/api/scenarios", headers=_headers(facilitator_token))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == sample_scenario.title


def test_list_requires_auth(client: TestClient):
    resp = client.get("/api/scenarios")
    assert resp.status_code == 401


# ── Create ────────────────────────────────────────────────────────────────────

def test_create_scenario(
    client: TestClient, facilitator_token: str, sample_definition: ScenarioDefinition
):
    resp = client.post(
        "/api/scenarios", json=sample_definition.model_dump(), headers=_headers(facilitator_token)
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == sample_definition.title
    assert "definition" in data
    assert data["definition"]["start_inject_id"] == "inject_01"


def test_create_requires_facilitator(
    client: TestClient, participant_token: str, sample_definition: ScenarioDefinition
):
    resp = client.post(
        "/api/scenarios", json=sample_definition.model_dump(), headers=_headers(participant_token)
    )
    assert resp.status_code == 403


# ── Get ───────────────────────────────────────────────────────────────────────

def test_get_scenario(client: TestClient, facilitator_token: str, sample_scenario: Scenario):
    resp = client.get(f"/api/scenarios/{sample_scenario.id}", headers=_headers(facilitator_token))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sample_scenario.id
    assert "definition" in data


def test_get_scenario_not_found(client: TestClient, facilitator_token: str):
    resp = client.get("/api/scenarios/9999", headers=_headers(facilitator_token))
    assert resp.status_code == 404


# ── Update ────────────────────────────────────────────────────────────────────

def test_update_scenario(
    client: TestClient,
    facilitator_token: str,
    sample_scenario: Scenario,
    sample_definition: ScenarioDefinition,
):
    updated = sample_definition.model_copy(update={"title": "Updated Title"})
    resp = client.put(
        f"/api/scenarios/{sample_scenario.id}",
        json=updated.model_dump(),
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated Title"


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_scenario(client: TestClient, facilitator_token: str, sample_scenario: Scenario):
    resp = client.delete(f"/api/scenarios/{sample_scenario.id}", headers=_headers(facilitator_token))
    assert resp.status_code == 204
    resp2 = client.get(f"/api/scenarios/{sample_scenario.id}", headers=_headers(facilitator_token))
    assert resp2.status_code == 404


# ── Import ────────────────────────────────────────────────────────────────────

def test_import_valid_json(
    client: TestClient, facilitator_token: str, sample_definition: ScenarioDefinition
):
    resp = client.post(
        "/api/scenarios/import",
        json={"definition": sample_definition.model_dump()},
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == sample_definition.title


def test_import_invalid_json_missing_field(client: TestClient, facilitator_token: str):
    resp = client.post(
        "/api/scenarios/import",
        json={"definition": {"title": "Missing injects"}},
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 422


def test_import_invalid_json_bad_reference(client: TestClient, facilitator_token: str):
    bad_def = {
        "title": "Bad ref",
        "participant_teams": [],
        "injects": [
            {
                "id": "inject_01",
                "title": "A",
                "content": "B",
                "options": [{"id": "opt_a", "label": "X", "next_inject_id": "NONEXISTENT"}],
            }
        ],
        "start_inject_id": "inject_01",
    }
    resp = client.post(
        "/api/scenarios/import",
        json={"definition": bad_def},
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 422


# ── Export ────────────────────────────────────────────────────────────────────

def test_export_roundtrip(
    client: TestClient,
    facilitator_token: str,
    sample_scenario: Scenario,
    sample_definition: ScenarioDefinition,
):
    resp = client.get(
        f"/api/scenarios/{sample_scenario.id}/export", headers=_headers(facilitator_token)
    )
    assert resp.status_code == 200
    exported = resp.json()
    assert exported["title"] == sample_definition.title
    assert len(exported["injects"]) == len(sample_definition.injects)
    assert exported["start_inject_id"] == sample_definition.start_inject_id


# ── Validate ──────────────────────────────────────────────────────────────────

def test_validate_valid_scenario(
    client: TestClient, facilitator_token: str, sample_scenario: Scenario
):
    resp = client.get(
        f"/api/scenarios/{sample_scenario.id}/validate", headers=_headers(facilitator_token)
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
