"""Audit + auth-token retention sweep tests (#251)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.audit import AuditEvent
from app.models.auth_token import AuthToken, AuthTokenPurpose
from app.models.user import User
from app.services import audit_settings_service, retention_service, token_service


class _CtxSession:
    """Async-context wrapper so the sweep reuses the test's transactional session
    (an independent AsyncSession(engine) would not see the test's uncommitted rows)."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


async def _event(session: AsyncSession, *, age_days: float) -> AuditEvent:
    row = AuditEvent(action="auth.login", created_at=NOW - timedelta(days=age_days))
    session.add(row)
    await session.flush()
    return row


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.exec(select(func.count()).select_from(model))).one()


async def _mint(
    session: AsyncSession, user: User, *, ttl: timedelta = timedelta(hours=1)
) -> str:
    return await token_service.create(
        session,
        purpose=AuthTokenPurpose.password_reset,
        email=user.email,
        user_id=user.id,
        ttl=ttl,
    )


async def _token_row(session: AsyncSession, raw: str) -> AuthToken | None:
    return (
        await session.exec(
            select(AuthToken).where(AuthToken.token_hash == token_service._hash(raw))
        )
    ).first()


# --------------------------------------------------------------------------- #
# purge_audit_events
# --------------------------------------------------------------------------- #


async def test_retention_zero_keeps_everything(session: AsyncSession):
    """The headline safety property: an upgrade must destroy nothing by default."""
    for age in (1, 400, 2000):
        await _event(session, age_days=age)
    before = await _count(session, AuditEvent)

    deleted = await retention_service.purge_audit_events(session, retention_days=0, now=NOW)

    assert deleted == 0
    assert await _count(session, AuditEvent) == before


async def test_negative_retention_is_treated_as_disabled(session: AsyncSession):
    """A hand-edited negative row would otherwise compute a cutoff in the future."""
    await _event(session, age_days=1)
    before = await _count(session, AuditEvent)

    deleted = await retention_service.purge_audit_events(session, retention_days=-1, now=NOW)

    assert deleted == 0
    assert await _count(session, AuditEvent) == before


async def test_purge_deletes_only_events_older_than_cutoff(session: AsyncSession):
    # Capture ids up front: the purge's commits expire these instances, and touching
    # an expired attribute afterwards would trigger a sync lazy-load (MissingGreenlet).
    old_id = (await _event(session, age_days=400)).id
    recent_id = (await _event(session, age_days=10)).id

    deleted = await retention_service.purge_audit_events(session, retention_days=90, now=NOW)

    assert deleted == 1
    session.expire_all()
    assert await session.get(AuditEvent, old_id) is None
    assert await session.get(AuditEvent, recent_id) is not None


async def test_purge_boundary_row_at_cutoff_survives(session: AsyncSession):
    """The cutoff is strict `<`, so a row landing exactly on it is kept."""
    boundary_id = (await _event(session, age_days=90)).id

    deleted = await retention_service.purge_audit_events(session, retention_days=90, now=NOW)

    assert deleted == 0
    session.expire_all()
    assert await session.get(AuditEvent, boundary_id) is not None


async def test_purge_batches_across_multiple_transactions(
    session: AsyncSession, monkeypatch
):
    """Proves the batch loop iterates rather than stopping after the first pass."""
    monkeypatch.setattr(retention_service, "_PURGE_BATCH", 2)
    stale_ids = [(await _event(session, age_days=400)).id for _ in range(5)]

    deleted = await retention_service.purge_audit_events(session, retention_days=90, now=NOW)

    assert deleted == 5
    session.expire_all()
    for row_id in stale_ids:
        assert await session.get(AuditEvent, row_id) is None


# --------------------------------------------------------------------------- #
# purge_auth_tokens
# --------------------------------------------------------------------------- #


async def test_expired_unused_token_purged_after_grace(
    session: AsyncSession, participant: User
):
    raw = await _mint(session, participant, ttl=timedelta(seconds=-1))
    row = await _token_row(session, raw)
    assert row is not None
    row.expires_at = NOW - retention_service._TOKEN_EXPIRED_GRACE - timedelta(hours=1)
    session.add(row)
    await session.flush()

    assert await retention_service.purge_auth_tokens(session, now=NOW) == 1
    session.expire_all()
    assert await _token_row(session, raw) is None


async def test_expired_token_within_grace_survives(
    session: AsyncSession, participant: User
):
    """The grace is real, not decorative — a just-expired link stays diagnosable."""
    raw = await _mint(session, participant, ttl=timedelta(seconds=-1))
    row = await _token_row(session, raw)
    assert row is not None
    row.expires_at = NOW - timedelta(hours=1)
    session.add(row)
    await session.flush()

    assert await retention_service.purge_auth_tokens(session, now=NOW) == 0
    session.expire_all()
    assert await _token_row(session, raw) is not None


async def test_used_token_purged_after_short_grace(
    session: AsyncSession, participant: User
):
    """A burnt token goes on its own grace even with expires_at still in the future."""
    raw = await _mint(session, participant, ttl=timedelta(days=7))
    consumed = await token_service.consume(
        session, raw=raw, purpose=AuthTokenPurpose.password_reset, commit=False
    )
    assert consumed is not None
    consumed.used_at = NOW - retention_service._TOKEN_USED_GRACE - timedelta(hours=1)
    consumed.expires_at = NOW + timedelta(days=6)
    session.add(consumed)
    await session.flush()

    assert await retention_service.purge_auth_tokens(session, now=NOW) == 1
    session.expire_all()
    assert await _token_row(session, raw) is None


async def test_used_token_within_grace_survives(session: AsyncSession, participant: User):
    raw = await _mint(session, participant, ttl=timedelta(days=7))
    consumed = await token_service.consume(
        session, raw=raw, purpose=AuthTokenPurpose.password_reset, commit=False
    )
    assert consumed is not None
    consumed.used_at = NOW - timedelta(hours=1)
    consumed.expires_at = NOW + timedelta(days=6)
    session.add(consumed)
    await session.flush()

    assert await retention_service.purge_auth_tokens(session, now=NOW) == 0
    session.expire_all()
    assert await _token_row(session, raw) is not None


async def test_live_token_never_purged(session: AsyncSession, participant: User):
    raw = await _mint(session, participant, ttl=timedelta(hours=1))

    assert await retention_service.purge_auth_tokens(session) == 0
    session.expire_all()
    assert (
        await token_service.consume(
            session, raw=raw, purpose=AuthTokenPurpose.password_reset, commit=False
        )
        is not None
    )


# --------------------------------------------------------------------------- #
# sweep_once
# --------------------------------------------------------------------------- #


@pytest.fixture(name="sweep_session")
def sweep_session_fixture(session: AsyncSession, monkeypatch) -> AsyncSession:
    """Point the sweep's own AsyncSession at this test's transactional session."""
    monkeypatch.setattr(
        retention_service, "AsyncSession", lambda *a, **k: _CtxSession(session)
    )
    return session


async def test_sweep_reads_retention_from_audit_settings(sweep_session: AsyncSession):
    await audit_settings_service.update_settings(sweep_session, {"retention_days": 90})
    old = AuditEvent(action="auth.login", created_at=datetime.now(UTC) - timedelta(days=400))
    sweep_session.add(old)
    await sweep_session.flush()
    old_id = old.id

    events, _ = await retention_service.sweep_once()

    assert events == 1
    sweep_session.expire_all()
    assert await sweep_session.get(AuditEvent, old_id) is None


async def test_token_purge_runs_with_retention_disabled(
    sweep_session: AsyncSession, participant: User
):
    """Token hygiene is unconditional — it does not wait on an opt-in audit window."""
    await audit_settings_service.update_settings(sweep_session, {"retention_days": 0})
    old = AuditEvent(action="auth.login", created_at=datetime.now(UTC) - timedelta(days=4000))
    sweep_session.add(old)
    raw = await _mint(sweep_session, participant, ttl=timedelta(seconds=-1))
    row = await _token_row(sweep_session, raw)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(days=30)
    sweep_session.add(row)
    await sweep_session.flush()
    old_id = old.id

    events, tokens = await retention_service.sweep_once()

    assert (events, tokens) == (0, 1)
    sweep_session.expire_all()
    assert await sweep_session.get(AuditEvent, old_id) is not None
    assert await _token_row(sweep_session, raw) is None


async def test_sweep_emits_nothing_when_no_rows_deleted(
    sweep_session: AsyncSession, monkeypatch
):
    """Zero-suppressed: a non-pruning deployment never emits a purge event."""
    await audit_settings_service.update_settings(sweep_session, {"retention_days": 0})
    emitted: list[tuple] = []
    monkeypatch.setattr(
        retention_service.audit_service,
        "emit",
        lambda action, **kw: emitted.append((action, kw)),
    )

    assert await retention_service.sweep_once() == (0, 0)
    assert emitted == []


async def test_sweep_emits_purge_event_when_rows_deleted(
    sweep_session: AsyncSession, monkeypatch
):
    # Spy rather than reading the trail back: conftest sets AUDIT_PERSIST=false, so
    # nothing this emits is persisted.
    await audit_settings_service.update_settings(sweep_session, {"retention_days": 90})
    old = AuditEvent(action="auth.login", created_at=datetime.now(UTC) - timedelta(days=400))
    sweep_session.add(old)
    await sweep_session.flush()
    emitted: list[tuple] = []
    monkeypatch.setattr(
        retention_service.audit_service,
        "emit",
        lambda action, **kw: emitted.append((action, kw)),
    )

    await retention_service.sweep_once()

    assert len(emitted) == 1
    action, kwargs = emitted[0]
    assert action == "audit.retention_purge"
    assert kwargs["severity"] == "warning"
    assert "events=1" in kwargs["reason"]
    assert "retention_days=90" in kwargs["reason"]


async def test_sweep_broadcasts_nothing(sweep_session: AsyncSession):
    """Tripwire for the whoever-commits-dispatches rule: a purge is invisible to clients."""
    await audit_settings_service.update_settings(sweep_session, {"retention_days": 90})
    sweep_session.add(
        AuditEvent(action="auth.login", created_at=datetime.now(UTC) - timedelta(days=400))
    )
    await sweep_session.flush()

    await retention_service.sweep_once()

    buffer = sweep_session.info.get("domain_events")
    assert buffer is None or not buffer.pending
