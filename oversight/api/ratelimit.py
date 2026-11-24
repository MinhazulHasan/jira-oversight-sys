from __future__ import annotations

import math
import threading
from collections import deque

RATE_LIMITED = "ask_rate_limited"
# Viewers whose window has emptied are dropped once this many are held, so the memory stays bounded.
_SWEEP_AT = 1024


class RateLimiter:
    def __init__(self, count: int, window_s: int) -> None:
        if count < 1 or window_s < 1:
            raise ValueError("a rate limit needs at least one question in at least one second")
        self.count = count
        self.window_s = window_s
        self._asked: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def admit(self, viewer: str, now: float) -> int | None:
        """Count one question for the viewer at `now` (seconds) and return None"""
        with self._lock:
            asked = self._asked.setdefault(viewer, deque())
            while asked and asked[0] <= now - self.window_s:
                asked.popleft()
            if len(asked) >= self.count:
                return max(1, math.ceil(asked[0] + self.window_s - now))
            asked.append(now)
            if len(self._asked) > _SWEEP_AT:
                self._sweep(now)
            return None

    def _sweep(self, now: float) -> None:
        for key in [key for key, asked in self._asked.items() if not asked or asked[-1] <= now - self.window_s]:
            del self._asked[key]
