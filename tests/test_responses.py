from httpx import AsyncClient
from httpx_ws import aconnect_ws
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise
from app.models.user import User
from app.schemas.scenario_json import InjectNode, InjectOption, ScenarioDefinition

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _first_released_inject_id(client: AsyncClient, token: str, exercise_id: int) -> int:
    """Release the first pending inject and return its id."""
    injects = (await client.get(
        f"/api/exercises/{exercise_id}/injects",
        headers={"Authorization": f"Bearer {token}"},
    )).json()
    pending = next(i for i in injects if i["state"] == "pending")
    released = (await client.post(
        f"/api/exercises/{exercise_id}/injects/{pending['id']}/release",
        headers={"Authorization": f"Bearer {token}"},
    )).json()
    return released["id"]


async def _submit(
    client: AsyncClient,
    token: str,
    exercise_id: int,
    inject_id: int,
    content: str = "We isolated the systems.",
    selected_option: str | None = None,
):
    body = {"inject_id": inject_id, "content": content}
    if selected_option:
        body["selected_option"] = selected_option
    return await client.post(
        f"/api/exercises/{exercise_id}/responses",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


# ── Submit ────────────────────────────────────────────────────────────────────

async def test_submit_response(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    r = (await _submit(client, participant_token, active_exercise.id, inject_id))
    assert r.status_code == 201
    data = r.json()
    assert data["inject_id"] == inject_id
    assert data["content"] == "We isolated the systems."
    assert data["selected_option"] is None
    assert data["submitted_at"] is not None


async def test_submit_response_with_option(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    r = (await _submit(
        client, participant_token, active_exercise.id, inject_id, selected_option="opt_a"
    ))
    assert r.status_code == 201
    assert r.json()["selected_option"] == "opt_a"


async def test_submit_response_invalid_inject(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = (await _submit(client, participant_token, active_exercise.id, inject_id=9999))
    assert r.status_code == 404


async def test_submit_response_unreleased_inject_forbidden(
    client: AsyncClient, participant_token: str, facilitator_token: str, active_exercise: Exercise
):
    injects = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    inject_id = next(i["id"] for i in injects if i["scenario_node_id"] == "inject_01")
    r = (await _submit(client, participant_token, active_exercise.id, inject_id))
    assert r.status_code == 404


async def test_submit_response_wrong_team_inject_forbidden(
    client: AsyncClient, participant_token: str, facilitator_token: str, active_exercise: Exercise
):
    injects = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    legal_inject = next(i for i in injects if i["scenario_node_id"] == "inject_02")
    await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{legal_inject['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = (await _submit(client, participant_token, active_exercise.id, legal_inject["id"]))
    assert r.status_code == 404


async def test_submit_response_invalid_option_rejected(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    r = (await _submit(
        client, participant_token, active_exercise.id, inject_id, selected_option="not_real"
    ))
    assert r.status_code == 422


async def test_submit_response_blank_free_text_rejected(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    r = (await _submit(client, participant_token, active_exercise.id, inject_id, content="  "))
    assert r.status_code == 422


async def test_submit_response_duplicate_rejected(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    assert (
        await _submit(client, participant_token, active_exercise.id, inject_id)
    ).status_code == 201
    r = (await _submit(client, participant_token, active_exercise.id, inject_id, "Second response"))
    assert r.status_code == 409


# ── List ──────────────────────────────────────────────────────────────────────

async def test_facilitator_sees_all_responses(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    participant: User,
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    (await _submit(client, participant_token, active_exercise.id, inject_id))

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/responses",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert len(r.json()) >= 1


async def test_participant_sees_only_own_responses(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
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
    await session.commit()
    await session.refresh(other)
    from app.services.exercise_service import enrol_member

    await enrol_member(session, exercise=active_exercise, user_id=other.id)
    other_token = create_access_token(subject=other.email, role=other.role.value)

    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    (await _submit(client, participant_token, active_exercise.id, inject_id, "My response"))
    (await _submit(client, other_token, active_exercise.id, inject_id, "Other response"))

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/responses",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    user_ids = {resp["user_id"] for resp in r.json()}
    from sqlmodel import select

    from app.models.user import User as UserModel
    p = (
        await session.exec(
            select(UserModel).where(UserModel.email == "participant@example.com")
        )
    ).first()
    assert user_ids == {p.id}


# ── Get single ────────────────────────────────────────────────────────────────

async def test_get_response(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, participant_token, active_exercise.id, inject_id)).json()

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/responses/{resp['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == resp["id"]


async def test_participant_cannot_get_other_response(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
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
    await session.commit()
    await session.refresh(other)
    from app.services.exercise_service import enrol_member

    await enrol_member(session, exercise=active_exercise, user_id=other.id)
    other_token = create_access_token(subject=other.email, role=other.role.value)

    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    resp = (await _submit(client, other_token, active_exercise.id, inject_id)).json()

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/responses/{resp['id']}",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


# ── Branch evaluation ─────────────────────────────────────────────────────────

async def test_response_with_valid_option_returns_next_inject(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """opt_a on inject_01 should resolve to inject_02."""
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    # Verify we released inject_01 (the start inject of sample_scenario)
    inj = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects/{inject_id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    assert inj["scenario_node_id"] == "inject_01"

    r = (await _submit(
        client, participant_token, active_exercise.id, inject_id, selected_option="opt_a"
    ))
    assert r.status_code == 201


async def test_response_records_group_and_facilitator_gets_pending_next_inject(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import create_exercise, enrol_member, transition_state
    from app.services.scenario_service import create_scenario

    scenario = await create_scenario(
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
    exercise = await create_exercise(
        session,
        scenario_id=scenario.id,
        title="Branch Group Exercise",
        created_by=facilitator.id,
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id)
    await transition_state(session, exercise, ExerciseState.active)

    injects = (await client.get(
        f"/api/exercises/{exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    first = next(i for i in injects if i["scenario_node_id"] == "a")
    second = next(i for i in injects if i["scenario_node_id"] == "b")
    await client.post(
        f"/api/exercises/{exercise.id}/injects/{first['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )

    r = (await _submit(client, participant_token, exercise.id, first["id"], selected_option="go"))
    assert r.status_code == 201
    assert r.json()["group_id"] == "it_ops"

    rows = (await client.get(
        f"/api/exercises/{exercise.id}/responses",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    assert rows[0]["group_id"] == "it_ops"
    assert rows[0]["next_injects"] == [
        {
            "id": second["id"],
            "scenario_node_id": "b",
            "title": "Next",
            "group_id": "it_ops",
        }
    ]


async def test_free_text_linear_response_suggests_next_inject(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import create_exercise, enrol_member, transition_state
    from app.services.scenario_service import create_scenario

    scenario = await create_scenario(
        session,
        definition=ScenarioDefinition(
            title="Linear Free Text",
            participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
            injects=[
                InjectNode(
                    id="a",
                    title="Briefing",
                    content="Explain the plan.",
                    target_teams=["it_ops"],
                    next_inject_id="b",
                    options=[],
                    free_text_response=True,
                ),
                InjectNode(
                    id="b",
                    title="Follow-up",
                    content="Continue.",
                    target_teams=["it_ops"],
                ),
            ],
            start_inject_id="a",
        ),
        created_by=facilitator.id,
    )
    exercise = await create_exercise(
        session,
        scenario_id=scenario.id,
        title="Linear Exercise",
        created_by=facilitator.id,
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id)
    await transition_state(session, exercise, ExerciseState.active)

    injects = (await client.get(
        f"/api/exercises/{exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    first = next(i for i in injects if i["scenario_node_id"] == "a")
    second = next(i for i in injects if i["scenario_node_id"] == "b")
    assert first["options"] == []
    assert first["next_inject_id"] == "b"
    assert first["free_text_response"] is True

    await client.post(
        f"/api/exercises/{exercise.id}/injects/{first['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = await _submit(
        client, participant_token, exercise.id, first["id"], content="We will proceed."
    )
    assert r.status_code == 201

    rows = (await client.get(
        f"/api/exercises/{exercise.id}/responses",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    assert rows[0]["next_injects"] == [
        {
            "id": second["id"],
            "scenario_node_id": "b",
            "title": "Follow-up",
            "group_id": "it_ops",
        }
    ]


async def test_facilitator_preview_participant_response_records_preview_team(
    client: AsyncClient,
    facilitator_token: str,
    active_exercise: Exercise,
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))
    client.cookies.set("dt_view_role", "participant")
    client.cookies.set("dt_view_team", "it_ops")

    r = (await _submit(client, facilitator_token, active_exercise.id, inject_id))

    assert r.status_code == 201
    assert r.json()["group_id"] == "it_ops"


async def test_ws_broadcasts_response_to_facilitator(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    inject_id = (await _first_released_inject_id(client, facilitator_token, active_exercise.id))

    async with aconnect_ws(
        f"/ws/exercises/{active_exercise.id}?token={facilitator_token}"
    , client) as ws:
        (await _submit(client, participant_token, active_exercise.id, inject_id))
        msg = await ws.receive_json()

    assert msg["type"] == "response_submitted"
    assert msg["payload"]["response"]["inject_id"] == inject_id
