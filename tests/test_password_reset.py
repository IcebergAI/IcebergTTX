"""Self-service password reset over SMTP (#117).

The mailer is faked and SMTP is toggled on via monkeypatch, so the suite passes with
no SMTP configured (the default). Covers: feature-off 404, no account enumeration,
single-use + expiring hashed tokens, session revocation, SSO branch, rate limiting,
and that the raw token never reaches the audit log.
"""

import asyncio
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.auth_token import AuthToken, AuthTokenPurpose
from app.models.user import User, UserRole
from app.services import audit_service, mail_service, token_service


@pytest.fixture(autouse=True)
def _reset_reset_limiter():
    from app.services.rate_limit import password_reset_rate_limiter

    password_reset_rate_limiter.clear()
    yield
    password_reset_rate_limiter.clear()


@pytest.fixture(name="smtp_on")
def smtp_on_fixture(monkeypatch):
    """Enable the email feature (smtp_enabled reads host+from)."""
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_from", "noreply@test")


@pytest.fixture(name="mail")
def mail_fixture(monkeypatch):
    """Capture outbound mail as (to, subject, body). send() is fired via spawn, so
    tests await a tick before asserting."""
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to, subject, body):
        sent.append((to, subject, body))

    monkeypatch.setattr(mail_service, "send", fake_send)
    return sent


async def _tick():
    # Let a spawned mail task run.
    await asyncio.sleep(0.05)


async def _token_rows(session, email):
    return (await session.exec(select(AuthToken).where(AuthToken.email == email))).all()


# ── Feature-off ───────────────────────────────────────────────────────────────


async def test_request_404_without_smtp(client: AsyncClient):
    r = await client.post("/api/auth/password-reset/request", json={"email": "a@b.com"})
    assert r.status_code == 404


async def test_complete_404_without_smtp(client: AsyncClient):
    r = await client.post(
        "/api/auth/password-reset/complete", json={"token": "x", "password": "newpassword123"}
    )
    assert r.status_code == 404


# ── Request: no enumeration ────────────────────────────────────────────────────


async def test_request_unknown_email_200_no_token_no_mail(
    client: AsyncClient, session: AsyncSession, smtp_on, mail
):
    r = await client.post(
        "/api/auth/password-reset/request", json={"email": "ghost@example.com"}
    )
    assert r.status_code == 200
    await _tick()
    assert await _token_rows(session, "ghost@example.com") == []
    assert mail == []


async def test_request_local_account_sends_link_and_mints_token(
    client: AsyncClient, session: AsyncSession, facilitator: User, smtp_on, mail
):
    r = await client.post(
        "/api/auth/password-reset/request", json={"email": facilitator.email}
    )
    assert r.status_code == 200
    await _tick()
    rows = await _token_rows(session, facilitator.email)
    assert len(rows) == 1
    assert rows[0].purpose == AuthTokenPurpose.password_reset
    assert rows[0].user_id == facilitator.id
    assert len(mail) == 1
    to, subject, body = mail[0]
    assert to == facilitator.email
    assert "reset" in subject.lower()
    assert "/reset-password?token=" in body


async def test_request_sso_account_sends_notice_no_token(
    client: AsyncClient, session: AsyncSession, smtp_on, mail
):
    sso = User(
        email="sso@example.com",
        display_name="SSO User",
        auth_provider="entra",
        subject="sub-123",
        hashed_password=None,
        role=UserRole.participant,
    )
    session.add(sso)
    await session.commit()

    r = await client.post("/api/auth/password-reset/request", json={"email": sso.email})
    assert r.status_code == 200
    await _tick()
    assert await _token_rows(session, sso.email) == []  # no reset token for SSO
    assert len(mail) == 1
    assert "single sign-on" in mail[0][2].lower()


async def test_request_rate_limited(client: AsyncClient, smtp_on, mail):
    for _ in range(settings.password_reset_max_attempts):
        r = await client.post(
            "/api/auth/password-reset/request", json={"email": "loop@example.com"}
        )
        assert r.status_code == 200
    r = await client.post(
        "/api/auth/password-reset/request", json={"email": "loop@example.com"}
    )
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


async def test_request_never_logs_token(
    client: AsyncClient, facilitator: User, smtp_on, mail, monkeypatch
):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(audit_service, "emit", lambda action, **kw: events.append((action, kw)))
    await client.post("/api/auth/password-reset/request", json={"email": facilitator.email})
    await _tick()
    # The raw token only exists in the emailed link.
    raw = mail[0][2].split("token=", 1)[1].split()[0]
    blob = repr(events)
    assert raw not in blob
    assert any(a == "auth.password_reset_request" for a, _ in events)


# ── Complete ───────────────────────────────────────────────────────────────────


async def _mint(session, user, *, ttl=timedelta(hours=1)):
    return await token_service.create(
        session,
        purpose=AuthTokenPurpose.password_reset,
        email=user.email,
        user_id=user.id,
        ttl=ttl,
    )


async def test_complete_sets_password_and_logs_in(
    client: AsyncClient, session: AsyncSession, participant: User, smtp_on
):
    raw = await _mint(session, participant)
    r = await client.post(
        "/api/auth/password-reset/complete",
        json={"token": raw, "password": "brandnewpass99"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]
    # New password works, old one fails.
    ok = await client.post(
        "/api/auth/login", json={"email": participant.email, "password": "brandnewpass99"}
    )
    assert ok.status_code == 200
    bad = await client.post(
        "/api/auth/login", json={"email": participant.email, "password": "password1234"}
    )
    assert bad.status_code == 401


async def test_complete_is_single_use(
    client: AsyncClient, session: AsyncSession, participant: User, smtp_on
):
    raw = await _mint(session, participant)
    first = await client.post(
        "/api/auth/password-reset/complete",
        json={"token": raw, "password": "brandnewpass99"},
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/auth/password-reset/complete",
        json={"token": raw, "password": "anotherpass123"},
    )
    assert second.status_code == 400


async def test_complete_expired_token_400(
    client: AsyncClient, session: AsyncSession, participant: User, smtp_on
):
    raw = await _mint(session, participant, ttl=timedelta(seconds=-1))
    r = await client.post(
        "/api/auth/password-reset/complete",
        json={"token": raw, "password": "brandnewpass99"},
    )
    assert r.status_code == 400


async def test_complete_invalid_token_400(client: AsyncClient, smtp_on):
    r = await client.post(
        "/api/auth/password-reset/complete",
        json={"token": "not-a-real-token", "password": "brandnewpass99"},
    )
    assert r.status_code == 400


async def test_complete_revokes_existing_sessions(
    client: AsyncClient, session: AsyncSession, participant: User, smtp_on
):
    raw = await _mint(session, participant)
    r = await client.post(
        "/api/auth/password-reset/complete",
        json={"token": raw, "password": "brandnewpass99"},
    )
    assert r.status_code == 200
    await session.refresh(participant)
    # token_valid_after is armed → dependencies.py rejects any earlier-issued token (#14).
    assert participant.token_valid_after is not None


async def test_complete_weak_password_422(
    client: AsyncClient, session: AsyncSession, participant: User, smtp_on
):
    raw = await _mint(session, participant)
    r = await client.post(
        "/api/auth/password-reset/complete", json={"token": raw, "password": "short"}
    )
    assert r.status_code == 422


# ── token_service unit ─────────────────────────────────────────────────────────


def test_generate_hashes_not_raw():
    raw, token_hash = token_service.generate()
    assert raw != token_hash
    assert len(token_hash) == 64  # sha256 hex
    assert token_service._hash(raw) == token_hash


async def test_consume_wrong_purpose_returns_none(
    session: AsyncSession, participant: User
):
    raw = await _mint(session, participant)
    assert (
        await token_service.consume(session, raw=raw, purpose=AuthTokenPurpose.invite)
    ) is None
    # A rejected consume must not burn the token.
    rows = await _token_rows(session, participant.email)
    assert rows[0].used_at is None
