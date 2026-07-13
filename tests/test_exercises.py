import asyncio
import csv
import io
import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import (
    Exercise,
    ExerciseMember,
    ExerciseState,
    ExerciseStateTransition,
)
from app.models.inject import Inject
from app.models.user import User, UserRole
from app.schemas.scenario_json import InjectNode, ScenarioDefinition
from app.services.exercise_service import transition_state
from app.services.progression_service import inject_audience_contexts
from app.services.ws_manager import manager


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _authz_denials(caplog) -> list[dict]:
    events = []
    for rec in caplog.records:
        if rec.name == "iceberg_ttx.audit":
            try:
                events.append(json.loads(rec.getMessage()))
            except ValueError:
                pass
    return [e for e in events if e.get("action") == "authz.denied" and e.get("result") == "deny"]


def test_targeted_shared_inject_excludes_non_target_participant_contexts():
    inject = Inject(
        id=9,
        exercise_id=4,
        title="Joint response",
        content="Respond",
        target_teams=["it_ops", "legal"],
    )
    members = [
        ExerciseMember(
            exercise_id=4,
            user_id=index,
            group_id=group,
            role_at_enrolment=UserRole.participant,
        )
        for index, group in enumerate(("it_ops", "legal", "finance"), start=1)
    ]
    assert inject_audience_contexts(inject, members) == {"it_ops", "legal"}


# ── CRUD ──────────────────────────────────────────────────────────────────────


async def test_create_exercise(client: AsyncClient, facilitator_token: str, sample_scenario):
    r = await client.post(
        "/api/exercises",
        json={"scenario_id": sample_scenario.id, "title": "My Exercise"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "My Exercise"
    assert data["state"] == "draft"
    assert data["current_node_id"] == "inject_01"
    assert data["llm_enabled"] is False


async def test_create_exercise_with_llm(
    client: AsyncClient, facilitator_token: str, sample_scenario
):
    r = await client.post(
        "/api/exercises",
        json={"scenario_id": sample_scenario.id, "title": "LLM Exercise", "llm_enabled": True},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    assert r.json()["llm_enabled"] is True


async def test_create_exercise_seeds_shared_and_group_injects(
    session: AsyncSession, facilitator: User
):
    from app.services.exercise_service import create_exercise
    from app.services.scenario_service import create_scenario

    scenario = await create_scenario(
        session,
        definition=ScenarioDefinition(
            title="Grouped Scenario",
            participant_teams=[
                {"id": "it_ops", "label": "IT Ops"},
                {"id": "legal", "label": "Legal"},
            ],
            injects=[
                InjectNode(id="shared", title="Shared", content="All groups"),
                InjectNode(
                    id="targeted",
                    title="Targeted",
                    content="Split by group",
                    target_teams=["it_ops", "legal"],
                ),
            ],
            start_inject_id="shared",
        ),
        created_by=facilitator.id,
    )

    exercise = await create_exercise(
        session,
        scenario_id=scenario.id,
        title="Grouped Exercise",
        created_by=facilitator.id,
    )
    injects = (await session.exec(select(Inject).where(Inject.exercise_id == exercise.id))).all()

    shared = [i for i in injects if i.scenario_node_id == "shared"]
    targeted = [i for i in injects if i.scenario_node_id == "targeted"]
    assert len(shared) == 1
    assert shared[0].group_id is None
    assert {i.group_id for i in targeted} == {"it_ops", "legal"}


async def test_create_exercise_missing_scenario(client: AsyncClient, facilitator_token: str):
    r = await client.post(
        "/api/exercises",
        json={"scenario_id": 9999, "title": "Ghost"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


async def test_create_exercise_participant_forbidden(
    client: AsyncClient, participant_token: str, sample_scenario
):
    r = await client.post(
        "/api/exercises",
        json={"scenario_id": sample_scenario.id, "title": "No"},
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


async def test_list_exercises(client: AsyncClient, facilitator_token: str, draft_exercise):
    r = await client.get("/api/exercises", headers={"Authorization": f"Bearer {facilitator_token}"})
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()]
    assert draft_exercise.id in ids


async def test_list_exercises_participant_allowed(
    client: AsyncClient, participant_token: str, draft_exercise
):
    r = await client.get("/api/exercises", headers={"Authorization": f"Bearer {participant_token}"})
    assert r.status_code == 200
    assert r.json() == []


async def test_participant_sees_enrolled_exercise(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = await client.get("/api/exercises", headers={"Authorization": f"Bearer {participant_token}"})
    assert r.status_code == 200
    assert [ex["id"] for ex in r.json()] == [active_exercise.id]


# ── Multiple concurrent active exercises (#96) ────────────────────────────────


async def _make_active(
    session: AsyncSession, facilitator: User, scenario, title: str, started_at: datetime
) -> Exercise:
    """An active exercise with an explicit started_at, so ordering is deterministic
    rather than dependent on wall-clock proximity."""
    from app.services.exercise_service import create_exercise, transition_state

    ex = await create_exercise(
        session, scenario_id=scenario.id, title=title, created_by=facilitator.id
    )
    ex = await transition_state(session, ex, ExerciseState.active)
    ex.started_at = started_at
    session.add(ex)
    await session.commit()
    await session.refresh(ex)
    return ex


async def test_list_exercises_two_simultaneously_active(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    facilitator: User,
    sample_scenario,
):
    """The backend permits N active exercises; the list must return the whole set,
    not collapse it to one (#96)."""
    now = datetime.now(UTC)
    await _make_active(session, facilitator, sample_scenario, "A", now - timedelta(hours=2))
    await _make_active(session, facilitator, sample_scenario, "B", now - timedelta(hours=1))

    r = await client.get("/api/exercises", headers=_bearer(facilitator_token))
    assert r.status_code == 200
    active = [e for e in r.json() if e["state"] == "active"]
    assert len(active) == 2
    assert {e["title"] for e in active} == {"A", "B"}


async def test_list_exercises_ordered_most_recently_started_first(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    facilitator: User,
    sample_scenario,
):
    now = datetime.now(UTC)
    older = await _make_active(
        session, facilitator, sample_scenario, "older", now - timedelta(hours=2)
    )
    newer = await _make_active(
        session, facilitator, sample_scenario, "newer", now - timedelta(hours=1)
    )

    r = await client.get("/api/exercises", headers=_bearer(facilitator_token))
    assert [e["id"] for e in r.json()] == [newer.id, older.id]


async def test_list_exercises_drafts_sort_after_started(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    facilitator: User,
    sample_scenario,
    draft_exercise: Exercise,
):
    """Drafts have started_at IS NULL — NULLS LAST puts them below anything that ran."""
    started = await _make_active(
        session, facilitator, sample_scenario, "started", datetime.now(UTC) - timedelta(hours=1)
    )
    r = await client.get("/api/exercises", headers=_bearer(facilitator_token))
    ids = [e["id"] for e in r.json()]
    assert ids.index(started.id) < ids.index(draft_exercise.id)


async def test_list_exercises_includes_scenario_title(
    client: AsyncClient, facilitator_token: str, draft_exercise: Exercise, sample_scenario
):
    """The dashboard binds scenario_title; it must actually be populated."""
    r = await client.get("/api/exercises", headers=_bearer(facilitator_token))
    row = next(e for e in r.json() if e["id"] == draft_exercise.id)
    assert row["scenario_title"] == sample_scenario.title


async def test_participant_can_list_exercise_team_labels(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/teams",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    assert r.json() == [
        {"id": "it_ops", "label": "IT Ops"},
        {"id": "legal", "label": "Legal"},
    ]


async def test_facilitator_preview_participant_still_lists_exercises_for_testing(
    client: AsyncClient,
    facilitator_token: str,
    draft_exercise: Exercise,
    active_exercise: Exercise,
):
    assert draft_exercise.id is not None
    assert active_exercise.id is not None
    client.cookies.set("dt_view_role", "participant")
    r = await client.get("/api/exercises", headers={"Authorization": f"Bearer {facilitator_token}"})
    assert r.status_code == 200
    ids = {ex["id"] for ex in r.json()}
    assert draft_exercise.id in ids
    assert active_exercise.id in ids


async def test_get_exercise(client: AsyncClient, facilitator_token: str, draft_exercise):
    r = await client.get(
        f"/api/exercises/{draft_exercise.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == draft_exercise.id


async def test_get_exercise_participant_not_enrolled_forbidden(
    client: AsyncClient, participant_token: str, draft_exercise
):
    r = await client.get(
        f"/api/exercises/{draft_exercise.id}",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


async def test_get_exercise_not_found(client: AsyncClient, facilitator_token: str):
    r = await client.get(
        "/api/exercises/9999", headers={"Authorization": f"Bearer {facilitator_token}"}
    )
    assert r.status_code == 404


async def test_update_exercise(client: AsyncClient, facilitator_token: str, draft_exercise):
    r = await client.put(
        f"/api/exercises/{draft_exercise.id}",
        json={"title": "Renamed", "llm_enabled": True},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Renamed"
    assert data["llm_enabled"] is True


async def test_update_exercise_preserves_omitted_fields(
    client: AsyncClient, facilitator_token: str, draft_exercise
):
    headers = {"Authorization": f"Bearer {facilitator_token}"}
    await client.put(
        f"/api/exercises/{draft_exercise.id}", json={"llm_enabled": True}, headers=headers
    )
    # Updating only the title must not reset llm_enabled back to its default.
    r = await client.put(
        f"/api/exercises/{draft_exercise.id}", json={"title": "Renamed"}, headers=headers
    )
    assert r.status_code == 200
    data = r.json()
    assert data["title"] == "Renamed"
    assert data["llm_enabled"] is True


async def test_delete_draft_exercise(
    client: AsyncClient,
    facilitator_token: str,
    session: AsyncSession,
    sample_scenario,
    facilitator: User,
):
    from app.services.exercise_service import create_exercise

    ex = await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="To Delete",
        created_by=facilitator.id,
    )
    r = await client.delete(
        f"/api/exercises/{ex.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 204
    assert (await session.get(Exercise, ex.id)) is None


async def test_delete_active_exercise_forbidden(
    client: AsyncClient, facilitator_token: str, active_exercise
):
    r = await client.delete(
        f"/api/exercises/{active_exercise.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


# ── Lifecycle ─────────────────────────────────────────────────────────────────


async def test_start_exercise(client: AsyncClient, facilitator_token: str, draft_exercise):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "active"
    assert data["started_at"] is not None


async def test_start_persists_authoritative_transition_history(
    client: AsyncClient,
    facilitator_token: str,
    facilitator: User,
    session: AsyncSession,
    draft_exercise: Exercise,
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers=_bearer(facilitator_token),
    )
    assert r.status_code == 200

    transitions = (
        await session.exec(
            select(ExerciseStateTransition).where(
                ExerciseStateTransition.exercise_id == draft_exercise.id
            )
        )
    ).all()
    assert len(transitions) == 1
    transition = transitions[0]
    assert transition.from_state == ExerciseState.draft
    assert transition.to_state == ExerciseState.active
    assert transition.actor_id == facilitator.id
    assert transition.transitioned_at.isoformat() == r.json()["started_at"]


async def test_pause_exercise(client: AsyncClient, facilitator_token: str, active_exercise):
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/pause",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "paused"


async def test_resume_exercise(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise
):
    paused = await transition_state(session, active_exercise, ExerciseState.paused)
    r = await client.post(
        f"/api/exercises/{paused.id}/resume",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "active"


async def test_complete_active_exercise(
    client: AsyncClient, facilitator_token: str, active_exercise
):
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/complete",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "completed"
    assert data["ended_at"] is not None


async def test_completed_exercise_blocks_operations_but_keeps_after_action_workflows(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    headers = _bearer(facilitator_token)
    completed = await client.post(f"/api/exercises/{active_exercise.id}/complete", headers=headers)
    assert completed.status_code == 200

    members = await client.get(
        f"/api/exercises/{active_exercise.id}/members", headers=headers
    )
    assert members.status_code == 200
    member_id = members.json()[0]["user_id"]
    injects = await client.get(
        f"/api/exercises/{active_exercise.id}/injects", headers=headers
    )
    assert injects.status_code == 200
    inject_id = injects.json()[0]["id"]

    # Operational changes across the exercise timeline are refused.
    assert (
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects",
            json={"title": "Late", "content": "Must not alter history"},
            headers=headers,
        )
    ).status_code == 409
    assert (
        await client.patch(
            f"/api/exercises/{active_exercise.id}/members/{member_id}",
            json={"group_id": "legal"},
            headers=headers,
        )
    ).status_code == 409
    assert (
        await client.delete(
            f"/api/exercises/{active_exercise.id}/members/{member_id}", headers=headers
        )
    ).status_code == 409
    assert (
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/release",
            headers=headers,
        )
    ).status_code == 409
    assert (
        await client.patch(
            f"/api/exercises/{active_exercise.id}/injects/{inject_id}/schedule",
            json={"release_offset_minutes": 10},
            headers=headers,
        )
    ).status_code == 409
    assert (
        await client.post(
            f"/api/exercises/{active_exercise.id}/communications/inject",
            json={"external_entity": "NCSC", "subject": "Late", "body": "No mutation"},
            headers=headers,
        )
    ).status_code == 409
    assert (
        await client.post(
            f"/api/exercises/{active_exercise.id}/responses",
            json={"inject_id": 1, "content": "Late response"},
            headers=_bearer(participant_token),
        )
    ).status_code == 409

    # Debrief and evidence export are intentionally available after completion.
    debrief = await client.put(
        f"/api/exercises/{active_exercise.id}",
        json={"debrief_notes": "Capture the improvement actions."},
        headers=headers,
    )
    assert debrief.status_code == 200
    export = await client.get(f"/api/exercises/{active_exercise.id}/export", headers=headers)
    assert export.status_code == 200


async def test_complete_paused_exercise(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise
):
    paused = await transition_state(session, active_exercise, ExerciseState.paused)
    r = await client.post(
        f"/api/exercises/{paused.id}/complete",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == "completed"


async def test_invalid_transition_draft_to_completed(
    client: AsyncClient, facilitator_token: str, draft_exercise
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/complete",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


async def test_invalid_transition_completed_to_active(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, active_exercise
):
    await transition_state(session, active_exercise, ExerciseState.completed)
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/start",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


async def test_stale_transition_cannot_revert_completed_exercise(
    sample_definition: ScenarioDefinition,
):
    """Two real sessions observe active; completion wins and the stale pause loses."""
    from app.database import engine
    from app.services.exercise_service import create_exercise
    from app.services.scenario_service import create_scenario

    user_id = scenario_id = exercise_id = None
    unique = uuid4().hex
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            facilitator = User(
                email=f"lifecycle-{unique}@example.test",
                display_name="Lifecycle Concurrency",
                role=UserRole.facilitator,
            )
            setup.add(facilitator)
            await setup.commit()
            await setup.refresh(facilitator)
            user_id = facilitator.id
            scenario = await create_scenario(
                setup, definition=sample_definition, created_by=facilitator.id
            )
            scenario_id = scenario.id
            exercise = await create_exercise(
                setup,
                scenario_id=scenario.id,
                title=f"Lifecycle concurrency {unique}",
                created_by=facilitator.id,
            )
            exercise = await transition_state(
                setup,
                exercise,
                ExerciseState.active,
                actor_id=facilitator.id,
            )
            exercise_id = exercise.id

        async with (
            AsyncSession(engine, expire_on_commit=False) as completion_session,
            AsyncSession(engine, expire_on_commit=False) as stale_pause_session,
        ):
            completion_view = await completion_session.get(Exercise, exercise_id)
            stale_pause_view = await stale_pause_session.get(Exercise, exercise_id)
            assert completion_view is not None and stale_pause_view is not None
            assert completion_view.state == stale_pause_view.state == ExerciseState.active

            completed = await transition_state(
                completion_session,
                completion_view,
                ExerciseState.completed,
                actor_id=user_id,
            )
            assert completed.state == ExerciseState.completed

            with pytest.raises(HTTPException) as exc_info:
                await transition_state(
                    stale_pause_session,
                    stale_pause_view,
                    ExerciseState.paused,
                    actor_id=user_id,
                )
            assert exc_info.value.status_code == 409

        async with AsyncSession(engine, expire_on_commit=False) as verify:
            stored = await verify.get(Exercise, exercise_id)
            assert stored is not None
            assert stored.state == ExerciseState.completed
            transitions = (
                await verify.exec(
                    select(ExerciseStateTransition).where(
                        ExerciseStateTransition.exercise_id == exercise_id
                    )
                )
            ).all()
            assert [(row.from_state, row.to_state) for row in transitions] == [
                (ExerciseState.draft, ExerciseState.active),
                (ExerciseState.active, ExerciseState.completed),
            ]
    finally:
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            if exercise_id is not None:
                exercise = await cleanup.get(Exercise, exercise_id)
                if exercise is not None:
                    await cleanup.delete(exercise)
                    await cleanup.commit()
            if scenario_id is not None:
                from app.models.scenario import Scenario

                scenario = await cleanup.get(Scenario, scenario_id)
                if scenario is not None:
                    await cleanup.delete(scenario)
                    await cleanup.commit()
            if user_id is not None:
                user = await cleanup.get(User, user_id)
                if user is not None:
                    await cleanup.delete(user)
                    await cleanup.commit()


async def test_release_serializes_against_concurrent_roster_change(monkeypatch):
    """A roster writer waiting behind release must observe the durable lock (#194)."""
    from app.database import engine
    from app.services import progression_service
    from app.services.exercise_service import create_exercise, enrol_member
    from app.services.inject_service import release_inject
    from app.services.scenario_service import create_scenario

    user_ids: list[int] = []
    scenario_id = exercise_id = None
    release_task = roster_task = None
    unique = uuid4().hex
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            facilitator = User(
                email=f"roster-lock-owner-{unique}@example.test",
                display_name="Roster Lock Owner",
                role=UserRole.facilitator,
            )
            initial = User(
                email=f"roster-lock-initial-{unique}@example.test",
                display_name="Initial Participant",
                role=UserRole.participant,
                team="it_ops",
            )
            late = User(
                email=f"roster-lock-late-{unique}@example.test",
                display_name="Late Participant",
                role=UserRole.participant,
                team="legal",
            )
            setup.add_all([facilitator, initial, late])
            await setup.commit()
            for user in (facilitator, initial, late):
                await setup.refresh(user)
                assert user.id is not None
                user_ids.append(user.id)
            scenario = await create_scenario(
                setup,
                definition=ScenarioDefinition(
                    title="Roster serialization",
                    participant_teams=[
                        {"id": "it_ops", "label": "IT Ops"},
                        {"id": "legal", "label": "Legal"},
                    ],
                    injects=[InjectNode(id="shared", title="Shared", content="Respond")],
                    start_inject_id="shared",
                ),
                created_by=facilitator.id,
            )
            scenario_id = scenario.id
            exercise = await create_exercise(
                setup,
                scenario_id=scenario.id,
                title="Roster serialization",
                created_by=facilitator.id,
            )
            exercise_id = exercise.id
            await enrol_member(
                setup, exercise=exercise, user_id=initial.id, group_id="it_ops"
            )
            exercise = await transition_state(setup, exercise, ExerciseState.active)
            inject = (
                await setup.exec(
                    select(Inject).where(Inject.exercise_id == exercise.id)
                )
            ).one()
            inject_id = inject.id
            owner_id = facilitator.id
            late_id = late.id

        snapshot_started = asyncio.Event()
        allow_snapshot = asyncio.Event()
        original_seed = progression_service.seed_inject_resolution_contexts

        async def paused_seed(session, inject):
            snapshot_started.set()
            await allow_snapshot.wait()
            return await original_seed(session, inject)

        monkeypatch.setattr(
            progression_service, "seed_inject_resolution_contexts", paused_seed
        )
        async with (
            AsyncSession(engine, expire_on_commit=False) as release_session,
            AsyncSession(engine, expire_on_commit=False) as roster_session,
        ):
            release_view = await release_session.get(Inject, inject_id)
            roster_view = await roster_session.get(Exercise, exercise_id)
            assert release_view is not None and roster_view is not None
            release_task = asyncio.create_task(
                release_inject(release_session, release_view, released_by=owner_id)
            )
            await asyncio.wait_for(snapshot_started.wait(), timeout=5)
            roster_task = asyncio.create_task(
                enrol_member(
                    roster_session,
                    exercise=roster_view,
                    user_id=late_id,
                    group_id="legal",
                )
            )
            await asyncio.sleep(0.05)
            assert not roster_task.done()
            allow_snapshot.set()
            released = await asyncio.wait_for(release_task, timeout=5)
            assert released.state.value == "released"
            with pytest.raises(HTTPException) as exc_info:
                await asyncio.wait_for(roster_task, timeout=5)
            assert exc_info.value.status_code == 409

        async with AsyncSession(engine, expire_on_commit=False) as verify:
            members = (
                await verify.exec(
                    select(ExerciseMember).where(
                        ExerciseMember.exercise_id == exercise_id
                    )
                )
            ).all()
            assert {member.group_id for member in members} == {"it_ops"}
    finally:
        for task in (release_task, roster_task):
            if task is not None and not task.done():
                task.cancel()
        async with AsyncSession(engine, expire_on_commit=False) as cleanup:
            if exercise_id is not None:
                exercise = await cleanup.get(Exercise, exercise_id)
                if exercise is not None:
                    await cleanup.delete(exercise)
                    await cleanup.commit()
            if scenario_id is not None:
                from app.models.scenario import Scenario

                scenario = await cleanup.get(Scenario, scenario_id)
                if scenario is not None:
                    await cleanup.delete(scenario)
                    await cleanup.commit()
            for user_id in user_ids:
                user = await cleanup.get(User, user_id)
                if user is not None:
                    await cleanup.delete(user)
                    await cleanup.commit()


async def test_commit_failure_rolls_back_transition_and_skips_broadcast(
    monkeypatch,
    session: AsyncSession,
    facilitator: User,
    draft_exercise: Exercise,
):
    from app.routers.exercises import _transition
    from app.services import audit_service

    exercise_id = draft_exercise.id
    broadcast = AsyncMock()
    audit_emit = Mock()
    monkeypatch.setattr(manager, "broadcast_to_exercise", broadcast)
    monkeypatch.setattr(audit_service, "emit", audit_emit)

    async def fail_commit(_session) -> None:
        raise RuntimeError("simulated commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="simulated commit failure"):
            await _transition(
                exercise_id,
                facilitator,
                session,
                ExerciseState.active,
            )

    stored = await session.get(Exercise, exercise_id, populate_existing=True)
    assert stored is not None
    assert stored.state == ExerciseState.draft
    transitions = (
        await session.exec(
            select(ExerciseStateTransition).where(
                ExerciseStateTransition.exercise_id == exercise_id
            )
        )
    ).all()
    assert transitions == []
    broadcast.assert_not_awaited()
    audit_emit.assert_not_called()


async def test_start_sets_started_at_only_once(
    client: AsyncClient, facilitator_token: str, session: AsyncSession, draft_exercise
):
    await client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    first_start = (await session.get(Exercise, draft_exercise.id)).started_at

    await client.post(
        f"/api/exercises/{draft_exercise.id}/pause",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    await client.post(
        f"/api/exercises/{draft_exercise.id}/resume",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    second_start = (await session.get(Exercise, draft_exercise.id)).started_at
    assert first_start == second_start


async def test_lifecycle_participant_forbidden(
    client: AsyncClient, participant_token: str, draft_exercise
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/start",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


# ── Members ───────────────────────────────────────────────────────────────────


async def test_enrol_member(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    assert r.json()["user_id"] == participant.id
    assert r.json()["group_id"] == "it_ops"
    assert r.json()["role_at_enrolment"] == "participant"


async def test_enrol_member_with_group_id(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id, "group_id": "legal"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201
    assert r.json()["group_id"] == "legal"


async def test_enrol_member_rejects_unknown_group(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id, "group_id": "unknown"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 422


async def test_enrol_member_rejects_unknown_user(
    client: AsyncClient, facilitator_token: str, draft_exercise
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": 2_147_483_647},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )

    assert r.status_code == 404
    assert r.json() == {"detail": "User not found"}


async def test_enrol_member_idempotent(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 201

    members = (
        await client.get(
            f"/api/exercises/{draft_exercise.id}/members",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    ).json()
    assert sum(1 for m in members if m["user_id"] == participant.id) == 1


async def test_list_members(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = await client.get(
        f"/api/exercises/{draft_exercise.id}/members",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert any(m["user_id"] == participant.id for m in r.json())


async def test_update_member_group(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = await client.patch(
        f"/api/exercises/{draft_exercise.id}/members/{participant.id}",
        json={"group_id": "legal"},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["group_id"] == "legal"


async def test_remove_member(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = await client.delete(
        f"/api/exercises/{draft_exercise.id}/members/{participant.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 204

    members = (
        await client.get(
            f"/api/exercises/{draft_exercise.id}/members",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )
    ).json()
    assert not any(m["user_id"] == participant.id for m in members)


async def test_remove_member_not_found(
    client: AsyncClient, facilitator_token: str, draft_exercise, participant: User
):
    r = await client.delete(
        f"/api/exercises/{draft_exercise.id}/members/{participant.id}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


async def test_enrol_member_participant_forbidden(
    client: AsyncClient, participant_token: str, draft_exercise, participant: User
):
    r = await client.post(
        f"/api/exercises/{draft_exercise.id}/members",
        json={"user_id": participant.id},
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


# ── #12: per-exercise facilitator ownership scoping ───────────────────────────


async def test_other_facilitator_denied_read(
    client: AsyncClient, second_facilitator_token: str, draft_exercise, participant: User, caplog
):
    with caplog.at_level(logging.INFO, logger="iceberg_ttx.audit"):
        r = await client.get(
            f"/api/exercises/{draft_exercise.id}", headers=_bearer(second_facilitator_token)
        )
    assert r.status_code == 403
    denials = _authz_denials(caplog)
    assert any(str(e.get("target_id")) == str(draft_exercise.id) for e in denials)


async def test_other_facilitator_denied_mutations(
    client: AsyncClient, second_facilitator_token: str, draft_exercise, participant: User
):
    h = _bearer(second_facilitator_token)
    eid = draft_exercise.id
    assert (
        await client.put(f"/api/exercises/{eid}", json={"title": "hijack"}, headers=h)
    ).status_code == 403
    assert (await client.delete(f"/api/exercises/{eid}", headers=h)).status_code == 403
    assert (await client.post(f"/api/exercises/{eid}/start", headers=h)).status_code == 403
    assert (await client.get(f"/api/exercises/{eid}/members", headers=h)).status_code == 403
    assert (
        await client.post(
            f"/api/exercises/{eid}/members", json={"user_id": participant.id}, headers=h
        )
    ).status_code == 403
    assert (await client.get(f"/api/exercises/{eid}/export", headers=h)).status_code == 403
    assert (await client.get(f"/api/exercises/{eid}/export.csv", headers=h)).status_code == 403


async def test_other_facilitator_cannot_delete_inject(
    client: AsyncClient,
    facilitator_token: str,
    second_facilitator_token: str,
    draft_exercise,
):
    eid = draft_exercise.id
    owner_headers = _bearer(facilitator_token)
    injects = (await client.get(f"/api/exercises/{eid}/injects", headers=owner_headers)).json()
    inject_id = injects[0]["id"]

    denied = await client.delete(
        f"/api/exercises/{eid}/injects/{inject_id}",
        headers=_bearer(second_facilitator_token),
    )
    assert denied.status_code == 403
    assert (
        await client.get(f"/api/exercises/{eid}/injects/{inject_id}", headers=owner_headers)
    ).status_code == 200


async def test_owner_facilitator_still_allowed(
    client: AsyncClient, facilitator_token: str, draft_exercise
):
    h = _bearer(facilitator_token)
    eid = draft_exercise.id
    assert (await client.get(f"/api/exercises/{eid}", headers=h)).status_code == 200
    assert (
        await client.put(f"/api/exercises/{eid}", json={"title": "renamed"}, headers=h)
    ).status_code == 200
    assert (await client.get(f"/api/exercises/{eid}/export", headers=h)).status_code == 200


async def test_cofacilitator_member_gains_access(
    client: AsyncClient,
    facilitator_token: str,
    second_facilitator: User,
    second_facilitator_token: str,
    draft_exercise,
):
    eid = draft_exercise.id
    # Owner enrols the second facilitator as a co-facilitator (member).
    enrol = await client.post(
        f"/api/exercises/{eid}/members",
        json={"user_id": second_facilitator.id},
        headers=_bearer(facilitator_token),
    )
    assert enrol.status_code == 201
    # Now the co-facilitator can read and mutate.
    h = _bearer(second_facilitator_token)
    assert (await client.get(f"/api/exercises/{eid}", headers=h)).status_code == 200
    assert (
        await client.put(f"/api/exercises/{eid}", json={"title": "co-edited"}, headers=h)
    ).status_code == 200


async def test_admin_has_global_access(client: AsyncClient, admin_token: str, draft_exercise):
    h = _bearer(admin_token)
    eid = draft_exercise.id
    assert (await client.get(f"/api/exercises/{eid}", headers=h)).status_code == 200
    assert (
        await client.put(f"/api/exercises/{eid}", json={"title": "admin-touch"}, headers=h)
    ).status_code == 200


async def test_list_exercises_scoped_per_facilitator(
    client: AsyncClient,
    facilitator_token: str,
    second_facilitator_token: str,
    admin_token: str,
    draft_exercise,
):
    eid = draft_exercise.id
    owner_ids = [
        e["id"]
        for e in (await client.get("/api/exercises", headers=_bearer(facilitator_token))).json()
    ]
    assert eid in owner_ids

    other = (await client.get("/api/exercises", headers=_bearer(second_facilitator_token))).json()
    assert other == []

    admin_ids = [
        e["id"] for e in (await client.get("/api/exercises", headers=_bearer(admin_token))).json()
    ]
    assert eid in admin_ids


async def test_participant_member_is_read_only(
    client: AsyncClient, participant_token: str, active_exercise
):
    # active_exercise enrols `participant` as a member — read is allowed…
    h = _bearer(participant_token)
    eid = active_exercise.id
    assert (await client.get(f"/api/exercises/{eid}", headers=h)).status_code == 200
    # …but mutation is still blocked (require_role facilitator).
    assert (
        await client.put(f"/api/exercises/{eid}", json={"title": "nope"}, headers=h)
    ).status_code == 403


async def test_all_projections_share_authoritative_inject_resolution(
    client: AsyncClient,
    facilitator: User,
    facilitator_token: str,
    second_facilitator: User,
    session: AsyncSession,
):
    """JSON, CSV, report, and timeline agree through partial shared resolution."""
    from app.services.auth_service import create_access_token, hash_password
    from app.services.exercise_service import create_exercise, enrol_member
    from app.services.scenario_service import create_scenario

    unassigned = User(
        email="unassigned-resolution@example.com",
        display_name="Unassigned Participant",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team=None,
    )
    legal = User(
        email="legal-resolution@example.com",
        display_name="Legal Participant",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="legal",
    )
    observer = User(
        email="observer-resolution@example.com",
        display_name="Observer",
        hashed_password=hash_password("password1234"),
        role=UserRole.observer,
        team=None,
    )
    late_participant = User(
        email="late-resolution@example.com",
        display_name="Late Participant",
        hashed_password=hash_password("password1234"),
        role=UserRole.participant,
        team="legal",
    )
    session.add_all([unassigned, legal, observer, late_participant])
    await session.commit()
    for user in (unassigned, legal, observer, late_participant):
        await session.refresh(user)

    scenario = await create_scenario(
        session,
        definition=ScenarioDefinition(
            title="Shared resolution authority",
            participant_teams=[{"id": "legal", "label": "Legal"}],
            injects=[InjectNode(id="all_hands", title="All hands", content="Respond")],
            start_inject_id="all_hands",
        ),
        created_by=facilitator.id,
    )
    exercise = await create_exercise(
        session,
        title="Shared resolution authority",
        scenario_id=scenario.id,
        created_by=facilitator.id,
    )
    assert (
        unassigned.id
        and legal.id
        and observer.id
        and late_participant.id
        and second_facilitator.id
    )
    await enrol_member(session, exercise=exercise, user_id=unassigned.id)
    await enrol_member(session, exercise=exercise, user_id=legal.id, group_id="legal")
    # These contexts can never submit participant responses and must not count.
    await enrol_member(session, exercise=exercise, user_id=observer.id)
    await enrol_member(
        session, exercise=exercise, user_id=second_facilitator.id, group_id="legal"
    )
    await transition_state(session, exercise, ExerciseState.active)

    owner = _bearer(facilitator_token)
    injects = (
        await client.get(f"/api/exercises/{exercise.id}/injects", headers=owner)
    ).json()
    inject_id = injects[0]["id"]
    assert injects[0]["group_id"] is None
    assert (
        await client.post(
            f"/api/exercises/{exercise.id}/injects/{inject_id}/release", headers=owner
        )
    ).status_code == 200

    async def submit(user: User, content: str) -> None:
        token = create_access_token(subject=user.email, role=user.role.value)
        response = await client.post(
            f"/api/exercises/{exercise.id}/responses",
            json={"inject_id": inject_id, "content": content},
            headers=_bearer(token),
        )
        assert response.status_code == 201, response.text

    async def projections() -> tuple[dict, dict, list[dict], dict]:
        exported = (
            await client.get(f"/api/exercises/{exercise.id}/export", headers=owner)
        ).json()
        report = (
            await client.get(f"/api/exercises/{exercise.id}/report", headers=owner)
        ).json()
        timeline = (
            await client.get(f"/api/exercises/{exercise.id}/timeline", headers=owner)
        ).json()
        csv_response = await client.get(
            f"/api/exercises/{exercise.id}/export.csv", headers=owner
        )
        csv_rows = list(csv.DictReader(io.StringIO(csv_response.text)))
        assert csv_rows
        assert len({row["inject_state"] for row in csv_rows}) == 1
        csv_row = csv_rows[0]
        export_row = next(row for row in exported["injects"] if row["id"] == inject_id)
        report_row = next(
            row for row in report["injects"] if row["scenario_node_id"] == "all_hands"
        )
        return export_row, report_row, timeline, csv_row

    await submit(unassigned, "unassigned complete")
    export_row, report_row, timeline, csv_row = await projections()
    assert export_row["state"] == "released"
    assert csv_row["inject_state"] == "released"
    expected_partial = {None: "resolved", "legal": "released"}
    assert {
        row["group_id"]: row["state"] for row in export_row["resolutions"]
    } == expected_partial
    assert {
        row["group_id"]: row["state"] for row in report_row["resolutions"]
    } == expected_partial
    assert {
        event["group_id"]
        for event in timeline
        if event["kind"] == "inject_resolved"
    } == {None}
    assert {
        row["group_id"]: row["state"]
        for row in json.loads(csv_row["inject_resolutions"])
    } == expected_partial
    late_enrolment = await client.post(
        f"/api/exercises/{exercise.id}/members",
        json={"user_id": late_participant.id, "group_id": "legal"},
        headers=owner,
    )
    assert late_enrolment.status_code == 409
    assert late_enrolment.json()["detail"] == (
        "Roster changes are locked after the first inject is released"
    )

    await submit(legal, "legal complete")
    export_row, report_row, timeline, csv_row = await projections()
    expected = {None, "legal"}
    assert export_row["state"] == "resolved"
    assert csv_row["inject_state"] == "resolved"
    assert {row["group_id"] for row in export_row["resolutions"]} == expected
    assert {row["group_id"] for row in report_row["resolutions"]} == expected
    assert {
        event["group_id"]
        for event in timeline
        if event["kind"] == "inject_resolved"
    } == expected
    group_change = await client.patch(
        f"/api/exercises/{exercise.id}/members/{legal.id}",
        json={"group_id": None},
        headers=owner,
    )
    assert group_change.status_code == 409
