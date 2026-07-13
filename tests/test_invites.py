"""Participant invites over SMTP (#117).

Mailer faked + SMTP toggled on via monkeypatch, so the suite passes without SMTP.
Covers: admin-only + feature-off gating, pre-bound email/team/exercise, single-use
tokens, working while open registration is disabled, and auto-enrolment on accept.
"""

import asyncio
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.auth_token import AuthToken, AuthTokenPurpose
from app.models.exercise import Exercise, ExerciseMember
from app.models.user import User
from app.services import audit_service, mail_service, token_service

AUTH = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731


@pytest.fixture(name="smtp_on")
def smtp_on_fixture(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@test")


@pytest.fixture(name="mail")
def mail_fixture(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to, subject, body):
        sent.append((to, subject, body))

    monkeypatch.setattr(mail_service, "send", fake_send)
    return sent


async def _tick():
    await asyncio.sleep(0.05)


async def _invite_token(session, email, *, team=None, exercise_id=None):
    return await token_service.create(
        session,
        purpose=AuthTokenPurpose.invite,
        email=email,
        team=team,
        exercise_id=exercise_id,
        ttl=timedelta(days=7),
    )


# ── Send: gating ────────────────────────────────────────────────────────────


async def test_invite_404_without_smtp(client: AsyncClient, admin_token: str):
    r = await client.post(
        "/api/users/invite", json={"email": "new@example.com"}, headers=AUTH(admin_token)
    )
    assert r.status_code == 404


async def test_invite_requires_admin(
    client: AsyncClient, facilitator_token: str, smtp_on, mail
):
    r = await client.post(
        "/api/users/invite", json={"email": "new@example.com"}, headers=AUTH(facilitator_token)
    )
    assert r.status_code == 403


async def test_invite_existing_email_409(
    client: AsyncClient, admin_token: str, participant: User, smtp_on, mail
):
    r = await client.post(
        "/api/users/invite", json={"email": participant.email}, headers=AUTH(admin_token)
    )
    assert r.status_code == 409


async def test_invite_unknown_exercise_404(
    client: AsyncClient, admin_token: str, smtp_on, mail
):
    r = await client.post(
        "/api/users/invite",
        json={"email": "new@example.com", "exercise_id": 999999},
        headers=AUTH(admin_token),
    )
    assert r.status_code == 404


# ── Send: happy path ──────────────────────────────────────────────────────────


async def test_invite_sends_link_and_mints_token(
    client: AsyncClient, session: AsyncSession, admin_token: str, active_exercise, smtp_on, mail
):
    r = await client.post(
        "/api/users/invite",
        json={"email": "invitee@example.com", "team": "it_ops", "exercise_id": active_exercise.id},
        headers=AUTH(admin_token),
    )
    assert r.status_code == 200
    await _tick()
    row = (
        await session.exec(select(AuthToken).where(AuthToken.email == "invitee@example.com"))
    ).first()
    assert row is not None
    assert row.purpose == AuthTokenPurpose.invite
    assert row.team == "it_ops"
    assert row.exercise_id == active_exercise.id
    assert row.user_id is None  # no account yet
    assert len(mail) == 1
    to, _subject, body = mail[0]
    assert to == "invitee@example.com"
    assert "/accept-invite?token=" in body


async def test_invite_works_with_registration_disabled(
    client: AsyncClient, admin_token: str, smtp_on, mail, monkeypatch
):
    monkeypatch.setattr(settings, "registration_enabled", False)
    r = await client.post(
        "/api/users/invite", json={"email": "invitee@example.com"}, headers=AUTH(admin_token)
    )
    assert r.status_code == 200


async def test_invite_never_logs_token(
    client: AsyncClient, admin_token: str, smtp_on, mail, monkeypatch
):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(audit_service, "emit", lambda action, **kw: events.append((action, kw)))
    await client.post(
        "/api/users/invite", json={"email": "invitee@example.com"}, headers=AUTH(admin_token)
    )
    await _tick()
    raw = mail[0][2].split("token=", 1)[1].split()[0]
    assert raw not in repr(events)
    assert any(a == "auth.invite" for a, _ in events)


# ── Accept ────────────────────────────────────────────────────────────────────


async def test_accept_404_without_smtp(client: AsyncClient):
    r = await client.post(
        "/api/auth/invite/accept",
        json={"token": "x", "display_name": "X", "password": "password12345"},
    )
    assert r.status_code == 404


async def test_accept_creates_participant_and_logs_in(
    client: AsyncClient, session: AsyncSession, smtp_on
):
    raw = await _invite_token(session, "invitee@example.com", team="it_ops")
    r = await client.post(
        "/api/auth/invite/accept",
        json={"token": raw, "display_name": "New Person", "password": "welcomepass12"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]
    created = (
        await session.exec(select(User).where(User.email == "invitee@example.com"))
    ).first()
    assert created is not None
    assert created.role.value == "participant"
    assert created.team == "it_ops"
    # New account can log in with the chosen password.
    ok = await client.post(
        "/api/auth/login", json={"email": "invitee@example.com", "password": "welcomepass12"}
    )
    assert ok.status_code == 200


async def test_accept_enrols_in_bound_exercise(
    client: AsyncClient, session: AsyncSession, active_exercise: Exercise, smtp_on
):
    raw = await _invite_token(
        session, "enrolled@example.com", team="it_ops", exercise_id=active_exercise.id
    )
    r = await client.post(
        "/api/auth/invite/accept",
        json={"token": raw, "display_name": "Enrolled", "password": "welcomepass12"},
    )
    assert r.status_code == 200
    user = (
        await session.exec(select(User).where(User.email == "enrolled@example.com"))
    ).first()
    member = (
        await session.exec(
            select(ExerciseMember)
            .where(ExerciseMember.exercise_id == active_exercise.id)
            .where(ExerciseMember.user_id == user.id)
        )
    ).first()
    assert member is not None


async def test_accept_bound_invite_after_release_preserves_token_without_account(
    client: AsyncClient,
    session: AsyncSession,
    active_exercise: Exercise,
    facilitator_token: str,
    smtp_on,
):
    email = "locked-roster-invite@example.com"
    exercise_id = active_exercise.id
    raw = await _invite_token(
        session, email, team="it_ops", exercise_id=exercise_id
    )
    injects = (
        await client.get(
            f"/api/exercises/{exercise_id}/injects",
            headers=AUTH(facilitator_token),
        )
    ).json()
    start = next(inject for inject in injects if inject["state"] == "pending")
    released = await client.post(
        f"/api/exercises/{exercise_id}/injects/{start['id']}/release",
        headers=AUTH(facilitator_token),
    )
    assert released.status_code == 200

    payload = {
        "token": raw,
        "display_name": "Locked Invitee",
        "password": "welcomepass12",
    }
    rejected = await client.post("/api/auth/invite/accept", json=payload)
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == (
        "Roster changes are locked after the first inject is released"
    )

    token = (
        await session.exec(select(AuthToken).where(AuthToken.email == email))
    ).one()
    assert token.used_at is None
    assert (await session.exec(select(User).where(User.email == email))).first() is None

    # A retry still reaches the roster policy rather than reporting a burned token.
    retry = await client.post("/api/auth/invite/accept", json=payload)
    assert retry.status_code == 409


async def test_accept_is_single_use(client: AsyncClient, session: AsyncSession, smtp_on):
    raw = await _invite_token(session, "once@example.com")
    first = await client.post(
        "/api/auth/invite/accept",
        json={"token": raw, "display_name": "Once", "password": "welcomepass12"},
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/auth/invite/accept",
        json={"token": raw, "display_name": "Again", "password": "welcomepass12"},
    )
    assert second.status_code == 400


async def test_accept_invalid_token_400(client: AsyncClient, smtp_on):
    r = await client.post(
        "/api/auth/invite/accept",
        json={"token": "bogus", "display_name": "X", "password": "welcomepass12"},
    )
    assert r.status_code == 400


async def test_accept_email_already_registered_409(
    client: AsyncClient, session: AsyncSession, participant: User, smtp_on
):
    # A token minted for an email that becomes a real account before acceptance.
    raw = await _invite_token(session, participant.email)
    r = await client.post(
        "/api/auth/invite/accept",
        json={"token": raw, "display_name": "Dup", "password": "welcomepass12"},
    )
    assert r.status_code == 409
