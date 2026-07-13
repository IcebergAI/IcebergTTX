"""Response-field requirements for free-text, option-only, and combined injects."""

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseState
from app.models.user import User
from app.schemas.api import SubmitResponseRequest
from app.schemas.scenario_json import InjectNode, InjectOption, ScenarioDefinition
from app.services.exercise_service import create_exercise, enrol_member, transition_state
from app.services.response_service import response_next_inject_ids, response_validation_error
from app.services.scenario_service import create_scenario

CHOICE_AND_TEXT = InjectNode(
    id="choice-and-text",
    title="Choose",
    content="Choose and explain.",
    options=[InjectOption(id="approve", label="Approve")],
    free_text_response=True,
)
CHOICE_ONLY = InjectNode(
    id="choice-only",
    title="Choose",
    content="Choose one.",
    options=[InjectOption(id="approve", label="Approve")],
    free_text_response=False,
)
LINEAR = InjectNode(
    id="linear",
    title="Explain",
    content="Explain.",
    options=[],
    free_text_response=False,
)


@pytest.mark.parametrize(
    ("node", "content", "selected_option", "expected_error"),
    [
        (CHOICE_AND_TEXT, "Because it contains the incident.", "approve", None),
        (
            CHOICE_AND_TEXT,
            "Because it contains the incident.",
            None,
            "selected_option is required for this inject",
        ),
        (CHOICE_AND_TEXT, " \t ", "approve", "content is required for this inject"),
        (
            CHOICE_AND_TEXT,
            "Reasoning.",
            "unknown",
            "selected_option is not valid for this inject",
        ),
        (CHOICE_ONLY, "", "approve", None),
        (CHOICE_ONLY, "", None, "selected_option is required for this inject"),
        (LINEAR, "We will isolate the host.", None, None),
        (LINEAR, "\n  ", None, "content is required for this inject"),
        (
            LINEAR,
            "We will isolate the host.",
            "approve",
            "selected_option is not valid for this inject",
        ),
        (None, "Manual response.", None, None),
        (None, "  ", None, "content is required for this inject"),
    ],
)
def test_response_validation_matrix(
    node: InjectNode | None,
    content: str,
    selected_option: str | None,
    expected_error: str | None,
):
    assert (
        response_validation_error(node, content=content, selected_option=selected_option)
        == expected_error
    )


def test_submit_request_allows_omitted_content_for_option_only_response():
    request = SubmitResponseRequest.model_validate(
        {"inject_id": 7, "selected_option": "approve"}
    )
    assert request.content == ""


def test_response_next_inject_ids_never_expands_a_missing_branch_choice():
    definition = ScenarioDefinition(
        title="Branches",
        injects=[
            InjectNode(
                id="decision",
                title="Choose",
                content="Choose.",
                options=[
                    InjectOption(id="left", label="Left", next_inject_id="left-next"),
                    InjectOption(id="right", label="Right", next_inject_id="right-next"),
                ],
            ),
            InjectNode(id="left-next", title="Left", content="Left branch."),
            InjectNode(id="right-next", title="Right", content="Right branch."),
        ],
        start_inject_id="decision",
    )

    assert response_next_inject_ids(definition, "decision", None) == []
    assert response_next_inject_ids(definition, "decision", "left") == ["left-next"]
    assert response_next_inject_ids(definition, "decision", "right") == ["right-next"]


async def _release_start(
    client: AsyncClient,
    facilitator_token: str,
    exercise_id: int,
) -> dict:
    headers = {"Authorization": f"Bearer {facilitator_token}"}
    injects = (await client.get(f"/api/exercises/{exercise_id}/injects", headers=headers)).json()
    start = next(inject for inject in injects if inject["state"] == "pending")
    response = await client.post(
        f"/api/exercises/{exercise_id}/injects/{start['id']}/release",
        headers=headers,
    )
    assert response.status_code == 200
    return response.json()


async def test_direct_api_requires_option_for_option_bearing_inject(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    inject = await _release_start(client, facilitator_token, active_exercise.id)
    response = await client.post(
        f"/api/exercises/{active_exercise.id}/responses",
        json={"inject_id": inject["id"], "content": "We will isolate."},
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "selected_option is required for this inject"


async def test_option_only_response_uses_exact_node_option_and_renders_without_content(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    scenario = await create_scenario(
        session,
        definition=ScenarioDefinition(
            title="Option-only branch",
            participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
            injects=[
                InjectNode(
                    id="decision",
                    title="Choose",
                    content="Choose the response.",
                    target_teams=["it_ops"],
                    options=[InjectOption(id="ack", label="Acknowledge", next_inject_id="next")],
                    free_text_response=False,
                ),
                InjectNode(
                    id="next",
                    title="Follow-up",
                    content="Continue.",
                    target_teams=["it_ops"],
                    options=[InjectOption(id="foreign", label="Foreign option")],
                    free_text_response=False,
                ),
            ],
            start_inject_id="decision",
        ),
        created_by=facilitator.id,
    )
    exercise = await create_exercise(
        session,
        scenario_id=scenario.id,
        title="Option-only Exercise",
        created_by=facilitator.id,
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id)
    await transition_state(session, exercise, ExerciseState.active)
    first = await _release_start(client, facilitator_token, exercise.id)
    participant_headers = {"Authorization": f"Bearer {participant_token}"}

    foreign = await client.post(
        f"/api/exercises/{exercise.id}/responses",
        json={"inject_id": first["id"], "selected_option": "foreign"},
        headers=participant_headers,
    )
    assert foreign.status_code == 422
    assert foreign.json()["detail"] == "selected_option is not valid for this inject"

    submitted = await client.post(
        f"/api/exercises/{exercise.id}/responses",
        json={"inject_id": first["id"], "selected_option": "ack"},
        headers=participant_headers,
    )
    assert submitted.status_code == 201
    assert submitted.json()["content"] == ""
    assert submitted.json()["selected_option"] == "ack"

    facilitator_headers = {"Authorization": f"Bearer {facilitator_token}"}
    facilitator_rows = (
        await client.get(
            f"/api/exercises/{exercise.id}/responses",
            headers=facilitator_headers,
        )
    ).json()
    assert facilitator_rows[0]["content"] == ""
    assert facilitator_rows[0]["selected_option"] == "ack"
    assert [item["scenario_node_id"] for item in facilitator_rows[0]["next_injects"]] == [
        "next"
    ]

    report = await client.get(
        f"/api/exercises/{exercise.id}/report",
        headers=facilitator_headers,
    )
    assert report.status_code == 200
    report_response = report.json()["injects"][0]["responses"][0]
    assert report_response["content"] == ""
    assert report_response["selected_option"] == "ack"
