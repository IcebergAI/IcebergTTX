"""Tests for the generated after-action report + executive summary (#113)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.communication import CommDirection, Communication
from app.models.exercise import ExerciseState
from app.models.inject import Inject, InjectState
from app.models.response import Response
from app.models.user import User, UserRole
from app.services.auth_service import hash_password


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _fake_provider(text: str, label: str = "test:model"):
    provider = MagicMock()
    provider.llm_model_label = label
    provider.complete = AsyncMock(return_value=text)
    return provider


async def _seed_completed(session: AsyncSession, exercise, participant) -> None:
    inject = Inject(
        exercise_id=exercise.id,
        scenario_node_id="inject_01",
        title="Multiple Workstations Locked",
        content="Ransom notes on finance machines.",
        target_teams=["it_ops"],
        state=InjectState.released,
        released_at=datetime(2026, 1, 1, 9, 1, tzinfo=UTC),
        released_by=exercise.created_by,
    )
    session.add(inject)
    await session.commit()
    await session.refresh(inject)

    session.add(
        Response(
            inject_id=inject.id,
            exercise_id=exercise.id,
            user_id=participant.id,
            content="We isolate the affected hosts immediately.",
            selected_option="isolate",
            submitted_at=datetime(2026, 1, 1, 9, 3, tzinfo=UTC),
        )
    )
    session.add(
        Communication(
            exercise_id=exercise.id,
            direction=CommDirection.outbound,
            external_entity="ICO",
            subject="Breach notification",
            body="Notifying the regulator of a suspected breach.",
            sent_at=datetime(2026, 1, 1, 9, 5, tzinfo=UTC),
        )
    )
    exercise.debrief_notes = "Containment was fast; the notification decision lagged."
    session.add(exercise)
    await session.commit()


async def test_report_markdown_assembles_sections(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise, participant
):
    await _seed_completed(session, active_exercise, participant)

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/report.md", headers=_bearer(facilitator_token)
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers["content-disposition"]
    md = r.text
    assert "# After-Action Report" in md
    assert "## Decision Summary" in md
    assert "Multiple Workstations Locked" in md
    assert "We isolate the affected hosts immediately." in md
    assert "## Communications Log" in md
    assert "Breach notification" in md
    assert "## Debrief" in md
    assert "notification decision lagged" in md
    # No summary drafted → no executive-summary section.
    assert "## Executive Summary" not in md


async def test_report_empty_sections_collapse(
    client: AsyncClient, facilitator_token: str, draft_exercise
):
    r = await client.get(
        f"/api/exercises/{draft_exercise.id}/report.md", headers=_bearer(facilitator_token)
    )
    assert r.status_code == 200
    md = r.text
    assert "No injects were released." in md
    assert "No communications were sent." in md


async def test_report_counts_attendance_by_enrolled_role_and_participant_team(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    active_exercise,
    participant: User,
):
    """Attendance roles are snapshots; later global role edits cannot rewrite an AAR."""
    from app.services.exercise_service import enrol_member

    co_facilitator = User(
        email="cofacilitator@example.com",
        display_name="Co-facilitator",
        hashed_password=hash_password("password1234"),
        role=UserRole.facilitator,
    )
    observer = User(
        email="observer@example.com",
        display_name="Observer",
        hashed_password=hash_password("password1234"),
        role=UserRole.observer,
    )
    legal_participant = User(
        email="legal-participant@example.com",
        display_name="Legal Participant",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="legal",
    )
    unassigned_participant = User(
        email="unassigned-participant@example.com",
        display_name="Unassigned Participant",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
    )
    session.add_all(
        [co_facilitator, observer, legal_participant, unassigned_participant]
    )
    await session.commit()
    for user in (co_facilitator, observer, legal_participant, unassigned_participant):
        await session.refresh(user)

    await enrol_member(session, exercise=active_exercise, user_id=co_facilitator.id)
    await enrol_member(session, exercise=active_exercise, user_id=observer.id)
    await enrol_member(session, exercise=active_exercise, user_id=legal_participant.id)
    await enrol_member(session, exercise=active_exercise, user_id=unassigned_participant.id)

    # Change every global role after enrolment. The report must retain the attendance
    # roles captured above (including the fixture participant's original role).
    participant.role = UserRole.facilitator
    co_facilitator.role = UserRole.participant
    observer.role = UserRole.participant
    legal_participant.role = UserRole.observer
    unassigned_participant.role = UserRole.observer
    session.add_all(
        [participant, co_facilitator, observer, legal_participant, unassigned_participant]
    )
    await session.commit()

    headers = _bearer(facilitator_token)
    response = await client.get(
        f"/api/exercises/{active_exercise.id}/report", headers=headers
    )
    assert response.status_code == 200
    report = response.json()
    assert report["participant_count"] == 3
    assert report["facilitator_count"] == 1
    assert report["observer_count"] == 1
    assert report["member_count"] == 5
    assert {team["id"]: team["participant_count"] for team in report["teams"]} == {
        "it_ops": 1,
        "legal": 1,
    }
    assert report["unassigned_participant_count"] == 1
    assert (
        sum(team["participant_count"] for team in report["teams"])
        + report["unassigned_participant_count"]
        == report["participant_count"]
    )

    markdown = (
        await client.get(
            f"/api/exercises/{active_exercise.id}/report.md", headers=headers
        )
    ).text
    assert "**Participants:** 3" in markdown
    assert "**Facilitators:** 1" in markdown
    assert "**Observers:** 1" in markdown
    assert "**Total enrolled:** 5" in markdown
    assert "IT Ops (1), Legal (1), Unassigned / other (1)" in markdown


async def test_report_has_zeroed_role_and_team_counts_for_empty_membership(
    client: AsyncClient, facilitator_token: str, draft_exercise
):
    response = await client.get(
        f"/api/exercises/{draft_exercise.id}/report",
        headers=_bearer(facilitator_token),
    )
    assert response.status_code == 200
    report = response.json()
    assert report["member_count"] == 0
    assert report["participant_count"] == 0
    assert report["facilitator_count"] == 0
    assert report["observer_count"] == 0
    assert report["unassigned_participant_count"] == 0
    assert all(team["participant_count"] == 0 for team in report["teams"])


async def test_report_owner_scoping(
    client: AsyncClient,
    second_facilitator_token: str,
    participant_token: str,
    active_exercise,
):
    for path in (f"/api/exercises/{active_exercise.id}/report",
                 f"/api/exercises/{active_exercise.id}/report.md"):
        r1 = await client.get(path, headers=_bearer(second_facilitator_token))
        r2 = await client.get(path, headers=_bearer(participant_token))
        assert r1.status_code == 403
        assert r2.status_code == 403


async def test_summary_unavailable_without_provider(
    client: AsyncClient, facilitator_token: str, active_exercise
):
    url = f"/api/exercises/{active_exercise.id}/report/summary"
    hdr = _bearer(facilitator_token)
    # No provider → drafting is unavailable and the POST is rejected.
    with patch("app.routers.exercises.active_provider", return_value=None):
        state = await client.get(url, headers=hdr)
        assert state.status_code == 200
        assert state.json()["available"] is False
        assert state.json()["summary"] is None

        post = await client.post(url, headers=hdr)
        assert post.status_code == 409


async def test_summary_requires_exercise_llm_opt_in(
    client: AsyncClient, facilitator_token: str, active_exercise
):
    url = f"/api/exercises/{active_exercise.id}/report/summary"
    # Provider present but the exercise's AI toggle is off (default) → still 409.
    with patch("app.routers.exercises.active_provider", return_value=_fake_provider("x")):
        post = await client.post(url, headers=_bearer(facilitator_token))
        assert post.status_code == 409


async def test_generate_and_edit_executive_summary(
    client: AsyncClient,
    facilitator: User,
    facilitator_token: str,
    session: AsyncSession,
    sample_scenario,
    participant,
):
    from app.services.exercise_service import create_exercise, enrol_member, transition_state
    from app.services.llm_service import generate_executive_summary

    ex = await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="AI Exercise",
        created_by=facilitator.id,
        llm_enabled=True,
    )
    await enrol_member(session, exercise=ex, user_id=participant.id)
    await transition_state(session, ex, ExerciseState.active)
    await _seed_completed(session, ex, participant)

    # Generate directly at the service seam (as test_llm does for assessments).
    with patch(
        "app.services.llm_service.active_provider",
        return_value=_fake_provider("The team contained the incident quickly."),
    ):
        summary = await generate_executive_summary(session, ex.id)
    assert summary is not None
    assert summary.llm_model == "test:model"
    assert summary.edited is False

    # The summary now appears in the report and its state.
    md_resp = await client.get(
        f"/api/exercises/{ex.id}/report.md", headers=_bearer(facilitator_token)
    )
    md = md_resp.text
    assert "## Executive Summary" in md
    assert "contained the incident quickly" in md

    # Facilitator edits it → edited flag flips.
    patched = await client.patch(
        f"/api/exercises/{ex.id}/report/summary",
        json={"summary_text": "Revised executive summary."},
        headers=_bearer(facilitator_token),
    )
    assert patched.status_code == 200
    assert patched.json()["edited"] is True
    assert patched.json()["summary_text"] == "Revised executive summary."
