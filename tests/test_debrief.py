"""Tests for facilitator debrief notes (#112)."""

from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.schemas.scenario_json import InjectNode, ScenarioDefinition


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_debrief_write_read_round_trip(
    client: AsyncClient, facilitator_token: str, draft_exercise
):
    # Owner writes notes via the exercise update route.
    r = await client.put(
        f"/api/exercises/{draft_exercise.id}",
        json={"debrief_notes": "Slow to declare an incident; good containment."},
        headers=_bearer(facilitator_token),
    )
    assert r.status_code == 200

    # Owner reads them back from the debrief endpoint.
    r = await client.get(
        f"/api/exercises/{draft_exercise.id}/debrief", headers=_bearer(facilitator_token)
    )
    assert r.status_code == 200
    data = r.json()
    assert data["debrief_notes"] == "Slow to declare an incident; good containment."
    assert data["exercise_id"] == draft_exercise.id


async def test_debrief_surfaces_scenario_notes(
    client: AsyncClient, facilitator: User, facilitator_token: str, session: AsyncSession
):
    from app.services.exercise_service import create_exercise
    from app.services.scenario_service import create_scenario

    definition = ScenarioDefinition(
        title="With Debrief",
        injects=[InjectNode(id="n1", title="A", content="c", options=[])],
        start_inject_id="n1",
        debrief_notes="Focus on notification thresholds.",
    )
    scenario = await create_scenario(session, definition=definition, created_by=facilitator.id)
    ex = await create_exercise(
        session, scenario_id=scenario.id, title="Ex", created_by=facilitator.id
    )

    r = await client.get(f"/api/exercises/{ex.id}/debrief", headers=_bearer(facilitator_token))
    assert r.status_code == 200
    assert r.json()["scenario_debrief_notes"] == "Focus on notification thresholds."


async def test_debrief_owner_scoping(
    client: AsyncClient,
    second_facilitator_token: str,
    participant_token: str,
    draft_exercise,
):
    url = f"/api/exercises/{draft_exercise.id}/debrief"
    # A facilitator who neither created nor co-facilitates cannot read.
    assert (await client.get(url, headers=_bearer(second_facilitator_token))).status_code == 403
    # A participant (non-facilitator) cannot read.
    assert (await client.get(url, headers=_bearer(participant_token))).status_code == 403
    # Nor can a non-owner write via the update route.
    w = await client.put(
        f"/api/exercises/{draft_exercise.id}",
        json={"debrief_notes": "nope"},
        headers=_bearer(second_facilitator_token),
    )
    assert w.status_code == 403


async def test_debrief_included_in_export(
    client: AsyncClient, facilitator: User, facilitator_token: str, session: AsyncSession
):
    from app.services.exercise_service import create_exercise
    from app.services.scenario_service import create_scenario

    definition = ScenarioDefinition(
        title="Export Debrief",
        injects=[InjectNode(id="n1", title="A", content="c", options=[])],
        start_inject_id="n1",
        debrief_notes="Scenario-authored prompt.",
    )
    scenario = await create_scenario(session, definition=definition, created_by=facilitator.id)
    ex = await create_exercise(
        session, scenario_id=scenario.id, title="Ex", created_by=facilitator.id
    )
    await client.put(
        f"/api/exercises/{ex.id}",
        json={"debrief_notes": "Facilitator observation."},
        headers=_bearer(facilitator_token),
    )

    r = await client.get(f"/api/exercises/{ex.id}/export", headers=_bearer(facilitator_token))
    assert r.status_code == 200
    debrief = r.json()["debrief"]
    assert debrief["scenario_debrief_notes"] == "Scenario-authored prompt."
    assert debrief["debrief_notes"] == "Facilitator observation."
