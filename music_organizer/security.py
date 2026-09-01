"""Small process-local security controls for the web login boundary."""

import math
import threading
import time
from collections import defaultdict, deque


class LoginRateLimiter:
    def __init__(self, max_failures: int = 5, window_seconds: float = 60) -> None:
        self.max_failures = max(1, max_failures)
        self.window_seconds = max(1.0, window_seconds)
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, values: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while values and values[0] <= cutoff:
            values.popleft()

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            values = self._failures[key]
            self._prune(values, now)
            if len(values) < self.max_failures:
                if not values:
                    self._failures.pop(key, None)
                return 0
            return max(1, math.ceil(values[0] + self.window_seconds - now))

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            values = self._failures[key]
            self._prune(values, now)
            values.append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
