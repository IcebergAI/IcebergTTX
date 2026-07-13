"""Unit tests for the login RateLimiter, incl. bounded memory (#49).

Uses a controllable monotonic clock so window expiry and the periodic sweep are
deterministic rather than time-dependent.
"""

import pytest

from app.services import rate_limit
from app.services.rate_limit import RateLimiter


@pytest.fixture
def clock(monkeypatch):
    """A settable stand-in for time.monotonic within the rate_limit module."""

    class Clock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    c = Clock()
    monkeypatch.setattr(rate_limit.time, "monotonic", c)
    return c


def test_checking_novel_key_does_not_materialise_it(clock):
    limiter = RateLimiter(max_attempts=5, window_seconds=300)
    assert limiter.is_limited("ip:novel") is False
    assert limiter.retry_after("ip:absent") == 0
    # Read-only checks must never leave a permanent entry behind (#49).
    assert limiter._hits == {}


def test_key_evicted_once_its_window_empties(clock):
    limiter = RateLimiter(max_attempts=5, window_seconds=300)
    limiter.record_failure("ip:a")
    assert "ip:a" in limiter._hits

    clock.now += 301  # entry now older than the window
    assert limiter.is_limited("ip:a") is False
    # Accessing an expired key prunes it to empty and drops it.
    assert "ip:a" not in limiter._hits


def test_lockout_after_max_attempts(clock):
    limiter = RateLimiter(max_attempts=3, window_seconds=300)
    for _ in range(3):
        assert limiter.is_limited("ip:a") is False
        limiter.record_failure("ip:a")
    assert limiter.is_limited("ip:a") is True
    assert limiter.retry_after("ip:a") >= 1


def test_reset_clears_counter(clock):
    limiter = RateLimiter(max_attempts=3, window_seconds=300)
    for _ in range(3):
        limiter.record_failure("ip:a")
    limiter.reset("ip:a")
    assert limiter._hits == {}
    assert limiter.is_limited("ip:a") is False


def test_reconfigure_preserves_hits_and_applies_tighter_threshold(clock):
    limiter = RateLimiter(max_attempts=5, window_seconds=300)
    limiter.record_failure("ip:a")
    limiter.record_failure("ip:a")

    limiter.reconfigure(max_attempts=2, window_seconds=600)

    assert len(limiter._hits["ip:a"]) == 2
    assert limiter.is_limited("ip:a") is True
    assert limiter.retry_after("ip:a") > 300


def test_periodic_sweep_bounds_rotating_keys(clock):
    """Rotating attacker keys (unique X-Forwarded-For / email) must not grow
    the dict without bound — expired keys are purged once per window (#49)."""
    limiter = RateLimiter(max_attempts=5, window_seconds=300)

    # First burst of unique keys, each with a single failed attempt.
    for i in range(50):
        key = f"attacker:{i}"
        limiter.is_limited(key)
        limiter.record_failure(key)
    assert len(limiter._hits) == 50

    # Advance past the window and mint one more key. The access triggers the
    # opportunistic sweep, which reclaims all 50 now-expired keys.
    clock.now += 301
    limiter.is_limited("attacker:new")
    limiter.record_failure("attacker:new")
    assert len(limiter._hits) == 1
    assert "attacker:new" in limiter._hits
