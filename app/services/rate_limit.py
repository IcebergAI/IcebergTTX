"""In-memory sliding-window rate limiter for login brute-force protection (#11).

In-memory only: like ws_manager this assumes a single app process. A multi-
replica deployment would need a shared store (e.g. Redis) — see the replica
constraint in CLAUDE.md.

Memory is bounded (#49): read paths never materialise keys, a key is dropped as
soon as its window empties, and an opportunistic sweep purges expired keys once
per window so rotating attacker-controlled keys (spoofed X-Forwarded-For / email)
cannot accumulate without bound.
"""

import time
from collections import deque

from app.config import settings


class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._last_sweep = time.monotonic()

    def _trim(self, dq: deque[float], now: float) -> None:
        while dq and now - dq[0] > self.window_seconds:
            dq.popleft()

    def _sweep(self, now: float) -> None:
        """Drop every key whose window has fully expired.

        Called opportunistically at most once per window. Without this, a key
        that receives a single failed attempt and is never touched again would
        persist forever, so an attacker rotating keys grows the dict without
        bound (#49). The per-window sweep caps residency to the distinct keys
        seen within roughly one window.
        """
        for key in list(self._hits):
            dq = self._hits[key]
            self._trim(dq, now)
            if not dq:
                del self._hits[key]
        self._last_sweep = now

    def _active_hits(self, key: str, now: float) -> deque[float]:
        """Return the pruned deque for a key **without** materialising it.

        A missing key yields a throwaway empty deque (never stored), and a key
        that prunes down to empty is evicted, so merely checking a novel key can
        never leave a permanent entry behind.
        """
        if now - self._last_sweep > self.window_seconds:
            self._sweep(now)
        dq = self._hits.get(key)
        if dq is None:
            return deque()
        self._trim(dq, now)
        if not dq:
            del self._hits[key]
        return dq

    def is_limited(self, key: str) -> bool:
        return len(self._active_hits(key, time.monotonic())) >= self.max_attempts

    def retry_after(self, key: str) -> int:
        dq = self._active_hits(key, time.monotonic())
        if not dq:
            return 0
        remaining = self.window_seconds - (time.monotonic() - dq[0])
        return max(1, int(remaining))

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        else:
            self._trim(dq, now)
        dq.append(now)

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def clear(self) -> None:
        self._hits.clear()


login_rate_limiter = RateLimiter(
    settings.login_max_attempts, settings.login_lockout_seconds
)

# Registration flood protection (#67), keyed per source IP (every attempt counts,
# not just failures) — caps mass account creation from one host.
registration_rate_limiter = RateLimiter(
    settings.registration_max_attempts, settings.registration_lockout_seconds
)
