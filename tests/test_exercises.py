import json
import logging
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise, ExerciseState
from app.models.inject import Inject
from app.models.user import User
from app.schemas.scenario_json import InjectNode, ScenarioDefinition
from app.services.exercise_service import transition_state


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
