"""Post-commit domain events (#212), published across replicas (#213).

Services announce that something *durably happened*; subscribers project it outward
(WebSocket frames, triggered communications). Nothing is emitted inline any more.

The point is the ordering guarantee. Every external projection in this app has always
had to fire *after* the commit — a frame describing a transition that then rolls back is
a lie the client cannot take back — but that was a convention re-implemented at nine call
sites and enforced only by reading the code carefully. Here it is structural:

    record(session, ev)   -> appends to session.info["domain_events"].pending
    <BEFORE COMMIT>       -> each pending event is published on the bus, in-transaction
    <COMMIT>              -> SQLAlchemy's after_commit promotes pending -> committed
    <ROLLBACK>            -> after_soft_rollback discards pending
    await dispatch(...)   -> drains *committed* only, on this replica

``dispatch`` cannot send an uncommitted event, because the only thing that can ever move
an event into ``committed`` is a callback that Postgres's COMMIT fires. Forgetting to
call ``dispatch`` is caught by a safety-net drain in ``get_session`` teardown (and, in
tests, an autouse fixture that fails on a non-empty buffer).

The bus publication has the same property for free, and that is why it is issued from
``before_commit`` rather than alongside ``dispatch``: ``NOTIFY`` inside a transaction is
delivered by Postgres only if that transaction commits, and discarded if it rolls back.
The remote replicas' copy of the guarantee is therefore enforced by the database rather
than by another convention nobody can see.

Three rules for anyone adding an event:

* **Record inside the transaction, dispatch after it.** ``record`` raises if the session
  has no transaction open, which is what "you recorded after the commit" looks like.
* **Flush before you record.** Events carry ids, so the row has to exist; every call
  site already flushes for its own reasons, and ``record`` will not accept a None id.
* **Dispatch while the session is still open.** Handlers read the database — they are
  given only ids — so this is not a fire-and-forget queue.

A handler that raises is logged and swallowed: the transaction is already committed and
authoritative, and one dead socket must never turn a successful request into a 500. That
generalises the guard #129 put on the lifecycle route to every projection.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.services import pg_bus

if TYPE_CHECKING:
    from sqlmodel.ext.asyncio.session import AsyncSession

logger = logging.getLogger(__name__)


# ── Events ────────────────────────────────────────────────────────────────────
#
# Every event is ids and scalars, never an ORM instance. Two reasons, and the second is
# the one that made it a rule: an ORM object cannot cross a process boundary, so a
# replicated event has to name its rows rather than carry them; and a row read back
# after the commit is authoritative, where a carried instance is a snapshot of whatever
# the recording session happened to hold (the lifecycle CAS runs with
# synchronize_session=False, so that snapshot was demonstrably stale — see #212).
#
# Handlers therefore read what they need. On the replica that recorded the event those
# reads are identity-map hits; on every other one they are the point.


@dataclass(frozen=True)
class DomainEvent:
    exercise_id: int


@dataclass(frozen=True)
class ExerciseStateChanged(DomainEvent):
    transition_id: int
    action: str


@dataclass(frozen=True)
class InjectReleased(DomainEvent):
    inject_id: int


@dataclass(frozen=True)
class InjectUpdated(DomainEvent):
    inject_id: int


@dataclass(frozen=True)
class InjectCommentCreated(DomainEvent):
    comment_id: int


@dataclass(frozen=True)
class ResponseSubmitted(DomainEvent):
    response_id: int


@dataclass(frozen=True)
class CommunicationCreated(DomainEvent):
    communication_id: int


@dataclass(frozen=True)
class ResponseAssessed(DomainEvent):
    response_id: int
    assessment_id: int


@dataclass(frozen=True)
class InjectSuggested(DomainEvent):
    suggested_id: int


@dataclass(frozen=True)
class SummaryGenerated(DomainEvent):
    summary_id: int


Handler = Callable[["AsyncSession", Any], Awaitable[None]]


# ── The wire format ───────────────────────────────────────────────────────────
#
# Wire names are declared rather than derived from the class name, so renaming a class
# cannot silently desynchronise two replicas mid-rolling-deploy.

EVENT_NAMES: dict[type[DomainEvent], str] = {
    ExerciseStateChanged: "exercise_state_changed",
    InjectReleased: "inject_released",
    InjectUpdated: "inject_updated",
    InjectCommentCreated: "inject_comment_created",
    ResponseSubmitted: "response_submitted",
    CommunicationCreated: "communication_created",
    ResponseAssessed: "response_assessed",
    InjectSuggested: "inject_suggested",
    SummaryGenerated: "summary_generated",
}

_EVENTS_BY_NAME: dict[str, type[DomainEvent]] = {
    name: cls for cls, name in EVENT_NAMES.items()
}


def to_descriptor(ev: DomainEvent) -> dict[str, Any]:
    """Render an event as a bus descriptor."""
    return pg_bus.envelope(event=EVENT_NAMES[type(ev)], **asdict(ev))


def from_descriptor(descriptor: dict[str, Any]) -> DomainEvent | None:
    """Rebuild an event from a bus descriptor, or None if it is not one we project.

    Unknown names and missing fields are skipped rather than raised: during a rolling
    deploy the replica on the other side of the bus may be a different build.
    """
    cls = _EVENTS_BY_NAME.get(descriptor.get("event", ""))
    if cls is None:
        return None
    try:
        return cls(**{f.name: descriptor[f.name] for f in fields(cls)})
    except KeyError:
        logger.warning("bus descriptor for %s is missing fields: %r", cls.__name__, descriptor)
        return None


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
    """Announce ``ev``, to be published and dispatched only if this transaction commits."""
    if not session.in_transaction():
        raise RuntimeError(
            f"{type(ev).__name__} was recorded with no transaction open — an event must be "
            "recorded *inside* the unit of work that persists it, or a rollback cannot "
            "discard it. Move the record() call above the commit."
        )
    missing = [f.name for f in fields(ev) if getattr(ev, f.name) is None]
    if missing:
        raise RuntimeError(
            f"{type(ev).__name__} was recorded with {', '.join(missing)} unset — the row "
            "has not been flushed yet, so no replica (including this one) could read it "
            "back. Flush before recording."
        )
    _buffer(session).pending.append(ev)


@event.listens_for(Session, "before_commit")
def _publish_on_commit(session: Session) -> None:
    """Put every pending event on the bus, inside the transaction that is committing.

    Deliberately not "after the commit, alongside dispatch": Postgres holds a NOTIFY
    until COMMIT and drops it on rollback, so issuing it here is what makes a peer
    replica's copy of the frame exactly as trustworthy as this one's. Publishing after
    the commit would reopen the window where the state changed and the announcement was
    lost, which is the bug class this whole seam exists to close.
    """
    buf = session.info.get(_KEY)
    if buf is None or not buf.pending:
        return
    connection = session.connection()
    for ev in buf.pending:
        try:
            payload = pg_bus.encode(to_descriptor(ev))
        except (ValueError, KeyError):
            # A descriptor this replica cannot render is a bug in the event, not in the
            # user's request: local projection still works, so log it rather than
            # failing a commit that has already done everything it was asked to.
            logger.exception("could not render %s for the bus", type(ev).__name__)
            continue
        connection.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": pg_bus.CHANNEL_EVENTS, "payload": payload},
        )


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
_local_only: set[Handler] = set()
_projectors_loaded = False


def subscribe(event_type: type[DomainEvent]) -> Callable[[Handler], Handler]:
    """Register a projection: runs on every replica, for local and relayed events alike."""

    def decorate(handler: Handler) -> Handler:
        _subscribers.setdefault(event_type, []).append(handler)
        return handler

    return decorate


def subscribe_local(event_type: type[DomainEvent]) -> Callable[[Handler], Handler]:
    """Register a consequence that must run exactly once, on the replica that recorded it.

    A projection is idempotent by nature — every replica rendering the same frame for
    its own sockets is the whole point. Work that *acts* is not: arming a timer on all
    three replicas fires it three times. Those handlers stay local until they can be
    made durable and transactional (#213 step 3).
    """

    def decorate(handler: Handler) -> Handler:
        _subscribers.setdefault(event_type, []).append(handler)
        _local_only.add(handler)
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


async def project(session: AsyncSession, ev: DomainEvent, *, local: bool = True) -> None:
    """Run the subscribers for one event.

    ``local=False`` is the relay's path: an event another replica recorded, so only the
    projections run — the acting handlers already ran where the work was done.
    """
    _load_projectors()
    for handler in _subscribers.get(type(ev), []):
        if not local and handler in _local_only:
            continue
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
        await project(session, ev, local=True)
