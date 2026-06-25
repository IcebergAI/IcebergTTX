"""In-memory sliding-window rate limiter for login brute-force protection (#11).

In-memory only: like ws_manager this assumes a single app process. A multi-
replica deployment would need a shared store (e.g. Redis) — see the replica
constraint in CLAUDE.md.
"""

import time
from collections import defaultdict, deque

from app.config import settings


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        dq = self._hits[key]
        while dq and now - dq[0] > self.window_seconds:
            dq.popleft()
        return dq

    def is_limited(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_attempts

    def retry_after(self, key: str) -> int:
        dq = self._prune(key, time.monotonic())
        if not dq:
            return 0
        remaining = self.window_seconds - (time.monotonic() - dq[0])
        return max(1, int(remaining))

    def record_failure(self, key: str) -> None:
        self._prune(key, time.monotonic()).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def clear(self) -> None:
        self._hits.clear()


login_rate_limiter = RateLimiter(
    settings.login_max_attempts, settings.login_lockout_seconds
)
