"""The post-commit event seam (#212).

These pin the *mechanism*, not any particular frame: that an event can only ever be
dispatched if its transaction committed, that a rollback discards it, and — the subtle
one — that a discarded event cannot be resurrected by the next commit on the same session.
"""

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.services import domain_events as de


def _ev(exercise_id: int = 1) -> de.SummaryGenerated:
    return de.SummaryGenerated(exercise_id=exercise_id, payload={"summary_text": "x"})


def _user(email: str) -> User:
    return User(email=email, display_name="U", hashed_password="x")


async def test_record_outside_a_transaction_raises(session: AsyncSession):
    """Recording after the commit is the mistake this seam exists to prevent, so it fails
    loudly rather than leaving an event that no rollback can ever discard."""
    session.add(_user("a@x.test"))
    await session.commit()
    assert not session.in_transaction()

    with pytest.raises(RuntimeError, match="no transaction open"):
        de.record(session, _ev())


async def test_commit_promotes_pending_to_committed(session: AsyncSession):
    ev = _ev()
    session.add(_user("b@x.test"))
    de.record(session, ev)
    buf = de.buffer_for(session)
    assert buf.pending == [ev] and buf.committed == []

    await session.commit()
    assert buf.pending == [] and buf.committed == [ev]

    # This test deliberately stops at promotion and never fans out, so drain the buffer:
    # the autouse leak guard in conftest (rightly) fails any test that leaves a committed
    # event undispatched.
    buf.committed.clear()


async def test_dispatch_before_commit_sends_nothing(session: AsyncSession, monkeypatch):
    """The whole guarantee in one assertion: an event that has not committed cannot be
    fanned out, because only after_commit can move it into the dispatchable list."""
    seen: list[de.DomainEvent] = []
    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [_collect(seen)])

    session.add(_user("c@x.test"))
    de.record(session, _ev())
    await de.dispatch(session)

    assert seen == []

    await session.commit()
    await de.dispatch(session)
    assert len(seen) == 1


async def test_rollback_discards_and_a_later_commit_cannot_resurrect(
    session: AsyncSession, monkeypatch
):
    """The session-reuse hazard, and the reason after_soft_rollback is load-bearing.

    A request session is reused across several units of work. Without the discard, an
    event stranded by a rolled-back one is still sitting in `pending` when the *next*
    unit of work commits — and after_commit would sweep it up and broadcast a frame for
    something that never happened.
    """
    seen: list[de.DomainEvent] = []
    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [_collect(seen)])

    doomed = de.SummaryGenerated(exercise_id=111, payload={"summary_text": "never"})
    session.add(_user("d@x.test"))
    de.record(session, doomed)
    await session.rollback()
    assert de.buffer_for(session).pending == []

    # A second, entirely unrelated unit of work on the same session commits.
    survivor = de.SummaryGenerated(exercise_id=222, payload={"summary_text": "real"})
    session.add(_user("e@x.test"))
    de.record(session, survivor)
    await session.commit()
    await de.dispatch(session)

    assert [ev.exercise_id for ev in seen] == [222], "the rolled-back event was resurrected"


async def test_a_failing_subscriber_neither_propagates_nor_blocks_its_siblings(
    session: AsyncSession, monkeypatch
):
    """The transaction is already committed and authoritative. A dead socket must not turn
    a successful request into a 500, and must not cost the other subscribers their event."""
    seen: list[de.DomainEvent] = []

    async def boom(_session, _ev):
        raise RuntimeError("socket exploded")

    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [boom, _collect(seen)])

    session.add(_user("f@x.test"))
    de.record(session, _ev())
    await session.commit()

    await de.dispatch(session)  # must not raise
    assert len(seen) == 1


async def test_dispatch_drains_so_a_second_call_does_not_re_emit(
    session: AsyncSession, monkeypatch
):
    seen: list[de.DomainEvent] = []
    monkeypatch.setitem(de._subscribers, de.SummaryGenerated, [_collect(seen)])

    session.add(_user("g@x.test"))
    de.record(session, _ev())
    await session.commit()

    await de.dispatch(session)
    await de.dispatch(session)
    assert len(seen) == 1


def _collect(sink: list):
    async def handler(_session, ev):
        sink.append(ev)

    return handler
