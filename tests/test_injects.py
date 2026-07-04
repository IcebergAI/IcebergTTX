from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.exercise import Exercise
from app.models.user import User

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _create_inject(
    client: AsyncClient,
    token: str,
    exercise_id: int,
    title: str = "Test Inject",
    content: str = "What do you do?",
    target_teams: list[str] | None = None,
    sequence_order: int = 0,
):
    body = {"title": title, "content": content, "sequence_order": sequence_order}
    if target_teams is not None:
        body["target_teams"] = target_teams
    return await client.post(
        f"/api/exercises/{exercise_id}/injects",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def test_create_inject(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    r = (await _create_inject(client, facilitator_token, active_exercise.id))
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Test Inject"
    assert data["state"] == "pending"
    assert data["target_teams"] is None


async def test_create_inject_with_teams(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    r = (await _create_inject(
        client, facilitator_token, active_exercise.id, target_teams=["it_ops", "legal"]
    ))
    assert r.status_code == 201
    assert r.json()["target_teams"] == ["it_ops", "legal"]


async def test_create_inject_with_attachment(
    client: AsyncClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    content = b"attached brief"
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        data={"title": "Attached", "content": "Read the file", "sequence_order": "5"},
        files={"attachment": ("brief.txt", content, "text/plain")},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )

    assert r.status_code == 201
    data = r.json()
    assert data["attachment"]["filename"] == "brief.txt"
    assert data["attachment"]["content_type"] == "text/plain"
    assert data["attachment"]["size"] == len(content)

    pending_download = await client.get(
        data["attachment"]["url"],
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert pending_download.status_code == 404

    await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{data['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    download = await client.get(
        data["attachment"]["url"],
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert download.status_code == 200
    assert download.content == content
    assert "brief.txt" in download.headers["content-disposition"]


async def test_create_inject_attachment_too_large(
    client: AsyncClient,
    facilitator_token: str,
    active_exercise: Exercise,
    monkeypatch,
):
    """An oversized upload is rejected mid-stream (413) and leaves no residual file (#39)."""
    from app.routers import injects as injects_router

    monkeypatch.setattr(injects_router, "MAX_ATTACHMENT_BYTES", 16)
    monkeypatch.setattr(injects_router, "ATTACHMENT_CHUNK_BYTES", 8)
    storage_dir = injects_router.ATTACHMENT_ROOT / str(active_exercise.id)
    before = set(storage_dir.glob("*")) if storage_dir.exists() else set()

    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects",
        data={"title": "Big", "content": "too big"},
        files={"attachment": ("big.bin", b"x" * 64, "application/octet-stream")},
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 413

    after = set(storage_dir.glob("*")) if storage_dir.exists() else set()
    assert after == before, "partial oversized upload should be cleaned up"


async def test_create_inject_participant_forbidden(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = (await _create_inject(client, participant_token, active_exercise.id))
    assert r.status_code == 403


async def test_list_injects(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    # Exercise is pre-seeded from the scenario; add two more at higher sequence_order
    await _create_inject(
        client, facilitator_token, active_exercise.id, title="A", sequence_order=10
    )
    await _create_inject(
        client, facilitator_token, active_exercise.id, title="B", sequence_order=11
    )
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    titles = [i["title"] for i in r.json()]
    # Verify A comes before B and both are present
    assert "A" in titles and "B" in titles
    assert titles.index("A") < titles.index("B")


async def test_list_injects_participant_allowed(
    client: AsyncClient, participant_token: str, active_exercise: Exercise
):
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    assert r.json() == []


async def test_participant_sees_only_released_visible_injects(
    client: AsyncClient, participant_token: str, facilitator_token: str, active_exercise: Exercise
):
    injects = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    it_ops_inject = next(i for i in injects if i["scenario_node_id"] == "inject_01")
    legal_inject = next(i for i in injects if i["scenario_node_id"] == "inject_02")

    await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{it_ops_inject['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{legal_inject['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )

    r = await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert [i["scenario_node_id"] for i in payload] == ["inject_01"]
    assert payload[0]["options"][0]["id"] == "opt_a"
    assert payload[0]["group_id"] == "it_ops"


async def test_facilitator_preview_participant_uses_preview_team_for_visibility(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    injects = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    it_ops_inject = next(i for i in injects if i["scenario_node_id"] == "inject_01")
    legal_inject = next(i for i in injects if i["scenario_node_id"] == "inject_02")

    for inject in (it_ops_inject, legal_inject):
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject['id']}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )

    client.cookies.set("dt_view_role", "participant")
    client.cookies.set("dt_view_team", "it_ops")
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )

    assert r.status_code == 200
    payload = r.json()
    assert [i["scenario_node_id"] for i in payload] == ["inject_01"]
    assert payload[0]["group_id"] == "it_ops"


async def test_different_groups_see_different_released_injects(
    client: AsyncClient,
    participant_token: str,
    facilitator_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
):
    from app.models.user import UserRole
    from app.services.auth_service import create_access_token, hash_password
    from app.services.exercise_service import enrol_member

    legal = User(
        email="legal-group@example.com",
        display_name="Legal Group",
        hashed_password=hash_password("pw"),
        role=UserRole.participant,
        team="legal",
    )
    session.add(legal)
    await session.commit()
    await session.refresh(legal)
    await enrol_member(session, exercise=active_exercise, user_id=legal.id)
    legal_token = create_access_token(subject=legal.email, role=legal.role.value)

    injects = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )).json()
    it_ops_inject = next(
        i for i in injects if i["scenario_node_id"] == "inject_01" and i["group_id"] == "it_ops"
    )
    legal_inject = next(
        i for i in injects if i["scenario_node_id"] == "inject_02" and i["group_id"] == "legal"
    )

    for inject in (it_ops_inject, legal_inject):
        await client.post(
            f"/api/exercises/{active_exercise.id}/injects/{inject['id']}/release",
            headers={"Authorization": f"Bearer {facilitator_token}"},
        )

    it_ops_payload = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {participant_token}"},
    )).json()
    legal_payload = (await client.get(
        f"/api/exercises/{active_exercise.id}/injects",
        headers={"Authorization": f"Bearer {legal_token}"},
    )).json()

    assert [i["group_id"] for i in it_ops_payload] == ["it_ops"]
    assert [i["group_id"] for i in legal_payload] == ["legal"]


async def test_get_inject(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    created = (await _create_inject(client, facilitator_token, active_exercise.id)).json()
    r = await client.get(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]


async def test_get_inject_wrong_exercise(
    client: AsyncClient,
    facilitator_token: str,
    active_exercise: Exercise,
    session: AsyncSession,
    facilitator: User,
    sample_scenario,
):
    from app.services.exercise_service import create_exercise

    other = await create_exercise(
        session,
        scenario_id=sample_scenario.id,
        title="Other",
        created_by=facilitator.id,
    )
    created = (await _create_inject(client, facilitator_token, active_exercise.id)).json()
    r = await client.get(
        f"/api/exercises/{other.id}/injects/{created['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 404


async def test_delete_inject(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    created = (await _create_inject(client, facilitator_token, active_exercise.id)).json()
    r = await client.delete(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 204


# ── Release ───────────────────────────────────────────────────────────────────

async def test_release_inject(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    created = (await _create_inject(client, facilitator_token, active_exercise.id)).json()
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "released"
    assert data["released_at"] is not None
    assert data["released_by"] is not None


async def test_release_already_released(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    created = (await _create_inject(client, facilitator_token, active_exercise.id)).json()
    await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 409


async def test_release_inject_participant_forbidden(
    client: AsyncClient, participant_token: str, facilitator_token: str, active_exercise: Exercise
):
    created = (await _create_inject(client, facilitator_token, active_exercise.id)).json()
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert r.status_code == 403


async def test_release_broadcast_all(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    """Broadcast inject (no target_teams) is released to all."""
    created = (await _create_inject(
        client, facilitator_token, active_exercise.id, target_teams=None
    )).json()
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["target_teams"] is None


async def test_release_broadcast_team_targeted(
    client: AsyncClient, facilitator_token: str, active_exercise: Exercise
):
    """Team-targeted inject is released only to named teams."""
    created = (await _create_inject(
        client, facilitator_token, active_exercise.id, target_teams=["it_ops"]
    )).json()
    r = await client.post(
        f"/api/exercises/{active_exercise.id}/injects/{created['id']}/release",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    )
    assert r.status_code == 200
    assert r.json()["target_teams"] == ["it_ops"]
