"""In-memory sliding-window rate limiter.

Two independent budgets are enforced in main.py:
  - per client IP (global abuse brake)
  - per (inbox, client IP) (one noisy webhook source can't starve an inbox)

In-memory is the right call for a single-instance service; for multi-instance
deployments, swap this for a Redis-backed token bucket behind the same
RateLimiter interface.
"""

import time
from collections import deque


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = {}

    def allowed(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        window_start = now - 60.0
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        while hits and hits[0] <= window_start:
            hits.popleft()
        if len(hits) >= self.per_minute:
            return False
        hits.append(now)
        # Opportunistic cleanup so idle keys don't accumulate forever.
        if len(self._hits) > 100_000:
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > window_start}
        return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = now if now is not None else time.monotonic()
        hits = self._hits.get(key)
        if not hits:
            return 0
        return max(1, int(hits[0] + 60.0 - now) + 1)
