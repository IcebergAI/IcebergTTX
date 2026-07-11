import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.models.exercise import Exercise
from app.models.scenario import Scenario
from app.schemas.scenario_json import ScenarioDefinition


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _minimal_definition(**overrides):
    definition = {
        "title": "Validation test",
        "participant_teams": [{"id": "ops", "label": "Ops"}],
        "injects": [{"id": "start", "title": "Start", "content": "x"}],
        "start_inject_id": "start",
    }
    definition.update(overrides)
    return definition


def test_definition_rejects_duplicate_teams_and_unknown_audiences():
    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(
            _minimal_definition(
                participant_teams=[
                    {"id": "ops", "label": "Ops"},
                    {"id": "ops", "label": "Duplicate"},
                ]
            )
        )
    with pytest.raises(ValidationError):
        ScenarioDefinition.model_validate(
            _minimal_definition(
                injects=[
                    {
                        "id": "start",
                        "title": "Start",
                        "content": "x",
                        "target_teams": ["other"],
                    }
                ]
            )
        )


@pytest.mark.parametrize("delay", [-1, 86_401])
def test_definition_rejects_invalid_trigger_delay(delay: int):
    with pytest.raises(ValidationError, match="delay_after_release_seconds"):
        ScenarioDefinition.model_validate(
            _minimal_definition(
                injects=[
                    {
                        "id": "start",
                        "title": "Start",
                        "content": "x",
                        "triggers_communications": [
                            {
                                "external_entity": "Regulator",
                                "direction": "outbound",
                                "subject": "Notice",
                                "body": "Details",
                                "delay_after_release_seconds": delay,
                            }
                        ],
                    }
                ]
            )
        )


def test_definition_rejects_duplicate_options():
    with pytest.raises(ValidationError, match="option ids must be unique"):
        ScenarioDefinition.model_validate(
            _minimal_definition(
                injects=[
                    {
                        "id": "start",
                        "title": "Start",
                        "content": "x",
                        "options": [
                            {"id": "decide", "label": "First"},
                            {"id": " decide ", "label": "Duplicate after normalization"},
                        ],
                    }
                ]
            )
        )


def test_definition_normalizes_graph_reference_ids():
    definition = ScenarioDefinition.model_validate(
        _minimal_definition(
            start_inject_id=" start ",
            injects=[
                {
                    "id": " start ",
                    "title": "Start",
                    "content": "x",
                    "next_inject_id": " next ",
                    "options": [
                        {"id": "branch", "label": "Branch", "next_inject_id": " next "}
                    ],
                },
                {"id": " next ", "title": "Next", "content": "y"},
            ],
        )
    )

    assert definition.start_inject_id == "start"
    assert definition.injects[0].next_inject_id == "next"
    assert definition.injects[0].options[0].next_inject_id == "next"


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


async def test_update_in_use_scenario_creates_revision(
    client: AsyncClient,
    second_facilitator_token: str,
    second_facilitator,
    sample_scenario: Scenario,
    sample_definition: ScenarioDefinition,
    draft_exercise: Exercise,
):
    updated = sample_definition.model_copy(update={"title": "Revision"})
    resp = await client.put(
        f"/api/scenarios/{sample_scenario.id}",
        json=updated.model_dump(),
        headers=_headers(second_facilitator_token),
    )
    assert resp.status_code == 200
    assert resp.json()["id"] != sample_scenario.id
    assert resp.json()["created_by"] == second_facilitator.id

    original = await client.get(
        f"/api/scenarios/{sample_scenario.id}", headers=_headers(second_facilitator_token)
    )
    assert original.json()["title"] == sample_definition.title


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
