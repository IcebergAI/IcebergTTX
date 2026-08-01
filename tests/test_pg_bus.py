"""The cross-replica bus and its transactional guarantee (#213)."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine, make_asyncpg_dsn
from app.services import pg_bus


def test_envelope_stamps_version_and_origin():
    descriptor = pg_bus.envelope(event="inject_released", inject_id=7)
    assert descriptor == {
        "v": 1,
        "origin": pg_bus.ORIGIN,
        "event": "inject_released",
        "inject_id": 7,
    }


def test_own_publications_are_recognisable():
    assert pg_bus.is_own(pg_bus.envelope(scope="proxy"))
    assert not pg_bus.is_own({"v": 1, "origin": "some-other-replica", "scope": "proxy"})
    # A descriptor with no origin at all is not ours — treating it as ours would make a
    # replica skip work it should have done.
    assert not pg_bus.is_own({"v": 1, "scope": "proxy"})


def test_round_trips_through_encode_and_decode():
    descriptor = pg_bus.envelope(event="communication_created", communication_id=3)
    assert pg_bus.decode(pg_bus.encode(descriptor)) == descriptor


def test_oversized_payload_is_refused_before_it_reaches_postgres():
    """Postgres rejects a NOTIFY over 8000 bytes with an error that would abort the
    publishing transaction — i.e. roll back the state change that occasioned it. Fail
    where the mistake is instead."""
    with pytest.raises(ValueError, match="ids, not content"):
        pg_bus.encode(pg_bus.envelope(content="x" * pg_bus.PAYLOAD_LIMIT_BYTES))


@pytest.mark.parametrize(
    "payload",
    ["not json at all", '["a", "list"]', '"a string"', '{"v": 99, "scope": "proxy"}'],
)
def test_undecodable_payloads_are_skipped_not_raised(payload):
    """A malformed or future-versioned message must not take down the listener that
    still has this replica's clients to serve. Version 99 is a newer replica talking
    mid-rolling-deploy."""
    assert pg_bus.decode(payload) is None


async def test_notify_is_delivered_only_if_the_transaction_commits():
    """The reason this app is on Postgres rather than Redis.

    A broker publish happens when you call it; a NOTIFY happens when the transaction
    commits, and never happens at all if it rolls back. That is the guarantee
    ``domain_events`` hand-builds for one process, extended across replicas for free.

    Runs against real commits — the per-test savepoint session never truly commits, so
    it could never deliver a notification.
    """
    channel = f"ttx_test_{uuid4().hex[:8]}"
    received: asyncio.Queue[str] = asyncio.Queue()
    connection = await asyncpg.connect(make_asyncpg_dsn(os.environ["DATABASE_URL"]))
    try:
        await connection.add_listener(
            channel, lambda conn, pid, ch, payload: received.put_nowait(payload)
        )

        async with AsyncSession(engine) as session:
            await pg_bus.publish(session, channel, pg_bus.envelope(scope="rolled-back"))
            await session.rollback()

        async with AsyncSession(engine) as session:
            await pg_bus.publish(session, channel, pg_bus.envelope(scope="committed"))
            await session.commit()

        delivered = pg_bus.decode(await asyncio.wait_for(received.get(), timeout=5))
        # The rolled-back publication was queued first, so had it survived it would be
        # sitting at the head of this queue.
        assert delivered is not None
        assert delivered["scope"] == "committed"
        assert received.empty()
    finally:
        await connection.close(timeout=5)
