"""Retention sweep for AuditEvent and AuthToken rows (#251).

Two tables previously grew without bound and nothing ever deleted from them:

* ``AuditEvent`` — every security-relevant event persists a row when
  ``audit_persist`` is on, including attacker-drivable ones on unauthenticated
  endpoints, while the admin surface only reads the newest 200. Everything older is
  unreachable through the product yet retained forever, carrying actor emails and
  source IPs. Pruning is **opt-in**: ``retention_days`` on the AuditSettings row
  defaults to 0 ("keep forever"), because an upgrade must never silently destroy
  security records. Once pruning is on, SIEM forwarding is the archival path — but it
  is best-effort with no outbox or retry, so a forwarder outage overlapping a purge
  window loses those events permanently.
* ``AuthToken`` — not a log but the live state behind an emailed single-use link.
  The row must exist while the link is live so a click can be matched, refused as
  used, or refused as stale. Once it is dead it is a spent hash plus an email address
  with no purpose, so those rows are purged **unconditionally** — the audit trail
  separately records issuance and acceptance, so nothing is lost.

Single-process, like every other timer in the app: two replicas would run two
concurrent sweeps. See the single-replica note in CLAUDE.md.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.audit import AuditEvent
from app.models.auth_token import AuthToken
from app.services import audit_service, audit_settings_service

logger = logging.getLogger(__name__)

_SWEEP_INTERVAL_SECONDS = 86_400  # daily

# Rows deleted per transaction. One unbounded DELETE across a year of history would be
# a single giant transaction, a long row-lock window and a WAL spike; per-batch commits
# bound each one. _MAX_BATCHES bounds the whole sweep so the first-ever pass over a
# legacy table cannot run for an hour — the remainder is picked up next sweep.
_PURGE_BATCH = 5_000
_MAX_BATCHES = 200

# Dead tokens are kept briefly so "when was that invite sent?" stays answerable through
# a normal support cycle. `consume` already refuses both classes, so nothing live races
# these deletes.
_TOKEN_EXPIRED_GRACE = timedelta(days=7)
_TOKEN_USED_GRACE = timedelta(days=1)


async def purge_audit_events(
    session: AsyncSession, *, retention_days: int, now: datetime | None = None
) -> int:
    """Delete AuditEvent rows older than ``retention_days``. Returns the count deleted.

    ``retention_days <= 0`` means keep forever. The ``<=`` also neutralises a
    hand-edited negative row, which would otherwise compute a cutoff in the *future*
    and delete the entire table.
    """
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    # Strict `<`: a row landing exactly on the cutoff survives.
    return await _purge_in_batches(
        session, AuditEvent, col(AuditEvent.created_at) < cutoff
    )


async def purge_auth_tokens(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete spent and long-expired AuthToken rows. Returns the count deleted."""
    moment = now or datetime.now(UTC)
    dead = or_(
        col(AuthToken.used_at).is_not(None)
        & (col(AuthToken.used_at) < moment - _TOKEN_USED_GRACE),
        col(AuthToken.expires_at) < moment - _TOKEN_EXPIRED_GRACE,
    )
    return await _purge_in_batches(session, AuthToken, dead)


async def _purge_in_batches(session: AsyncSession, model: type, where) -> int:
    """Delete every row matching ``where``, committing one bounded batch at a time."""
    total = 0
    for _ in range(_MAX_BATCHES):
        deleted = len(
            (
                await session.exec(
                    delete(model)
                    .where(
                        col(model.id).in_(
                            select(col(model.id)).where(where).limit(_PURGE_BATCH)
                        )
                    )
                    .returning(col(model.id))
                    .execution_options(synchronize_session=False)
                )
            ).all()
        )
        await session.commit()
        total += deleted
        if deleted < _PURGE_BATCH:
            return total
    # Cap reached with rows still matching. Say so rather than returning a count that
    # reads as "everything older than the cutoff is gone" — the remainder goes next sweep.
    logger.warning(
        "retention sweep hit the %d-batch cap for %s after %d rows; remainder deferred "
        "to the next sweep",
        _MAX_BATCHES,
        model.__name__,
        total,
    )
    return total


async def sweep_once() -> tuple[int, int]:
    """Run one full retention pass. Returns ``(events_deleted, tokens_deleted)``."""
    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as session:
        # Read the window straight off the settings row — the only reader of this knob
        # is right here, so it needs no process-global cache (#251).
        retention_days = (await audit_settings_service.get_settings(session)).retention_days
        events = await purge_audit_events(session, retention_days=retention_days)
        tokens = await purge_auth_tokens(session)

    if events or tokens:
        # Zero-suppressed: a deployment with retention off and no dead tokens emits
        # nothing ever, and a pruning one emits at most one event per day. Destroying
        # security records is itself security-relevant — without this, a purged window
        # in the trail is indistinguishable from tampering. The event cannot eat itself:
        # its created_at is now, newer than any future cutoff by construction.
        audit_service.emit(
            "audit.retention_purge",
            target_type="audit_settings",
            reason=f"events={events} tokens={tokens} retention_days={retention_days}",
            severity="warning",
        )
    return events, tokens


async def retention_task() -> None:
    """Background task: sweep on startup, then once a day.

    Sweeping *before* the first sleep is what collapses the issue's "on-startup pass
    plus daily timer" into one construct. It deliberately runs inside the task rather
    than being awaited inline in the lifespan like ``rehydrate_schedules``: a first
    purge over a legacy table is unbounded and would delay readiness.
    """
    while True:
        try:
            await sweep_once()
        except Exception:
            # A failed sweep must never kill the loop. CancelledError is BaseException,
            # so the lifespan's task.cancel() still stops shutdown (cf. heartbeat_task).
            logger.exception("retention sweep failed; continuing")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
