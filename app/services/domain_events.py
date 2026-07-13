"""Post-commit domain events (#212).

Services announce that something *durably happened*; subscribers project it outward
(WebSocket frames, triggered communications). Nothing is emitted inline any more.

The point is the ordering guarantee. Every external projection in this app has always
had to fire *after* the commit — a frame describing a transition that then rolls back is
a lie the client cannot take back — but that was a convention re-implemented at nine call
sites and enforced only by reading the code carefully. Here it is structural:

    record(session, ev)   -> appends to session.info["domain_events"].pending
    <COMMIT>              -> SQLAlchemy's after_commit promotes pending -> committed
    <ROLLBACK>            -> after_soft_rollback discards pending
    await dispatch(...)   -> drains *committed* only

``dispatch`` cannot send an uncommitted event, because the only thing that can ever move
an event into ``committed`` is a callback that Postgres's COMMIT fires. Forgetting to
call ``dispatch`` is caught by a safety-net drain in ``get_session`` teardown (and, in
tests, an autouse fixture that fails on a non-empty buffer).

Two rules for anyone adding an event:

* **Record inside the transaction, dispatch after it.** ``record`` raises if the session
  has no transaction open, which is what "you recorded after the commit" looks like.
* **Dispatch while the session is still open.** Handlers may read the database — the
  inject payload builders do — so this is not a fire-and-forget queue.

A handler that raises is logged and swallowed: the transaction is already committed and
authoritative, and one dead socket must never turn a successful request into a 500. That
generalises the guard #129 put on the lifecycle route to every projection.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import event
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.exercise import ExerciseStateTransition
    from app.models.inject import Inject
    from app.models.inject_comment import InjectComment
    from app.models.response import Response
    from app.models.suggested_inject import SuggestedInject

logger = logging.getLogger(__name__)


# ── Events ────────────────────────────────────────────────────────────────────
#
# Events may carry ORM objects: every session that records one is opened with
# expire_on_commit=False, so attributes stay loaded after the commit that promotes it.
# (app/main.py's lifespan sessions and audit_service's persistence session are NOT —
# never record an event on those.)


@dataclass(frozen=True)
class DomainEvent:
    exercise_id: int


@dataclass(frozen=True)
class ExerciseStateChanged(DomainEvent):
    # No Exercise here on purpose. The lifecycle CAS runs with synchronize_session=False,
    # so the in-session Exercise still holds its pre-transition attributes at record time
    # and is only refreshed afterwards. Carrying it would work today purely because
    # dispatch happens later — the handler re-reads it instead (an identity-map hit).
    transition: ExerciseStateTransition
    action: str


@dataclass(frozen=True)
class InjectReleased(DomainEvent):
    inject: Inject


@dataclass(frozen=True)
class InjectUpdated(DomainEvent):
    inject: Inject


@dataclass(frozen=True)
class InjectCommentCreated(DomainEvent):
    comment: InjectComment
    payload: dict[str, Any]


@dataclass(frozen=True)
class ResponseSubmitted(DomainEvent):
    response: Response


@dataclass(frozen=True)
class CommunicationCreated(DomainEvent):
    # An id, not the ORM row: _delayed_comm learns its id from an INSERT ... RETURNING
    # and never loads the object.
    communication_id: int


@dataclass(frozen=True)
class ResponseAssessed(DomainEvent):
    response_id: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class InjectSuggested(DomainEvent):
    suggested: SuggestedInject


@dataclass(frozen=True)
class SummaryGenerated(DomainEvent):
    payload: dict[str, Any]


Handler = Callable[["AsyncSession", Any], Awaitable[None]]


# ── The session-scoped buffer ─────────────────────────────────────────────────

_KEY = "domain_events"


@dataclass
class _Buffer:
    pending: list[DomainEvent] = field(default_factory=list)
    committed: list[DomainEvent] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.pending or self.committed)


def _buffer(session: Any) -> _Buffer:
    """The buffer for this session, created on first use.

    ``AsyncSession.info`` *is* the underlying ``Session.info`` (the same dict), which is
    what lets the synchronous listeners below see what async code recorded.
    """
    buf = session.info.get(_KEY)
    if buf is None:
        buf = _Buffer()
        session.info[_KEY] = buf
    return buf


def buffer_for(session: Any) -> _Buffer:
    """Read-only accessor, for tests and the teardown drain."""
    return _buffer(session)


def record(session: AsyncSession, ev: DomainEvent) -> None:
    """Announce ``ev``, to be dispatched only if the current transaction commits."""
    if not session.in_transaction():
        raise RuntimeError(
            f"{type(ev).__name__} was recorded with no transaction open — an event must be "
            "recorded *inside* the unit of work that persists it, or a rollback cannot "
            "discard it. Move the record() call above the commit."
        )
    _buffer(session).pending.append(ev)


@event.listens_for(Session, "after_commit")
def _promote_on_commit(session: Session) -> None:
    buf = session.info.get(_KEY)
    if buf is None or not buf.pending:
        return
    buf.committed.extend(buf.pending)
    buf.pending.clear()


@event.listens_for(Session, "after_soft_rollback")
def _discard_on_rollback(session: Session, previous_transaction: Any) -> None:
    """Drop events whose transaction did not survive.

    Load-bearing, and the subtlest part of this module: a request session is reused
    across several units of work, so an event stranded in ``pending`` by a rolled-back
    one would otherwise be swept up by the *next* successful commit and broadcast — a
    frame for something that never happened.
    """
    buf = session.info.get(_KEY)
    if buf is None or not buf.pending:
        return
    logger.debug("discarding %d uncommitted domain event(s) after rollback", len(buf.pending))
    buf.pending.clear()


# ── Subscribers ───────────────────────────────────────────────────────────────

_subscribers: dict[type[DomainEvent], list[Handler]] = {}
_projectors_loaded = False


def subscribe(event_type: type[DomainEvent]) -> Callable[[Handler], Handler]:
    def decorate(handler: Handler) -> Handler:
        _subscribers.setdefault(event_type, []).append(handler)
        return handler

    return decorate


def _load_projectors() -> None:
    """Import the subscriber modules on first dispatch.

    Deferred deliberately, and *not* for the usual cycle reason: registration has to be
    guaranteed for any entry point, and tests call services like ``release_inject``
    directly without ever importing ``app.main``. Registering at app startup would leave
    those paths with an empty registry, silently dropping every frame — a failure that
    looks like "the feature is broken" rather than "the wiring is missing".
    """
    global _projectors_loaded
    if _projectors_loaded:
        return
    _projectors_loaded = True
    from app.services import ws_projector  # noqa: F401


async def dispatch(session: AsyncSession) -> None:
    """Fan out every event whose transaction committed. Call after each unit of work."""
    _load_projectors()
    buf = session.info.get(_KEY)
    if buf is None:
        return
    # Anything still pending belongs to a transaction that has not committed yet. Leave it
    # alone: it is not ours to send, and it is not ours to drop either — dropping would
    # destroy an event that is about to commit legitimately. If its unit of work is never
    # committed, closing the session rolls it back and _discard_on_rollback clears it.
    if buf.pending:
        logger.debug(
            "dispatch called mid-transaction; %d event(s) still pending", len(buf.pending)
        )

    events, buf.committed = buf.committed, []
    for ev in events:
        for handler in _subscribers.get(type(ev), []):
            try:
                await handler(session, ev)
            except Exception:
                # The transaction is committed and authoritative. A failed projection is
                # logged, never raised: it must not turn a successful request into a 500,
                # and it must not stop the other subscribers for this event.
                logger.exception(
                    "domain event subscriber %s failed for %s",
                    getattr(handler, "__qualname__", handler),
                    type(ev).__name__,
                )
