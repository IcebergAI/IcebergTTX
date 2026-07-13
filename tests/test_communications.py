import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

from httpx import AsyncClient
from httpx_ws import aconnect_ws
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.communication import CommDirection, Communication, CommunicationRead
from app.models.exercise import Exercise
from app.models.scenario import Scenario
from app.models.user import User, UserRole
from app.schemas.scenario_json import InjectNode, ScenarioDefinition
from app.services.auth_service import create_access_token, hash_password
from app.services.communication_service import (
    mark_read,
    sender_team_for_comm,
    sender_teams_for_comms,
)
from app.services.exercise_service import enrol_member

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _send(
    client: AsyncClient,
    token: str,
    exercise_id: int,
    direction: str = "outbound",
    subject: str = "Test",
    body: str = "Body text",
    external_entity: str | None = None,
    visible_to_teams: list[str] | None = None,
):
    payload = {"direction": direction, "subject": subject, "body": body}
    if external_entity:
        payload["external_entity"] = external_entity
    if visible_to_teams:
        payload["visible_to_teams"] = visible_to_teams
    return await client.post(
        f"/api/exercises/{exercise_id}/communications",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


async def _inject_comm(
    client: AsyncClient,
    token: str,
    exercise_id: int,
    external_entity: str = "ICO",
    subject: str = "Advisory",
    body: str = "Please note…",
    visible_to_teams: list[str] | None = None,
):
    payload = {"external_entity": external_entity, "subject": subject, "body": body}
    if visible_to_teams:
        payload["visible_to_teams"] = visible_to_teams
    return await client.post(
        f"/api/exercises/{exercise_id}/communications/inject",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_node_triggered_comm_is_idempotent_across_group_injects(monkeypatch):
    """Two physical group copies create/broadcast one logical node trigger (#140)."""
    from app.models.inject import Inject
    from app.services import communication_service

    suffix = uuid4().hex
    user_id = scenario_id = exercise_id = first_inject_id = second_inject_id = None
    broadcast = AsyncMock()
    monkeypatch.setattr(communication_service, "broadcast_communication", broadcast)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as seed:
            owner = User(
                email=f"trigger-owner-{suffix}@example.test",
                display_name="Trigger Owner",
                hashed_password="unused",
                role=UserRole.facilitator,
            )
            seed.add(owner)
            await seed.commit()
            user_id = owner.id
            assert user_id is not None
            scenario = Scenario(
                title="Logical trigger",
                definition=ScenarioDefinition(
                    title="Logical trigger",
                    participant_teams=[
                        {"id": "it_ops", "label": "IT Ops"},
                        {"id": "legal", "label": "Legal"},
                    ],
                    injects=[InjectNode(id="node", title="Node", content="x")],
                    start_inject_id="node",
                ).model_dump_json(),
                created_by=user_id,
            )
            seed.add(scenario)
            await seed.commit()
            scenario_id = scenario.id
            assert scenario_id is not None
            exercise = Exercise(
                scenario_id=scenario_id,
                title="Logical trigger",
                created_by=user_id,
            )
            seed.add(exercise)
            await seed.commit()
            exercise_id = exercise.id
            assert exercise_id is not None
            injects = [
                Inject(
                    exercise_id=exercise_id,
                    scenario_node_id="node",
                    title="Node",
                    content="x",
                    group_id=group_id,
                    target_teams=[group_id],
                )
                for group_id in ("it_ops", "legal")
            ]
            seed.add_all(injects)
            await seed.commit()
            first_inject_id, second_inject_id = injects[0].id, injects[1].id
            assert first_inject_id is not None and second_inject_id is not None

        await asyncio.gather(
            communication_service._delayed_comm(
                exercise_id=exercise_id,
                inject_id=first_inject_id,
                direction="inbound",
                external_entity="NCSC",
                subject="One logical trigger",
                body="Body",
                delay=0,
                trigger_key="node:0",
            ),
            communication_service._delayed_comm(
                exercise_id=exercise_id,
                inject_id=second_inject_id,
                direction="inbound",
                external_entity="NCSC",
                subject="One logical trigger",
                body="Body",
                delay=0,
                trigger_key="node:0",
            ),
        )
        async with AsyncSession(engine, expire_on_commit=False) as verify:
            comms = (
                await verify.exec(
                    select(Communication).where(Communication.exercise_id == exercise_id)
                )
            ).all()
            assert len(comms) == 1
            assert comms[0].trigger_key == "node:0"
            assert comms[0].visible_to_teams is None
        broadcast.assert_awaited_once()
    finally:
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            if exercise_id is not None:
                exercise = await cleanup.get(Exercise, exercise_id)
                if exercise is not None:
                    await cleanup.delete(exercise)
                    await cleanup.commit()
            if scenario_id is not None:
                scenario = await cleanup.get(Scenario, scenario_id)
                if scenario is not None:
                    await cleanup.delete(scenario)
                    await cleanup.commit()
            if user_id is not None:
                owner = await cleanup.get(User, user_id)
                if owner is not None:
                    await cleanup.delete(owner)
                    await cleanup.commit()


# ── Send ──────────────────────────────────────────────────────────────────────

async def test_send_outbound(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = (await _send(client, participant_token, active_exercise.id))
    assert r.status_code == 201
    data = r.json()
    assert data["direction"] == "outbound"
    assert data["subject"] == "Test"
    assert data["sender_team"] == "it_ops"
    assert data["is_read"] is False
    assert data["read_at"] is None
    assert "read_by" not in data


async def test_facilitator_injected_comm_rejects_unknown_or_duplicate_audiences(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    for teams in (["unknown"], ["it_ops", "it_ops"]):
        response = await _inject_comm(
            client, facilitator_token, active_exercise.id, visible_to_teams=teams
        )
        assert response.status_code == 422


async def test_participant_send_blocked_when_not_active(
    client: AsyncClient,
    session: AsyncSession,
    participant_token: str,
    active_exercise: Exercise,
):
    """Participant outbound comms require an active exercise, like responses (#40)."""
    from app.models.exercise import ExerciseState
    from app.services.exercise_service import transition_state

    await transition_state(session, active_exercise, ExerciseState.paused)

    r = await _send(client, participant_token, active_exercise.id)
    assert r.status_code == 409


async def test_facilitator_inject_comm_allowed_in_draft(
    client: AsyncClient, facilitator_token: str, draft_exercise: Exercise
):
    """Facilitators may seed simulated inbound comms before the exercise starts (#40)."""
    r = await _inject_comm(client, facilitator_token, draft_exercise.id)
    assert r.status_code == 201
    assert r.json()["direction"] == "inbound"


async def test_inject_inbound_facilitator(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    r = (await _inject_comm(client, facilitator_token, active_exercise.id))
    assert r.status_code == 201
    data = r.json()
    assert data["direction"] == "inbound"
    assert data["external_entity"] == "ICO"
    assert data["visible_to_teams"] == ["it_ops", "legal"]


async def test_inject_inbound_participant_forbidden(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = (await _inject_comm(client, participant_token, active_exercise.id))
    assert r.status_code == 403


# ── List ──────────────────────────────────────────────────────────────────────

async def test_list_comms_all_visible(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    (await _send(client, participant_token, active_exercise.id, subject="Message A"))
    (await _inject_comm(client, facilitator_token, active_exercise.id, subject="ICO Advisory"))

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "Message A" in subjects
    assert "ICO Advisory" in subjects


async def test_visibility_filtering(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """Comm visible only to 'legal' should NOT appear for it_ops participant."""
    (await _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="Legal Only", visible_to_teams=["legal"]
    ))
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {participant_token}"},  # participant is it_ops
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "Legal Only" not in subjects


async def test_visibility_own_team(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """Comm targeted to it_ops is visible to the it_ops participant."""
    (await _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="IT Ops Only", visible_to_teams=["it_ops"]
    ))
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "IT Ops Only" in subjects


async def test_participant_does_not_see_other_participant_outbound(
    client: AsyncClient,
    session: AsyncSession,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    legal = User(
        email="legal-participant@example.com",
        display_name="Legal Participant",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="legal",
    )
    session.add(legal)
    await session.commit()
    await session.refresh(legal)
    await enrol_member(session, exercise=active_exercise, user_id=legal.id, group_id="legal")
    legal_token = create_access_token(subject=legal.email, role=legal.role.value)

    (await _send(client, participant_token, active_exercise.id, subject="IT Ops outbound"))
    legal_r = (await _send(client, legal_token, active_exercise.id, subject="Legal outbound"))
    assert legal_r.status_code == 201
    assert legal_r.json()["sender_team"] == "legal"

    participant_r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert participant_r.status_code == 200
    participant_subjects = [c["subject"] for c in participant_r.json()]
    assert "IT Ops outbound" in participant_subjects
    assert "Legal outbound" not in participant_subjects

    facilitator_r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert facilitator_r.status_code == 200
    facilitator_comms = facilitator_r.json()
    legal_comm = next(c for c in facilitator_comms if c["subject"] == "Legal outbound")
    assert legal_comm["sender_team"] == "legal"


async def test_participant_can_send_outbound_to_team(
    client: AsyncClient,
    session: AsyncSession,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    legal = User(
        email="legal-recipient@example.com",
        display_name="Legal Recipient",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="legal",
    )
    session.add(legal)
    await session.commit()
    await session.refresh(legal)
    await enrol_member(session, exercise=active_exercise, user_id=legal.id, group_id="legal")
    legal_token = create_access_token(subject=legal.email, role=legal.role.value)

    created = (await _send(
        client,
        participant_token,
        active_exercise.id,
        subject="Legal help needed",
        visible_to_teams=["legal"],
    ))
    assert created.status_code == 201
    payload = created.json()
    assert payload["external_entity"] is None
    assert payload["sender_team"] == "it_ops"
    assert payload["visible_to_teams"] == ["legal"]

    legal_r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {legal_token}"},
    )
    assert legal_r.status_code == 200
    assert "Legal help needed" in [c["subject"] for c in legal_r.json()]

    sender_r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert sender_r.status_code == 200
    assert "Legal help needed" in [c["subject"] for c in sender_r.json()]

    facilitator_r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    facilitator_comm = next(
        c for c in facilitator_r.json() if c["subject"] == "Legal help needed"
    )
    assert facilitator_comm["sender_team"] == "it_ops"
    assert facilitator_comm["visible_to_teams"] == ["legal"]


async def test_participant_send_to_unknown_team_rejected(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = (await _send(
        client,
        participant_token,
        active_exercise.id,
        subject="Unknown team",
        visible_to_teams=["not_a_team"],
    ))
    assert r.status_code == 422


async def test_facilitator_sees_all_regardless_of_visibility(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    (await _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="Secret", visible_to_teams=["legal"]
    ))
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    subjects = [c["subject"] for c in r.json()]
    assert "Secret" in subjects


# ── Mark read ─────────────────────────────────────────────────────────────────

async def test_get_comm_is_side_effect_free(
    client: AsyncClient,
    participant_token: str,
    session: AsyncSession,
    active_exercise: Exercise,
    participant: User,
):
    comm = (await _send(client, participant_token, active_exercise.id)).json()

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications/{comm['id']}",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["is_read"] is False
    assert data["read_at"] is None
    assert await session.get(CommunicationRead, (comm["id"], participant.id)) is None


async def test_put_comm_read_is_idempotent_and_preserves_first_timestamp(
    client: AsyncClient,
    participant_token: str,
    session: AsyncSession,
    active_exercise: Exercise,
    participant: User,
):
    comm = (await _send(client, participant_token, active_exercise.id)).json()
    url = f"/api/exercises/{active_exercise.id}/communications/{comm['id']}/read"
    headers = {"Authorization": f"Bearer {participant_token}"}

    first = await client.put(url, headers=headers)
    second = await client.put(url, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["is_read"] is True
    assert first.json()["read_at"] is not None
    assert second.json()["read_at"] == first.json()["read_at"]
    receipts = (
        await session.exec(
            select(CommunicationRead).where(
                CommunicationRead.communication_id == comm["id"],
                CommunicationRead.user_id == participant.id,
            )
        )
    ).all()
    assert len(receipts) == 1

    listed = await client.get(
        f"/api/exercises/{active_exercise.id}/communications",
        headers=headers,
    )
    listed_comm = next(item for item in listed.json() if item["id"] == comm["id"])
    assert listed_comm["is_read"] is True
    assert listed_comm["read_at"] == first.json()["read_at"]


async def test_read_state_is_private_to_the_current_viewer(
    client: AsyncClient,
    participant_token: str,
    facilitator_token: str,
    active_exercise: Exercise,
):
    comm = (await _send(client, participant_token, active_exercise.id)).json()
    url = f"/api/exercises/{active_exercise.id}/communications/{comm['id']}/read"
    facilitator_headers = {"Authorization": f"Bearer {facilitator_token}"}
    participant_headers = {"Authorization": f"Bearer {participant_token}"}

    assert (await client.put(url, headers=facilitator_headers)).status_code == 200
    facilitator_list = (
        await client.get(
            f"/api/exercises/{active_exercise.id}/communications",
            headers=facilitator_headers,
        )
    ).json()
    participant_list = (
        await client.get(
            f"/api/exercises/{active_exercise.id}/communications",
            headers=participant_headers,
        )
    ).json()

    assert next(item for item in facilitator_list if item["id"] == comm["id"])[
        "is_read"
    ] is True
    participant_comm = next(item for item in participant_list if item["id"] == comm["id"])
    assert participant_comm["is_read"] is False
    assert participant_comm["read_at"] is None


async def test_mark_hidden_comm_read_returns_not_found_without_receipt(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    session: AsyncSession,
    active_exercise: Exercise,
    participant: User,
):
    comm = (
        await _inject_comm(
            client,
            facilitator_token,
            active_exercise.id,
            subject="Legal receipt",
            visible_to_teams=["legal"],
        )
    ).json()
    response = await client.put(
        f"/api/exercises/{active_exercise.id}/communications/{comm['id']}/read",
        headers={"Authorization": f"Bearer {participant_token}"},
    )

    assert response.status_code == 404
    assert await session.get(CommunicationRead, (comm["id"], participant.id)) is None


async def test_concurrent_read_receipts_are_lossless_and_cascade():
    """Separate sessions reproduce the old JSON lost-update race against PostgreSQL."""
    suffix = uuid4().hex
    owner_id = reader_a_id = reader_b_id = scenario_id = exercise_id = comm_id = None
    try:
        async with AsyncSession(engine, expire_on_commit=False) as seed:
            owner = User(
                email=f"receipt-owner-{suffix}@example.test",
                display_name="Receipt Owner",
                hashed_password="unused",
                role=UserRole.facilitator,
            )
            reader_a = User(
                email=f"receipt-a-{suffix}@example.test",
                display_name="Reader A",
                hashed_password="unused",
            )
            reader_b = User(
                email=f"receipt-b-{suffix}@example.test",
                display_name="Reader B",
                hashed_password="unused",
            )
            seed.add_all([owner, reader_a, reader_b])
            await seed.commit()
            for user in (owner, reader_a, reader_b):
                await seed.refresh(user)
            owner_id, reader_a_id, reader_b_id = owner.id, reader_a.id, reader_b.id
            assert owner_id is not None and reader_a_id is not None and reader_b_id is not None

            definition = ScenarioDefinition(
                title="Receipt concurrency",
                start_inject_id="opening",
                injects=[
                    InjectNode(id="opening", title="Opening", content="Opening")
                ],
            )
            scenario = Scenario(
                title=definition.title,
                definition=definition.model_dump_json(),
                created_by=owner_id,
            )
            seed.add(scenario)
            await seed.commit()
            await seed.refresh(scenario)
            scenario_id = scenario.id
            assert scenario_id is not None

            exercise = Exercise(
                scenario_id=scenario_id,
                title="Receipt concurrency",
                created_by=owner_id,
            )
            seed.add(exercise)
            await seed.commit()
            await seed.refresh(exercise)
            exercise_id = exercise.id
            assert exercise_id is not None

            communication = Communication(
                exercise_id=exercise_id,
                direction=CommDirection.inbound,
                external_entity="NCSC",
                subject="Concurrent receipt",
                body="Read concurrently",
            )
            seed.add(communication)
            await seed.commit()
            await seed.refresh(communication)
            comm_id = communication.id
            assert comm_id is not None

        async def record(user_id: int):
            async with AsyncSession(engine, expire_on_commit=False) as concurrent_session:
                return await mark_read(concurrent_session, comm_id, user_id)

        receipt_a, receipt_b, duplicate_a = await asyncio.gather(
            record(reader_a_id),
            record(reader_b_id),
            record(reader_a_id),
        )
        assert receipt_a.read_at == duplicate_a.read_at
        assert receipt_b.user_id == reader_b_id

        async with AsyncSession(engine, expire_on_commit=False) as verify:
            receipts = (
                await verify.exec(
                    select(CommunicationRead).where(
                        CommunicationRead.communication_id == comm_id
                    )
                )
            ).all()
            assert {receipt.user_id for receipt in receipts} == {reader_a_id, reader_b_id}

            reader_a = await verify.get(User, reader_a_id)
            assert reader_a is not None
            await verify.delete(reader_a)
            await verify.commit()
            remaining = (
                await verify.exec(
                    select(CommunicationRead).where(
                        CommunicationRead.communication_id == comm_id
                    )
                )
            ).all()
            assert [receipt.user_id for receipt in remaining] == [reader_b_id]

            exercise = await verify.get(Exercise, exercise_id)
            assert exercise is not None
            await verify.delete(exercise)
            await verify.commit()
            assert (
                await verify.exec(
                    select(CommunicationRead).where(
                        CommunicationRead.communication_id == comm_id
                    )
                )
            ).all() == []
            exercise_id = None
    finally:
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            if exercise_id is not None:
                exercise = await cleanup.get(Exercise, exercise_id)
                if exercise is not None:
                    await cleanup.delete(exercise)
                    await cleanup.commit()
            if scenario_id is not None:
                scenario = await cleanup.get(Scenario, scenario_id)
                if scenario is not None:
                    await cleanup.delete(scenario)
                    await cleanup.commit()
            for user_id in (reader_a_id, reader_b_id, owner_id):
                if user_id is None:
                    continue
                user = await cleanup.get(User, user_id)
                if user is not None:
                    await cleanup.delete(user)
                    await cleanup.commit()


async def test_get_comm_not_found(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications/9999",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


# ── WS broadcast ──────────────────────────────────────────────────────────────

async def test_ws_receives_communication(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    async with aconnect_ws(
        f"/ws/exercises/{active_exercise.id}",
        client,
        headers={"origin": "http://testserver", "cookie": f"access_token={participant_token}"},
    ) as ws:
        (await _inject_comm(client, facilitator_token, active_exercise.id, subject="WS Test"))
        msg = await ws.receive_json()

    assert msg["type"] == "communication_received"
    assert msg["payload"]["subject"] == "WS Test"


async def test_ws_visibility_filtered_broadcast(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """Comm targeted to 'legal' should NOT arrive at the it_ops participant's WS."""
    async with aconnect_ws(
        f"/ws/exercises/{active_exercise.id}",
        client,
        headers={"origin": "http://testserver", "cookie": f"access_token={participant_token}"},
    ) as ws:
        (await _inject_comm(
            client, facilitator_token, active_exercise.id,
            subject="Legal Only WS", visible_to_teams=["legal"]
        ))
        await ws.send_json({"type": "ping"})
        msg = await ws.receive_json()

    # Should receive pong, not the communication
    assert msg["type"] == "pong"


async def test_ws_team_outbound_reaches_recipient_team(
    client: AsyncClient,
    session: AsyncSession,
    participant_token: str,
    active_exercise: Exercise,
):
    legal = User(
        email="legal-ws-recipient@example.com",
        display_name="Legal WS Recipient",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="legal",
    )
    session.add(legal)
    await session.commit()
    await session.refresh(legal)
    await enrol_member(session, exercise=active_exercise, user_id=legal.id, group_id="legal")
    legal_token = create_access_token(subject=legal.email, role=legal.role.value)

    async with aconnect_ws(
        f"/ws/exercises/{active_exercise.id}",
        client,
        headers={"origin": "http://testserver", "cookie": f"access_token={legal_token}"},
    ) as ws:
        (await _send(
            client,
            participant_token,
            active_exercise.id,
            subject="WS legal help",
            visible_to_teams=["legal"],
        ))
        msg = await ws.receive_json()

    assert msg["type"] == "communication_received"
    assert msg["payload"]["subject"] == "WS legal help"
    assert msg["payload"]["visible_to_teams"] == ["legal"]


async def test_unread_count_respects_team_visibility(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """The badge must not count comms the viewer isn't allowed to read."""
    await _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="IT Ops Only", visible_to_teams=["it_ops"],
    )
    await _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="Legal Only", visible_to_teams=["legal"],
    )

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications/unread-count",
        headers={"Authorization": f"Bearer {participant_token}"},  # participant is it_ops
    )
    # 200 (not 422) also proves the literal path is matched ahead of GET /{comm_id}.
    assert r.status_code == 200
    assert r.json() == {"unread": 1}

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications/unread-count",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.json() == {"unread": 2}


async def test_unread_count_drops_once_a_comm_is_read(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    resp = await _inject_comm(
        client, facilitator_token, active_exercise.id,
        subject="Advisory", visible_to_teams=["it_ops"],
    )
    comm_id = resp.json()["id"]
    headers = {"Authorization": f"Bearer {participant_token}"}

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications/unread-count", headers=headers
    )
    assert r.json() == {"unread": 1}

    await client.put(
        f"/api/exercises/{active_exercise.id}/communications/{comm_id}/read", headers=headers
    )

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications/unread-count", headers=headers
    )
    assert r.json() == {"unread": 0}


# ── Batched sender-team resolution (#210) ─────────────────────────────────────

async def _seed_unresolved_comms(
    session: AsyncSession, exercise_id: int, sender_id: int, count: int, prefix: str
) -> None:
    """Add outbound comms with no denormalised sender_team — the N+1's trigger.

    Comms sent through the API carry sender_team already, so they short-circuit the
    lookup. Only rows without it (legacy, or a sender with neither group nor team)
    ever reached the per-row ExerciseMember query.
    """
    for i in range(count):
        session.add(
            Communication(
                exercise_id=exercise_id,
                sender_id=sender_id,
                sender_team=None,
                direction=CommDirection.outbound,
                subject=f"{prefix} {i}",
                body="Body",
            )
        )
    await session.commit()


async def test_listing_comms_does_not_scale_queries_with_inbox_size(
    client: AsyncClient,
    session: AsyncSession,
    participant: User,
    facilitator_token: str,
    active_exercise: Exercise,
    count_statements,
):
    """The inbox must cost a constant number of queries, not two per message (#210)."""
    assert participant.id is not None
    headers = {"Authorization": f"Bearer {facilitator_token}"}
    url = f"/api/exercises/{active_exercise.id}/communications"

    await _seed_unresolved_comms(session, active_exercise.id, participant.id, 3, "small")
    with count_statements() as small:
        r = await client.get(url, headers=headers)
    assert r.status_code == 200
    assert len(r.json()) == 3

    await _seed_unresolved_comms(session, active_exercise.id, participant.id, 15, "large")
    with count_statements() as large:
        r = await client.get(url, headers=headers)
    assert r.status_code == 200
    assert len(r.json()) == 18

    # A fixed threshold would rot as the endpoint grows; the invariant is that the
    # count does not move when the inbox six-times in size.
    assert len(large) == len(small)


async def test_unread_count_does_not_scale_queries_with_inbox_size(
    client: AsyncClient,
    session: AsyncSession,
    participant: User,
    participant_token: str,
    active_exercise: Exercise,
    count_statements,
):
    """#210 called /unread-count "not implicated"; it was, via list_communications."""
    assert participant.id is not None
    headers = {"Authorization": f"Bearer {participant_token}"}
    url = f"/api/exercises/{active_exercise.id}/communications/unread-count"

    await _seed_unresolved_comms(session, active_exercise.id, participant.id, 3, "small")
    with count_statements() as small:
        r = await client.get(url, headers=headers)
    assert r.status_code == 200

    await _seed_unresolved_comms(session, active_exercise.id, participant.id, 15, "large")
    with count_statements() as large:
        r = await client.get(url, headers=headers)
    assert r.status_code == 200

    assert len(large) == len(small)


async def test_sender_teams_for_comms_matches_per_row_resolution(
    session: AsyncSession,
    participant: User,
    active_exercise: Exercise,
):
    """The batch resolver must agree with sender_team_for_comm at every precedence level."""
    # A sender enrolled with an exercise group that differs from their global team,
    # to prove group_id wins over User.team rather than coinciding with it.
    grouped = User(
        email="grouped@example.com",
        display_name="Grouped",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="it_ops",
    )
    # A sender with neither an exercise group nor a global team: resolves to None, and
    # stores None, so the per-row version re-queried this row on every single load.
    teamless = User(
        email="teamless@example.com",
        display_name="Teamless",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team=None,
    )
    session.add(grouped)
    session.add(teamless)
    await session.commit()
    await session.refresh(grouped)
    await session.refresh(teamless)
    await enrol_member(session, exercise=active_exercise, user_id=grouped.id, group_id="legal")
    await enrol_member(session, exercise=active_exercise, user_id=teamless.id, group_id=None)

    comms = [
        # denormalised column wins outright
        Communication(
            exercise_id=active_exercise.id,
            sender_id=participant.id,
            sender_team="exec",
            direction=CommDirection.outbound,
            subject="denormalised",
            body="b",
        ),
        # ExerciseMember.group_id beats the sender's global User.team
        Communication(
            exercise_id=active_exercise.id,
            sender_id=grouped.id,
            direction=CommDirection.outbound,
            subject="group",
            body="b",
        ),
        # no group → fall back to User.team
        Communication(
            exercise_id=active_exercise.id,
            sender_id=participant.id,
            direction=CommDirection.outbound,
            subject="user team",
            body="b",
        ),
        # neither group nor team
        Communication(
            exercise_id=active_exercise.id,
            sender_id=teamless.id,
            direction=CommDirection.outbound,
            subject="teamless",
            body="b",
        ),
        # no sender at all (a facilitator-injected inbound)
        Communication(
            exercise_id=active_exercise.id,
            sender_id=None,
            direction=CommDirection.inbound,
            external_entity="ICO",
            subject="inbound",
            body="b",
        ),
    ]
    for c in comms:
        session.add(c)
    await session.commit()
    for c in comms:
        await session.refresh(c)

    batched = await sender_teams_for_comms(session, comms)

    by_subject = {c.subject: batched[c.id] for c in comms}
    assert by_subject == {
        "denormalised": "exec",
        "group": "legal",
        "user team": "it_ops",
        "teamless": None,
        "inbound": None,
    }

    # And it agrees with the per-row helper it replaces, which still serves single-comm
    # callers (_comm_visible_to_user, send_comm, get_comm).
    for c in comms:
        assert batched[c.id] == await sender_team_for_comm(session, c)
