from httpx import AsyncClient
from httpx_ws import aconnect_ws
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseProgress
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
    selected_option: str | None = "opt_a",
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
    assert data["selected_option"] == "opt_a"
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
    r = (await _submit(
        client,
        participant_token,
        active_exercise.id,
        inject_id,
        "Second response",
        selected_option=None,
    ))
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
    assert r.json()["progression"]["cursors"][0]["current_node_id"] == "b"

    participant_progression = await client.get(
        f"/api/exercises/{exercise.id}/progression",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert participant_progression.status_code == 200
    assert [
        cursor["group_id"] for cursor in participant_progression.json()["cursors"]
    ] == ["it_ops"]

    facilitator_progression = await client.get(
        f"/api/exercises/{exercise.id}/progression",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert facilitator_progression.status_code == 200
    assert {cursor["group_id"] for cursor in facilitator_progression.json()["cursors"]} == {
        None,
        "it_ops",
    }

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

    from sqlmodel import select

    from app.models.exercise import ExerciseProgress
    from app.models.inject import InjectProgress, InjectState

    inject_progress = (
        await session.exec(
            select(InjectProgress).where(
                InjectProgress.inject_id == first["id"],
                InjectProgress.group_id == "it_ops",
            )
        )
    ).one()
    assert inject_progress.state == InjectState.resolved
    assert inject_progress.resolved_by == participant.id
    assert inject_progress.resolution_reason == "participant_response"

    cursor = (
        await session.exec(
            select(ExerciseProgress).where(
                ExerciseProgress.exercise_id == exercise.id,
                ExerciseProgress.group_id == "it_ops",
            )
        )
    ).one()
    assert cursor.current_node_id == "b"
    assert cursor.current_inject_id == first["id"]

    refreshed_injects = (await client.get(
        f"/api/exercises/{exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    assert next(i for i in refreshed_injects if i["id"] == first["id"])["state"] == "resolved"

    timeline = await client.get(
        f"/api/exercises/{exercise.id}/timeline",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert timeline.status_code == 200
    resolution_event = next(
        event for event in timeline.json() if event["kind"] == "inject_resolved"
    )
    assert resolution_event["inject_id"] == first["id"]
    assert resolution_event["group_id"] == "it_ops"

    report = await client.get(
        f"/api/exercises/{exercise.id}/report",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert report.status_code == 200
    report_inject = next(
        item for item in report.json()["injects"] if item["scenario_node_id"] == "a"
    )
    assert report_inject["resolutions"][0]["group_id"] == "it_ops"


async def test_selected_group_branch_blocks_mutually_exclusive_release(
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
            title="Exclusive branch",
            participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
            injects=[
                InjectNode(
                    id="a",
                    title="Choose",
                    content="Choose a path.",
                    target_teams=["it_ops"],
                    options=[
                        InjectOption(id="left", label="Left", next_inject_id="b"),
                        InjectOption(id="right", label="Right", next_inject_id="c"),
                    ],
                ),
                InjectNode(id="b", title="Left path", content="Left", target_teams=["it_ops"]),
                InjectNode(id="c", title="Right path", content="Right", target_teams=["it_ops"]),
            ],
            start_inject_id="a",
        ),
        created_by=facilitator.id,
    )
    exercise = await create_exercise(
        session,
        scenario_id=scenario.id,
        title="Exclusive branch",
        created_by=facilitator.id,
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id, group_id="it_ops")
    await transition_state(session, exercise, ExerciseState.active)

    headers = {"Authorization": f"Bearer {facilitator_token}"}
    injects = (await client.get(
        f"/api/exercises/{exercise.id}/injects", headers=headers
    )).json()
    by_node = {inject["scenario_node_id"]: inject for inject in injects}
    assert (await client.post(
        f"/api/exercises/{exercise.id}/injects/{by_node['a']['id']}/release",
        headers=headers,
    )).status_code == 200
    assert (await _submit(
        client,
        participant_token,
        exercise.id,
        by_node["a"]["id"],
        selected_option="left",
    )).status_code == 201
    assert (await client.post(
        f"/api/exercises/{exercise.id}/injects/{by_node['c']['id']}/release",
        headers=headers,
    )).status_code == 409
    assert (await client.post(
        f"/api/exercises/{exercise.id}/injects/{by_node['b']['id']}/release",
        headers=headers,
    )).status_code == 200


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
        client,
        participant_token,
        exercise.id,
        first["id"],
        content="We will proceed.",
        selected_option=None,
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
        f"/ws/exercises/{active_exercise.id}",
        client,
        headers={"origin": "http://testserver", "cookie": f"access_token={facilitator_token}"},
    ) as ws:
        (await _submit(client, participant_token, active_exercise.id, inject_id))
        msg = await ws.receive_json()

    assert msg["type"] == "response_submitted"
    assert msg["payload"]["response"]["inject_id"] == inject_id


async def test_unlinked_inject_is_releasable_only_before_the_first_response(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    """An orphan node has a release window that closes at the FIRST response anywhere.

    release_is_allowed() lets a node nothing links to through while no cursor has
    advanced, but bails once *any* cursor holds a current_inject_id -- and only the
    responding team's cursor ever advances (resolve_response_progression scopes its
    UPDATE to that group). So one team answering shuts the window for every team,
    including teams that have not responded at all.

    Two teams, and only it_ops responds: that is what discriminates the real `any`
    gate from an `all` one, under which legal's untouched cursor would keep the
    orphan releasable. The cookbook's "reachability is not required" recipe depends
    on this, so pin both ends of it.
    """
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import create_exercise, enrol_member, transition_state
    from app.services.scenario_service import create_scenario

    scenario = await create_scenario(
        session,
        definition=ScenarioDefinition(
            title="Orphan window",
            participant_teams=[
                {"id": "it_ops", "label": "IT Ops"},
                # never responds -- its cursor stays un-advanced for the whole test
                {"id": "legal", "label": "Legal"},
            ],
            injects=[
                InjectNode(id="start", title="Start", content="c", target_teams=["it_ops"]),
                # linked to by nothing — the cookbook's `legal_task` shape
                InjectNode(id="orphan", title="Orphan", content="c", target_teams=["it_ops"]),
                InjectNode(id="spare", title="Spare", content="c", target_teams=["it_ops"]),
            ],
            start_inject_id="start",
        ),
        created_by=facilitator.id,
    )
    exercise = await create_exercise(
        session, title="Orphan window", scenario_id=scenario.id, created_by=facilitator.id
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id, group_id="it_ops")
    await transition_state(session, exercise, ExerciseState.active)

    headers = {"Authorization": f"Bearer {facilitator_token}"}
    injects = (
        await client.get(f"/api/exercises/{exercise.id}/injects", headers=headers)
    ).json()
    by_node = {inject["scenario_node_id"]: inject for inject in injects}

    # Before any response: an orphan is releasable.
    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/injects/{by_node['orphan']['id']}/release",
            headers=headers,
        )
    ).status_code == 200

    # Advance a cursor by answering the start node.
    await client.post(
        f"/api/exercises/{exercise.id}/injects/{by_node['start']['id']}/release",
        headers=headers,
    )
    assert (
        await _submit(
            client,
            participant_token,
            exercise.id,
            by_node["start"]["id"],
            selected_option=None,
        )
    ).status_code == 201

    # Only it_ops' cursor advanced. legal never responded, so its cursor still holds no
    # current_inject_id -- an `all` gate would still let the orphan through here.
    cursors = (
        await session.exec(
            select(ExerciseProgress).where(ExerciseProgress.exercise_id == exercise.id)
        )
    ).all()
    advanced = {c.group_id: c.current_inject_id is not None for c in cursors}
    assert advanced["it_ops"] is True
    assert advanced["legal"] is False

    # The window is shut regardless: a second orphan is refused like any off-cursor node.
    r = await client.post(
        f"/api/exercises/{exercise.id}/injects/{by_node['spare']['id']}/release",
        headers=headers,
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "Inject is not the current branch for its group"


async def test_adhoc_inject_response_does_not_move_the_cursor(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    """An inject outside the scenario graph resolves without touching any cursor (#256).

    Ad-hoc injects (and approved LLM suggestions, which share the no-scenario_node_id
    shape) are interruptions, not steps on a path. Before the fix, responding to one
    overwrote the team cursor with current_node_id=None and a non-null
    current_inject_id -- the exact state release_is_allowed reads as "advanced to a
    dead end", refusing the team's real branch forever, manually and on schedule.
    """
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import create_exercise, enrol_member, transition_state
    from app.services.scenario_service import create_scenario

    scenario = await create_scenario(
        session,
        definition=ScenarioDefinition(
            title="Interrupted branch",
            participant_teams=[{"id": "it_ops", "label": "IT Ops"}],
            injects=[
                InjectNode(
                    id="start",
                    title="Start",
                    content="c",
                    target_teams=["it_ops"],
                    options=[InjectOption(id="go", label="Go", next_inject_id="followup")],
                ),
                InjectNode(id="followup", title="Followup", content="c", target_teams=["it_ops"]),
            ],
            start_inject_id="start",
        ),
        created_by=facilitator.id,
    )
    exercise = await create_exercise(
        session, title="Interrupted branch", scenario_id=scenario.id, created_by=facilitator.id
    )
    await enrol_member(session, exercise=exercise, user_id=participant.id, group_id="it_ops")
    await transition_state(session, exercise, ExerciseState.active)

    headers = {"Authorization": f"Bearer {facilitator_token}"}
    injects = (
        await client.get(f"/api/exercises/{exercise.id}/injects", headers=headers)
    ).json()
    by_node = {inject["scenario_node_id"]: inject for inject in injects}

    # Walk the scenario one step: the cursor lands on the followup node.
    await client.post(
        f"/api/exercises/{exercise.id}/injects/{by_node['start']['id']}/release",
        headers=headers,
    )
    assert (
        await _submit(
            client, participant_token, exercise.id, by_node["start"]["id"], selected_option="go"
        )
    ).status_code == 201

    # Interrupt with an ad-hoc inject and let the team answer it.
    adhoc = await client.post(
        f"/api/exercises/{exercise.id}/injects",
        json={"title": "Breaking news", "content": "c", "target_teams": ["it_ops"]},
        headers=headers,
    )
    assert adhoc.status_code == 201
    adhoc_id = adhoc.json()["id"]
    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/injects/{adhoc_id}/release", headers=headers
        )
    ).status_code == 200
    assert (
        await _submit(
            client, participant_token, exercise.id, adhoc_id, selected_option=None
        )
    ).status_code == 201

    # The ad-hoc inject resolved like any other...
    refreshed = (
        await client.get(f"/api/exercises/{exercise.id}/injects", headers=headers)
    ).json()
    assert next(i for i in refreshed if i["id"] == adhoc_id)["state"] == "resolved"

    # ...but the cursor still points where the scenario response left it.
    cursor = (
        await session.exec(
            select(ExerciseProgress).where(
                ExerciseProgress.exercise_id == exercise.id,
                ExerciseProgress.group_id == "it_ops",
            )
        )
    ).one()
    assert cursor.current_node_id == "followup"
    assert cursor.current_inject_id == by_node["start"]["id"]

    # End to end: the branch the participants chose is still releasable.
    r = await client.post(
        f"/api/exercises/{exercise.id}/injects/{by_node['followup']['id']}/release",
        headers=headers,
    )
    assert r.status_code == 200


async def test_out_of_audience_response_does_not_resolve_or_advance(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    session: AsyncSession,
    facilitator: User,
    participant: User,
):
    """A response from a context outside the release audience moves nothing (#256).

    Scenario nodes materialize as group-scoped physical injects, so the only
    fallback-visible shape is an ad-hoc inject with several target teams
    (group_id stays None). The User.team visibility fallback lets a member
    enrolled in one group *see* such an inject targeted at their global team --
    but their response submits under their enrolment group, which the release
    never seeded. Before the fix, resolve_response_progression invented an
    InjectProgress row for that context (corrupting the shared-close audience)
    and advanced that context's cursor.
    """
    from app.models.exercise import ExerciseState
    from app.models.inject import InjectProgress
    from app.models.user import UserRole
    from app.services.auth_service import create_access_token, hash_password
    from app.services.exercise_service import create_exercise, enrol_member, transition_state
    from app.services.scenario_service import create_scenario

    scenario = await create_scenario(
        session,
        definition=ScenarioDefinition(
            title="Audience gate",
            participant_teams=[
                {"id": "it_ops", "label": "IT Ops"},
                {"id": "legal", "label": "Legal"},
                # declared so the ad-hoc inject can target two teams, enrolled by nobody
                {"id": "comms", "label": "Comms"},
            ],
            injects=[InjectNode(id="start", title="Start", content="c", target_teams=["it_ops"])],
            start_inject_id="start",
        ),
        created_by=facilitator.id,
    )
    exercise = await create_exercise(
        session, title="Audience gate", scenario_id=scenario.id, created_by=facilitator.id
    )
    # The fixture participant's global team is it_ops, but they are enrolled into
    # legal -- the fallback makes the it_ops-targeted inject visible to them.
    await enrol_member(session, exercise=exercise, user_id=participant.id, group_id="legal")
    insider = User(
        email="insider@example.com",
        display_name="Insider",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="it_ops",
    )
    session.add(insider)
    await session.commit()
    await session.refresh(insider)
    insider_token = create_access_token(subject=insider.email, role=insider.role.value)
    await enrol_member(session, exercise=exercise, user_id=insider.id, group_id="it_ops")
    await transition_state(session, exercise, ExerciseState.active)

    headers = {"Authorization": f"Bearer {facilitator_token}"}
    # Two target teams keep group_id None; the audience is enrolled contexts only:
    # {legal, it_ops} & {it_ops, comms} == {it_ops}.
    created = await client.post(
        f"/api/exercises/{exercise.id}/injects",
        json={"title": "All hands", "content": "c", "target_teams": ["it_ops", "comms"]},
        headers=headers,
    )
    assert created.status_code == 201
    inject_id = created.json()["id"]
    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/injects/{inject_id}/release", headers=headers
        )
    ).status_code == 200

    # The legal-enrolled member can see and answer it, and the response is recorded...
    r = await _submit(client, participant_token, exercise.id, inject_id, selected_option=None)
    assert r.status_code == 201
    assert r.json()["group_id"] == "legal"

    # ...but it resolves no context the release opened and moves no cursor.
    legal_rows = (
        await session.exec(
            select(InjectProgress).where(
                InjectProgress.inject_id == inject_id,
                InjectProgress.group_id == "legal",
            )
        )
    ).all()
    assert legal_rows == []
    legal_cursor = (
        await session.exec(
            select(ExerciseProgress).where(
                ExerciseProgress.exercise_id == exercise.id,
                ExerciseProgress.group_id == "legal",
            )
        )
    ).one()
    assert legal_cursor.current_node_id == "start"
    assert legal_cursor.current_inject_id is None
    refreshed = (
        await client.get(f"/api/exercises/{exercise.id}/injects", headers=headers)
    ).json()
    assert next(i for i in refreshed if i["id"] == inject_id)["state"] == "released"

    # The seeded audience still owns the inject: the it_ops response resolves it.
    assert (
        await _submit(client, insider_token, exercise.id, inject_id, selected_option=None)
    ).status_code == 201
    refreshed = (
        await client.get(f"/api/exercises/{exercise.id}/injects", headers=headers)
    ).json()
    assert next(i for i in refreshed if i["id"] == inject_id)["state"] == "resolved"
