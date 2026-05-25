from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.exercise import Exercise
from app.models.user import User, UserRole


def _release_node(
    client: TestClient,
    token: str,
    exercise_id: int,
    scenario_node_id: str = "inject_01",
) -> dict:
    injects = client.get(
        f"/api/exercises/{exercise_id}/injects",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    inject = next(i for i in injects if i["scenario_node_id"] == scenario_node_id)
    return client.post(
        f"/api/exercises/{exercise_id}/injects/{inject['id']}/release",
        headers={"Authorization": f"Bearer {token}"},
    ).json()


def _comment(
    client: TestClient,
    token: str,
    exercise_id: int,
    inject_id: int,
    content: str,
):
    return client.post(
        f"/api/exercises/{exercise_id}/inject-comments",
        json={"inject_id": inject_id, "content": content},
        headers={"Authorization": f"Bearer {token}"},
    )


def _participant(
    session: Session,
    *,
    email: str,
    name: str,
    team: str,
) -> User:
    from app.services.auth_service import hash_password

    user = User(
        email=email,
        display_name=name,
        hashed_password=hash_password("pw"),
        role=UserRole.participant,
        team=team,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _token(user: User) -> str:
    from app.services.auth_service import create_access_token

    return create_access_token(subject=user.email, role=user.role.value)


def test_team_members_can_comment_on_same_released_inject(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: Session,
):
    from app.services.exercise_service import enrol_member

    other = _participant(
        session,
        email="second-it@example.com",
        name="Second IT",
        team="it_ops",
    )
    enrol_member(session, exercise=active_exercise, user_id=other.id)
    other_token = _token(other)
    inject = _release_node(client, facilitator_token, active_exercise.id)

    first = _comment(
        client,
        participant_token,
        active_exercise.id,
        inject["id"],
        " First analyst note. ",
    )
    second = _comment(
        client,
        other_token,
        active_exercise.id,
        inject["id"],
        "Second analyst note.",
    )

    assert first.status_code == 201
    assert first.json()["content"] == "First analyst note."
    assert second.status_code == 201

    visible = client.get(
        f"/api/exercises/{active_exercise.id}/inject-comments",
        headers={"Authorization": f"Bearer {participant_token}"},
    )
    assert visible.status_code == 200
    assert [c["content"] for c in visible.json()] == [
        "First analyst note.",
        "Second analyst note.",
    ]
    assert {c["group_id"] for c in visible.json()} == {"it_ops"}


def test_team_comments_are_scoped_to_the_commenters_team(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
    session: Session,
):
    from app.services.exercise_service import enrol_member

    legal = _participant(
        session,
        email="legal-commenter@example.com",
        name="Legal Commenter",
        team="legal",
    )
    enrol_member(session, exercise=active_exercise, user_id=legal.id, group_id="legal")
    legal_token = _token(legal)

    it_inject = _release_node(client, facilitator_token, active_exercise.id, "inject_01")
    legal_inject = _release_node(client, facilitator_token, active_exercise.id, "inject_02")

    assert _comment(
        client,
        participant_token,
        active_exercise.id,
        it_inject["id"],
        "IT team note.",
    ).status_code == 201
    assert _comment(
        client,
        legal_token,
        active_exercise.id,
        legal_inject["id"],
        "Legal team note.",
    ).status_code == 201

    it_rows = client.get(
        f"/api/exercises/{active_exercise.id}/inject-comments",
        headers={"Authorization": f"Bearer {participant_token}"},
    ).json()
    legal_rows = client.get(
        f"/api/exercises/{active_exercise.id}/inject-comments",
        headers={"Authorization": f"Bearer {legal_token}"},
    ).json()
    facilitator_rows = client.get(
        f"/api/exercises/{active_exercise.id}/inject-comments",
        headers={"Authorization": f"Bearer {facilitator_token}"},
    ).json()

    assert [c["content"] for c in it_rows] == ["IT team note."]
    assert [c["content"] for c in legal_rows] == ["Legal team note."]
    assert {c["content"] for c in facilitator_rows} == {
        "IT team note.",
        "Legal team note.",
    }


def test_comment_on_wrong_team_inject_is_forbidden(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    legal_inject = _release_node(client, facilitator_token, active_exercise.id, "inject_02")

    response = _comment(
        client,
        participant_token,
        active_exercise.id,
        legal_inject["id"],
        "Can I see this?",
    )

    assert response.status_code == 404


def test_blank_comment_is_rejected(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    inject = _release_node(client, facilitator_token, active_exercise.id)

    response = _comment(client, participant_token, active_exercise.id, inject["id"], "   ")

    assert response.status_code == 422


def test_ws_broadcasts_inject_comment_to_facilitator(
    client: TestClient,
    facilitator_token: str,
    participant_token: str,
    active_exercise: Exercise,
):
    inject = _release_node(client, facilitator_token, active_exercise.id)

    with client.websocket_connect(
        f"/ws/exercises/{active_exercise.id}?token={facilitator_token}"
    ) as ws:
        _comment(
            client,
            participant_token,
            active_exercise.id,
            inject["id"],
            "Live comment.",
        )
        message = ws.receive_json()

    assert message["type"] == "inject_comment_created"
    assert message["payload"]["inject_id"] == inject["id"]
    assert message["payload"]["content"] == "Live comment."
