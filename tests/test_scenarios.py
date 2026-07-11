import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.models.scenario import Scenario
from app.schemas.scenario_json import ScenarioDefinition


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_definition_rejects_duplicate_teams_options_and_unknown_audiences():
    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(
            {
                "title": "Invalid",
                "participant_teams": [{"id": "ops", "label": "Ops"}, {"id": "ops", "label": "Dup"}],
                "injects": [{"id": "start", "title": "Start", "content": "x"}],
                "start_inject_id": "start",
            }
        )
    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(
            {
                "title": "Invalid",
                "participant_teams": [{"id": "ops", "label": "Ops"}],
                "injects": [
                    {
                        "id": "start",
                        "title": "Start",
                        "content": "x",
                        "target_teams": ["other"],
                    }
                ],
                "start_inject_id": "start",
            }
        )


# ── List ──────────────────────────────────────────────────────────────────────

async def test_list_scenarios_empty(client: AsyncClient, facilitator_token: str):
    resp = await client.get("/api/scenarios", headers=_headers(facilitator_token))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_scenarios(
    client: AsyncClient, facilitator_token: str, sample_scenario: Scenario
):
    resp = await client.get("/api/scenarios", headers=_headers(facilitator_token))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == sample_scenario.title


async def test_list_requires_auth(client: AsyncClient):
    resp = await client.get("/api/scenarios")
    assert resp.status_code == 401


async def test_list_requires_facilitator(client: AsyncClient, participant_token: str):
    resp = await client.get("/api/scenarios", headers=_headers(participant_token))
    assert resp.status_code == 403


# ── Create ────────────────────────────────────────────────────────────────────

async def test_create_scenario(
    client: AsyncClient, facilitator_token: str, sample_definition: ScenarioDefinition
):
    resp = await client.post(
        "/api/scenarios", json=sample_definition.model_dump(), headers=_headers(facilitator_token)
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == sample_definition.title
    assert "definition" in data
    assert data["definition"]["start_inject_id"] == "inject_01"


async def test_create_requires_facilitator(
    client: AsyncClient, participant_token: str, sample_definition: ScenarioDefinition
):
    resp = await client.post(
        "/api/scenarios", json=sample_definition.model_dump(), headers=_headers(participant_token)
    )
    assert resp.status_code == 403


# ── Get ───────────────────────────────────────────────────────────────────────

async def test_get_scenario(client: AsyncClient, facilitator_token: str, sample_scenario: Scenario):
    resp = await client.get(
        f"/api/scenarios/{sample_scenario.id}", headers=_headers(facilitator_token)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == sample_scenario.id
    assert "definition" in data


async def test_get_scenario_not_found(client: AsyncClient, facilitator_token: str):
    resp = await client.get("/api/scenarios/9999", headers=_headers(facilitator_token))
    assert resp.status_code == 404


async def test_get_scenario_requires_facilitator(
    client: AsyncClient, participant_token: str, sample_scenario: Scenario
):
    resp = await client.get(
        f"/api/scenarios/{sample_scenario.id}", headers=_headers(participant_token)
    )
    assert resp.status_code == 403


# ── Update ────────────────────────────────────────────────────────────────────

async def test_update_scenario(
    client: AsyncClient,
    facilitator_token: str,
    sample_scenario: Scenario,
    sample_definition: ScenarioDefinition,
):
    updated = sample_definition.model_copy(update={"title": "Updated Title"})
    resp = await client.put(
        f"/api/scenarios/{sample_scenario.id}",
        json=updated.model_dump(),
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated Title"


# ── Delete ────────────────────────────────────────────────────────────────────

async def test_delete_scenario(
    client: AsyncClient, facilitator_token: str, sample_scenario: Scenario
):
    resp = await client.delete(
        f"/api/scenarios/{sample_scenario.id}", headers=_headers(facilitator_token)
    )
    assert resp.status_code == 204
    resp2 = await client.get(
        f"/api/scenarios/{sample_scenario.id}", headers=_headers(facilitator_token)
    )
    assert resp2.status_code == 404


# ── Import ────────────────────────────────────────────────────────────────────

async def test_import_valid_json(
    client: AsyncClient, facilitator_token: str, sample_definition: ScenarioDefinition
):
    resp = await client.post(
        "/api/scenarios/import",
        json={"definition": sample_definition.model_dump()},
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == sample_definition.title


async def test_import_invalid_json_missing_field(client: AsyncClient, facilitator_token: str):
    resp = await client.post(
        "/api/scenarios/import",
        json={"definition": {"title": "Missing injects"}},
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 422


async def test_import_invalid_json_bad_reference(client: AsyncClient, facilitator_token: str):
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
    resp = await client.post(
        "/api/scenarios/import",
        json={"definition": bad_def},
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 422


# ── Export ────────────────────────────────────────────────────────────────────

async def test_export_roundtrip(
    client: AsyncClient,
    facilitator_token: str,
    sample_scenario: Scenario,
    sample_definition: ScenarioDefinition,
):
    resp = await client.get(
        f"/api/scenarios/{sample_scenario.id}/export", headers=_headers(facilitator_token)
    )
    assert resp.status_code == 200
    exported = resp.json()
    assert exported["title"] == sample_definition.title
    assert len(exported["injects"]) == len(sample_definition.injects)
    assert exported["start_inject_id"] == sample_definition.start_inject_id


# ── Validate ──────────────────────────────────────────────────────────────────

async def test_validate_valid_scenario(
    client: AsyncClient, facilitator_token: str, sample_scenario: Scenario
):
    resp = await client.get(
        f"/api/scenarios/{sample_scenario.id}/validate", headers=_headers(facilitator_token)
    )
    assert resp.status_code == 200
    assert resp.json()["valid"] is True
