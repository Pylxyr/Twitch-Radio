from __future__ import annotations

import time
from collections import deque

# Same sliding-window shape as admin.security.AuthRateLimiter, generalized:
# each named counter keeps timestamps within `window_seconds` and reports
# a count. Bounded by maxlen so a runaway event source can't grow this
# unboundedly between prunes.
_WINDOW_SECONDS = 3600.0
_MAX_SAMPLES = 2000


class RollingCounters:
    """In-memory only (resets on restart, like AuthRateLimiter) — this is
    operator visibility, not an audit log."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}

    def record(self, name: str) -> None:
        bucket = self._events.setdefault(name, deque(maxlen=_MAX_SAMPLES))
        bucket.append(time.monotonic())

    def count_last_hour(self, name: str) -> int:
        bucket = self._events.get(name)
        if not bucket:
            return 0
        cutoff = time.monotonic() - _WINDOW_SECONDS
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket)

    def snapshot(self) -> dict[str, int]:
        return {name: self.count_last_hour(name) for name in self._events}


# Process-wide singleton — every module that wants to record an event
# imports this directly rather than threading a counters object through
# every constructor, the same way `logging.getLogger(__name__)` is used
# throughout this codebase rather than passing loggers around explicitly.
counters = RollingCounters()
