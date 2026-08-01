import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseMember, ExerciseState
from app.models.inject import Inject, InjectState
from app.models.scenario import Scenario
from app.models.user import UserRole
from app.services.sample_service import get_sample_definition


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def test_list_sample_scenarios_requires_actual_facilitator(
    client: AsyncClient, participant_token: str
):
    resp = await client.get(
        "/api/settings/samples/scenarios",
        headers=_headers(participant_token),
    )
    assert resp.status_code == 403


async def test_list_sample_scenarios(client: AsyncClient, facilitator_token: str):
    resp = await client.get(
        "/api/settings/samples/scenarios",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert {sample["id"] for sample in data} >= {"ransomware_response", "vendor_outage"}
    assert all(sample["inject_count"] > 0 for sample in data)


# ── #15: path traversal in the sample loader ─────────────────────────────────


@pytest.mark.parametrize(
    "sample_id",
    ["../../etc/passwd", "..", "foo/bar", "evil.json", "with space", ""],
)
def test_get_sample_definition_rejects_untrusted_ids(sample_id: str):
    assert get_sample_definition(sample_id) is None


def test_get_sample_definition_loads_legit_id():
    assert get_sample_definition("ransomware_response") is not None


async def test_load_sample_rejects_traversal_id(client: AsyncClient, facilitator_token: str):
    # A single path segment with a disallowed char never resolves to a file → 404.
    resp = await client.post(
        "/api/settings/samples/scenarios/evil.json/load",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 404


async def test_load_sample_scenario_is_idempotent(
    client: AsyncClient, facilitator_token: str, session: AsyncSession
):
    url = "/api/settings/samples/scenarios/ransomware_response/load"
    first = await client.post(url, headers=_headers(facilitator_token))
    second = await client.post(url, headers=_headers(facilitator_token))
    assert first.status_code == 201
    assert first.json()["created"] is True
    assert second.status_code == 201
    assert second.json()["created"] is False

    scenarios = (await session.exec(select(Scenario))).all()
    assert len(scenarios) == 1
    assert scenarios[0].title == "Ransomware Response Quick Start"


async def test_actual_facilitator_can_load_samples_while_previewing_participant(
    client: AsyncClient, facilitator_token: str
):
    client.cookies.set("dt_view_role", "participant")
    resp = await client.post(
        "/api/settings/samples/scenarios/vendor_outage/load",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 201
    assert resp.json()["scenario"]["title"] == "Critical Vendor Outage"


async def test_create_demo_exercise_enrolls_facilitator_and_releases_start(
    client: AsyncClient,
    facilitator_token: str,
    facilitator,
    session: AsyncSession,
):
    resp = await client.post(
        "/api/settings/samples/scenarios/ransomware_response/demo-exercise",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 201
    exercise = resp.json()["exercise"]
    assert exercise["state"] == ExerciseState.active

    member = (await session.exec(
        select(ExerciseMember)
        .where(ExerciseMember.exercise_id == exercise["id"])
        .where(ExerciseMember.user_id == facilitator.id)
    )).first()
    assert member is not None
    assert member.role_at_enrolment == UserRole.participant

    released = (await session.exec(
        select(Inject)
        .where(Inject.exercise_id == exercise["id"])
        .where(Inject.state == InjectState.released)
    )).all()
    assert [inject.scenario_node_id for inject in released] == ["initial_alert"]

    stored = await session.get(Exercise, exercise["id"])
    assert stored is not None
    assert stored.current_node_id == "initial_alert"


async def test_demo_with_multi_team_start_releases_the_enrolled_teams_inject(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    monkeypatch,
):
    """A multi-team start node seeds one physical inject per team; the demo enrols
    only participant_teams[0], so releasing any other team's copy 409s on an empty
    audience and fails the whole demo load (#269). The pick must be the enrolled
    team's inject — and activation must arm the sample's schedules."""
    from app.schemas.scenario_json import ScenarioDefinition
    from app.services import sample_service

    definition = ScenarioDefinition.model_validate(
        {
            "title": "Multi-team Start Demo",
            "description": "Trap case for #269",
            "participant_teams": [
                {"id": "alpha", "label": "Alpha"},
                {"id": "beta", "label": "Beta"},
            ],
            "injects": [
                {
                    "id": "kickoff",
                    "title": "Kickoff",
                    "content": "Both teams get their own copy.",
                    "target_teams": ["beta", "alpha"],
                    "release_at_minutes": 5,
                }
            ],
            "start_inject_id": "kickoff",
        }
    )
    monkeypatch.setattr(sample_service, "get_sample_definition", lambda _id: definition)
    armed: list[int] = []

    async def spy_schedule(session_, exercise) -> None:
        armed.append(exercise.id)

    monkeypatch.setattr(sample_service, "schedule_exercise_injects", spy_schedule)

    resp = await client.post(
        "/api/settings/samples/scenarios/whatever/demo-exercise",
        headers=_headers(facilitator_token),
    )
    assert resp.status_code == 201, resp.text
    exercise = resp.json()["exercise"]

    released = (
        await session.exec(
            select(Inject)
            .where(Inject.exercise_id == exercise["id"])
            .where(Inject.state == InjectState.released)
        )
    ).all()
    # Exactly the enrolled team's copy — not another team's, not both.
    assert [i.group_id for i in released] == ["alpha"]
    assert armed == [exercise["id"]]
