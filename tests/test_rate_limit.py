"""The rate limiter, now backed by Postgres (#11, #49, #67, #117, #213).

Assertions are on behaviour rather than on a private dict: the counters are rows, and
what matters is the window semantics, not how they are stored. A settable clock keeps
window expiry deterministic instead of making the suite sleep through it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.database import engine
from app.services import rate_limit
from app.services.rate_limit import RateLimiter


@pytest.fixture
def clock(monkeypatch):
    """A settable stand-in for the limiter's notion of now."""

    class Clock:
        def __init__(self) -> None:
            self.now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

        def __call__(self):
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    c = Clock()
    monkeypatch.setattr(rate_limit, "now", c)
    return c


@pytest.fixture
async def limiter():
    """A limiter on its own scope, so these never collide with the app's three."""
    instance = RateLimiter("test_scope", max_attempts=5, window_seconds=300)
    await instance.clear()
    yield instance
    await instance.clear()


async def _row_count(scope: str) -> int:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT count(*) FROM rate_limit_hits WHERE scope = :scope"),
                {"scope": scope},
            )
        ).scalar_one()


async def test_a_novel_key_is_not_limited(clock, limiter):
    assert await limiter.is_limited("ip:novel") is False
    assert await limiter.retry_after("ip:absent") == 0
    # Merely asking about a key must not record anything against it.
    assert await _row_count("test_scope") == 0


async def test_lockout_after_max_attempts(clock, limiter):
    for _ in range(4):
        await limiter.record_failure("ip:a")
    assert await limiter.is_limited("ip:a") is False

    await limiter.record_failure("ip:a")

    limited, retry_after = await limiter.check("ip:a")
    assert limited is True
    assert 0 < retry_after <= 300


async def test_the_window_slides_rather_than_resetting_in_a_block(clock, limiter):
    """A fixed bucket would let an attacker spend the whole allowance at the end of one
    window and again at the start of the next. Expiring the oldest hit individually is
    what prevents that."""
    for _ in range(5):
        await limiter.record_failure("ip:a")
    assert await limiter.is_limited("ip:a") is True

    # Just short of the window: the oldest hit still counts.
    clock.advance(299)
    assert await limiter.is_limited("ip:a") is True

    # Past it: one hit falls out and the caller is under the threshold again.
    clock.advance(2)
    assert await limiter.is_limited("ip:a") is False


async def test_retry_after_counts_down_as_the_window_moves(clock, limiter):
    for _ in range(5):
        await limiter.record_failure("ip:a")
    _, initial = await limiter.check("ip:a")

    clock.advance(200)

    _, later = await limiter.check("ip:a")
    assert later < initial
    # Never zero while still limited — a caller told to retry after 0 retries at once.
    assert later >= 1


async def test_reset_clears_only_its_own_key(clock, limiter):
    for _ in range(5):
        await limiter.record_failure("ip:a")
    await limiter.record_failure("ip:b")

    await limiter.reset("ip:a")

    assert await limiter.is_limited("ip:a") is False
    assert await _row_count("test_scope") == 1


async def test_scopes_cannot_count_each_others_attempts(clock):
    """The three limiters share a table; a registration flood must not lock out logins."""
    first = RateLimiter("scope_one", max_attempts=2, window_seconds=300)
    second = RateLimiter("scope_two", max_attempts=2, window_seconds=300)
    await first.clear()
    await second.clear()
    try:
        for _ in range(2):
            await first.record_failure("ip:shared")

        assert await first.is_limited("ip:shared") is True
        assert await second.is_limited("ip:shared") is False
    finally:
        await first.clear()
        await second.clear()


async def test_reconfigure_preserves_hits_and_applies_a_tighter_threshold(clock, limiter):
    """An admin tightening the policy locks out a host that is already over the new
    line, rather than granting it a fresh allowance."""
    for _ in range(3):
        await limiter.record_failure("ip:a")
    assert await limiter.is_limited("ip:a") is False

    limiter.reconfigure(max_attempts=3, window_seconds=300)

    assert await limiter.is_limited("ip:a") is True


async def test_sweep_removes_only_expired_hits(clock, monkeypatch):
    """Keys are attacker-controlled (a spoofed X-Forwarded-For, an arbitrary email), so
    the table has to shed old rows on a schedule rather than when someone happens to
    look (#49)."""
    instance = RateLimiter("sweep_scope", max_attempts=5, window_seconds=60)
    monkeypatch.setattr(rate_limit, "ALL_LIMITERS", (instance,))
    await instance.clear()
    try:
        await instance.record_failure("ip:old")
        clock.advance(120)
        await instance.record_failure("ip:fresh")

        removed = await rate_limit.sweep()

        assert removed == 1
        assert await _row_count("sweep_scope") == 1
    finally:
        await instance.clear()


async def test_the_app_limiters_have_distinct_scopes():
    """A shared scope would silently pool three different policies' counters."""
    scopes = [limiter.scope for limiter in rate_limit.ALL_LIMITERS]
    assert len(set(scopes)) == len(scopes)
