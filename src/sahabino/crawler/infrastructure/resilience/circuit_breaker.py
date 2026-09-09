from __future__ import annotations

from enum import StrEnum
from threading import Lock

from sahabino.crawler.application.ports.clock import Clock
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    CircuitOpen,
    NetworkTimeout,
    RateLimited,
    TemporaryConnectionFailure,
    UpstreamFailure,
)
from sahabino.crawler.infrastructure.resilience.clock import SystemClock


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int,
        cooldown_seconds: float,
        *,
        enabled: bool = True,
        clock: Clock | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("circuit failure threshold must be at least one")
        if cooldown_seconds < 0:
            raise ValueError("circuit cooldown must not be negative")
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._enabled = enabled
        self._clock = clock or SystemClock()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._generation = 0
        self._lock = Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    def before_call(self) -> int | None:
        if not self._enabled:
            return None
        with self._lock:
            if self._state == CircuitState.OPEN:
                assert self._opened_at is not None
                if self._clock.monotonic() - self._opened_at < self._cooldown:
                    raise CircuitOpen("Google Play circuit is open")
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = False
            if self._state == CircuitState.HALF_OPEN:
                if self._probe_in_flight:
                    raise CircuitOpen("Google Play circuit half-open probe is in progress")
                self._probe_in_flight = True
            return self._generation

    def record_success(self, *, call_token: int | None = None) -> None:
        if not self._enabled:
            return
        with self._lock:
            if call_token is not None and call_token != self._generation:
                return
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(
        self,
        error: BaseException,
        *,
        proxied: bool = False,
        call_token: int | None = None,
    ) -> None:
        if not self._enabled:
            return
        relevant = isinstance(
            error,
            (
                AccessForbidden,
                UpstreamFailure,
                RateLimited,
                NetworkTimeout,
                TemporaryConnectionFailure,
            ),
        )
        if proxied and isinstance(
            error,
            (AccessForbidden, NetworkTimeout, TemporaryConnectionFailure),
        ):
            relevant = False
        with self._lock:
            if call_token is not None and call_token != self._generation:
                return
            if self._state == CircuitState.OPEN:
                return
            if not relevant:
                self._probe_in_flight = False
                return
            if self._state == CircuitState.HALF_OPEN:
                self._open()
                return
            self._failure_count += 1
            if self._failure_count >= self._threshold:
                self._open()

    def _open(self) -> None:
        self._generation += 1
        self._state = CircuitState.OPEN
        self._opened_at = self._clock.monotonic()
        self._probe_in_flight = False
