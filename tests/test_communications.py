from httpx import AsyncClient
from httpx_ws import aconnect_ws
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise
from app.models.user import User, UserRole
from app.services.auth_service import create_access_token, hash_password
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
        hashed_password=hash_password("password123"),
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
        hashed_password=hash_password("password123"),
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

async def test_get_comm_marks_read(
    client: AsyncClient, participant_token: str, facilitator_token: str,
    active_exercise: Exercise, participant: User
):
    comm = (await _send(client, participant_token, active_exercise.id)).json()

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/communications/{comm['id']}",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert participant.id in data["read_by"]


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
        f"/ws/exercises/{active_exercise.id}?token={participant_token}"
    , client) as ws:
        (await _inject_comm(client, facilitator_token, active_exercise.id, subject="WS Test"))
        msg = await ws.receive_json()

    assert msg["type"] == "communication_received"
    assert msg["payload"]["subject"] == "WS Test"


async def test_ws_visibility_filtered_broadcast(
    client: AsyncClient, facilitator_token: str, participant_token: str, active_exercise: Exercise
):
    """Comm targeted to 'legal' should NOT arrive at the it_ops participant's WS."""
    async with aconnect_ws(
        f"/ws/exercises/{active_exercise.id}?token={participant_token}"
    , client) as ws:
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
        hashed_password=hash_password("password123"),
        role=UserRole.participant,
        team="legal",
    )
    session.add(legal)
    await session.commit()
    await session.refresh(legal)
    await enrol_member(session, exercise=active_exercise, user_id=legal.id, group_id="legal")
    legal_token = create_access_token(subject=legal.email, role=legal.role.value)

    async with aconnect_ws(
        f"/ws/exercises/{active_exercise.id}?token={legal_token}"
    , client) as ws:
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
