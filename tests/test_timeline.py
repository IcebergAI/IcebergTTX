"""Tests for the exercise timeline / replay feed (#111)."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.assessment import ResponseAssessment
from app.models.communication import CommDirection, Communication
from app.models.inject import Inject, InjectState
from app.models.inject_comment import InjectComment
from app.models.response import Response

BASE = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_events(session: AsyncSession, exercise, participant) -> None:
    """A released inject, a response (+assessment), a communication, and a comment,
    each at a distinct, deliberately out-of-order insertion time."""
    inject = Inject(
        exercise_id=exercise.id,
        scenario_node_id="inject_01",
        title="Initial Alert",
        content="Systems compromised.",
        target_teams=["it_ops"],
        state=InjectState.released,
        released_at=BASE + timedelta(minutes=1),
        released_by=exercise.created_by,
    )
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    response = Response(
        inject_id=inject.id,
        exercise_id=exercise.id,
        user_id=participant.id,
        group_id="it_ops",
        content="We isolate the affected hosts.",
        selected_option="opt_a",
        submitted_at=BASE + timedelta(minutes=3),
    )
    session.add(response)
    await session.commit()
    await session.refresh(response)

    assessment = ResponseAssessment(
        response_id=response.id,
        llm_model="test:model",
        assessment_text="Sound containment.",
        decision_quality="good",
    )
    session.add(assessment)
    await session.commit()
    await session.refresh(assessment)
    response.assessment_id = assessment.id
    session.add(response)

    comm = Communication(
        exercise_id=exercise.id,
        direction=CommDirection.outbound,
        external_entity="ICO",
        subject="Breach notification",
        body="Notifying the regulator.",
        visible_to_teams=["legal"],
        sent_at=BASE + timedelta(minutes=5),
    )
    session.add(comm)

    comment = InjectComment(
        inject_id=inject.id,
        exercise_id=exercise.id,
        user_id=exercise.created_by,
        content="Good call by IT Ops.",
        created_at=BASE + timedelta(minutes=4),
    )
    session.add(comment)
    await session.commit()


async def test_timeline_ordered_and_typed(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise, participant
):
    await _seed_events(session, active_exercise, participant)

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/timeline", headers=_bearer(facilitator_token)
    )
    assert r.status_code == 200
    events = r.json()

    # All five kinds present (state_change comes from the active_exercise started_at).
    kinds = {e["kind"] for e in events}
    assert {"inject_released", "response", "communication", "comment", "state_change"} <= kinds

    # Strictly non-decreasing by timestamp.
    times = [e["at"] for e in events]
    assert times == sorted(times)


async def test_timeline_inject_visibility_metadata(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise, participant
):
    await _seed_events(session, active_exercise, participant)
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/timeline", headers=_bearer(facilitator_token)
    )
    inj = next(e for e in r.json() if e["kind"] == "inject_released")
    assert inj["target_teams"] == ["it_ops"]
    assert inj["released_by"] == active_exercise.created_by
    assert inj["scenario_node_id"] == "inject_01"

    comm = next(e for e in r.json() if e["kind"] == "communication")
    assert comm["visible_to_teams"] == ["legal"]
    assert comm["external_entity"] == "ICO"


async def test_timeline_response_carries_decision_quality(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise, participant
):
    await _seed_events(session, active_exercise, participant)
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/timeline", headers=_bearer(facilitator_token)
    )
    resp = next(e for e in r.json() if e["kind"] == "response")
    assert resp["decision_quality"] == "good"
    assert resp["selected_option"] == "opt_a"


async def test_timeline_uses_durable_lifecycle_history_when_audit_persistence_is_off(
    client: AsyncClient,
    facilitator_token: str,
    facilitator,
    active_exercise,
):
    headers = _bearer(facilitator_token)
    pause = await client.post(f"/api/exercises/{active_exercise.id}/pause", headers=headers)
    assert pause.status_code == 200
    resume = await client.post(f"/api/exercises/{active_exercise.id}/resume", headers=headers)
    assert resume.status_code == 200

    r = await client.get(f"/api/exercises/{active_exercise.id}/timeline", headers=headers)
    assert r.status_code == 200
    lifecycle = [event for event in r.json() if event["kind"] == "state_change"]
    assert [event["action"] for event in lifecycle] == [
        "exercise.start",
        "exercise.pause",
        "exercise.resume",
    ]
    assert [(event["previous_state"], event["new_state"]) for event in lifecycle] == [
        ("draft", "active"),
        ("active", "paused"),
        ("paused", "active"),
    ]
    assert all(event["transition_id"] for event in lifecycle)
    assert all(event["actor_id"] == facilitator.id for event in lifecycle)


async def test_timeline_owner_scoping(
    client: AsyncClient,
    facilitator_token: str,
    second_facilitator_token: str,
    admin_token: str,
    participant_token: str,
    session: AsyncSession,
    active_exercise,
    participant,
):
    url = f"/api/exercises/{active_exercise.id}/timeline"

    # Owner and admin succeed.
    assert (await client.get(url, headers=_bearer(facilitator_token))).status_code == 200
    assert (await client.get(url, headers=_bearer(admin_token))).status_code == 200

    # A facilitator who neither created nor co-facilitates is denied.
    assert (await client.get(url, headers=_bearer(second_facilitator_token))).status_code == 403

    # A participant (non-facilitator role) is denied.
    assert (await client.get(url, headers=_bearer(participant_token))).status_code == 403
