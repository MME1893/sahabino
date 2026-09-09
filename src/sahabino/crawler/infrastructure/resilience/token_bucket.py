from __future__ import annotations

from threading import Lock

from sahabino.crawler.application.ports.clock import Clock
from sahabino.crawler.domain.errors import LocalRateLimitWaitExceeded
from sahabino.crawler.infrastructure.resilience.clock import SystemClock


class NoOpRateLimiter:
    def acquire(self) -> None:
        return


class TokenBucketRateLimiter:
    """Process-wide token bucket; a consumed request token is never refunded."""

    def __init__(
        self,
        refill_per_second: float,
        capacity: int,
        *,
        clock: Clock | None = None,
        maximum_wait_seconds: float = 60.0,
    ) -> None:
        if refill_per_second <= 0:
            raise ValueError("refill rate must be positive")
        if capacity < 1:
            raise ValueError("capacity must be at least one")
        if maximum_wait_seconds <= 0:
            raise ValueError("maximum wait must be positive")
        self._rate = refill_per_second
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock or SystemClock()
        self._last_refill = self._clock.monotonic()
        self._maximum_wait = maximum_wait_seconds
        self._lock = Lock()

    def acquire(self) -> None:
        deadline = self._clock.monotonic() + self._maximum_wait
        while True:
            with self._lock:
                now = self._clock.monotonic()
                self._refill(now)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait_seconds = (1 - self._tokens) / self._rate
                if now + wait_seconds > deadline:
                    raise LocalRateLimitWaitExceeded(
                        "global rate limiter could not grant a token within its local wait bound"
                    )
            self._clock.sleep(wait_seconds)

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill(self._clock.monotonic())
            return self._tokens

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = max(self._last_refill, now)
