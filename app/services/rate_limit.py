"""Sliding-window rate limiter for brute-force protection (#11, #67, #117).

Counters live in Postgres, in an unlogged table, one row per counted attempt. They used
to be deques in process memory, which meant every replica enforced its own private
allowance: three replicas behind a load balancer multiplied the effective login limit by
three, and an attacker who simply kept reconnecting got the multiplier for free (#213).

Still a *sliding* window, not a fixed bucket, and for the same reason as before: a fixed
bucket lets an attacker spend the whole allowance at the end of one window and again at
the start of the next, i.e. double the limit back to back.

Three properties are worth stating because they are easy to break:

* **Attempts are counted outside the request's transaction.** Recording a failed login
  on the request session would let the 401's rollback erase the strike. Each limiter
  call takes its own short transaction and commits on its own.
* **Reads and writes are one round trip each.** ``check`` returns the count and the
  oldest hit together, so a limited caller learns its ``Retry-After`` without a second
  query racing the first.
* **Growth is bounded by a sweep, not by luck.** Keys are attacker-controlled (a spoofed
  ``X-Forwarded-For``, an arbitrary email), so expired rows are deleted on a schedule by
  ``task_queue.rate_limit_sweep`` rather than opportunistically by whoever calls next (#49).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, insert, select

from app.config import settings
from app.models.rate_limit import rate_limit_hits


def now() -> datetime:
    """Indirection so tests can drive the window without sleeping through it."""
    return datetime.now(UTC)


def _engine():
    """Resolve the engine at call time — the test suite rebinds it after import."""
    from app.database import engine

    return engine


class RateLimiter:
    """One named limiter. Instances are cheap: the state is entirely in the database."""

    def __init__(self, scope: str, max_attempts: int, window_seconds: int):
        self.scope = scope
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds

    async def check(self, key: str) -> tuple[bool, int]:
        """Return ``(is_limited, retry_after_seconds)`` for ``key``.

        One query, because a limited caller immediately needs the retry hint and asking
        twice would let the window move between the two answers.
        """
        moment = now()
        cutoff = moment - timedelta(seconds=self.window_seconds)
        statement = select(
            func.count(rate_limit_hits.c.id), func.min(rate_limit_hits.c.at)
        ).where(
            and_(
                rate_limit_hits.c.scope == self.scope,
                rate_limit_hits.c.key == key,
                rate_limit_hits.c.at > cutoff,
            )
        )
        async with _engine().connect() as connection:
            count, oldest = (await connection.execute(statement)).one()
        if count < self.max_attempts or oldest is None:
            return False, 0
        remaining = self.window_seconds - (moment - oldest).total_seconds()
        # At least a second: a caller told to retry after 0 retries immediately, which is
        # indistinguishable from not being limited at all.
        return True, max(1, int(remaining))

    async def is_limited(self, key: str) -> bool:
        limited, _ = await self.check(key)
        return limited

    async def retry_after(self, key: str) -> int:
        _, retry = await self.check(key)
        return retry

    async def record_failure(self, key: str) -> None:
        """Count one attempt against ``key``.

        Committed on its own connection, deliberately: the caller is usually about to
        raise, and a strike a rollback could erase is not a strike.
        """
        async with _engine().begin() as connection:
            await connection.execute(
                insert(rate_limit_hits).values(scope=self.scope, key=key, at=now())
            )

    async def reset(self, key: str) -> None:
        """Forget every attempt for ``key`` — a successful login clearing its own slate."""
        async with _engine().begin() as connection:
            await connection.execute(
                delete(rate_limit_hits).where(
                    and_(
                        rate_limit_hits.c.scope == self.scope,
                        rate_limit_hits.c.key == key,
                    )
                )
            )

    async def clear(self) -> None:
        """Drop this limiter's whole history (tests, and an admin unlocking everyone)."""
        async with _engine().begin() as connection:
            await connection.execute(
                delete(rate_limit_hits).where(rate_limit_hits.c.scope == self.scope)
            )

    def reconfigure(self, max_attempts: int, window_seconds: int) -> None:
        """Apply new limits without erasing active histories.

        Thresholds are configuration, not state, so they stay in memory and reach the
        other replicas over the config bus with the settings they belong to (#213).
        """
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds


login_rate_limiter = RateLimiter(
    "login", settings.login_max_attempts, settings.login_lockout_seconds
)

# Registration flood protection (#67), keyed per source IP (every attempt counts,
# not just failures) — caps mass account creation from one host.
registration_rate_limiter = RateLimiter(
    "registration",
    settings.registration_max_attempts,
    settings.registration_lockout_seconds,
)

# Password-reset request throttle (#117), keyed per source IP — caps outbound reset
# emails / token minting from one host (independent of whether the account exists).
password_reset_rate_limiter = RateLimiter(
    "password_reset",
    settings.password_reset_max_attempts,
    settings.password_reset_lockout_seconds,
)

ALL_LIMITERS = (login_rate_limiter, registration_rate_limiter, password_reset_rate_limiter)


def apply_config(
    *,
    login_max_attempts: int,
    login_lockout_seconds: int,
    registration_max_attempts: int,
    registration_lockout_seconds: int,
    password_reset_max_attempts: int,
    password_reset_lockout_seconds: int,
) -> None:
    """Push a runtime settings snapshot into all three live limiters."""
    login_rate_limiter.reconfigure(login_max_attempts, login_lockout_seconds)
    registration_rate_limiter.reconfigure(
        registration_max_attempts, registration_lockout_seconds
    )
    password_reset_rate_limiter.reconfigure(
        password_reset_max_attempts, password_reset_lockout_seconds
    )


async def sweep() -> int:
    """Delete hits older than the widest configured window. Returns rows removed.

    Scheduled rather than opportunistic: the previous in-process version swept on a read
    path, so a limiter nobody consulted never shed its rows — and the keys are exactly
    the ones an attacker can mint at will.
    """
    widest = max(limiter.window_seconds for limiter in ALL_LIMITERS)
    cutoff = now() - timedelta(seconds=widest)
    async with _engine().begin() as connection:
        result = await connection.execute(
            delete(rate_limit_hits).where(rate_limit_hits.c.at < cutoff)
        )
    return result.rowcount or 0
