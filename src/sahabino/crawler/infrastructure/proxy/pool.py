from __future__ import annotations

from threading import RLock

from pydantic import SecretStr

from sahabino.crawler.application.ports.clock import Clock
from sahabino.crawler.domain.errors import ProxyUnavailable
from sahabino.crawler.infrastructure.proxy.models import ProxyEndpoint, ProxyLease
from sahabino.crawler.infrastructure.resilience.clock import SystemClock


class ProxyPool:
    def __init__(
        self,
        proxy_urls: list[str],
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        direct_fallback: bool,
        clock: Clock | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("proxy failure threshold must be at least one")
        if cooldown_seconds < 0:
            raise ValueError("proxy cooldown must not be negative")
        if any(not value.strip() for value in proxy_urls):
            raise ValueError("proxy URLs must not be blank")
        self._endpoints = [
            ProxyEndpoint(f"proxy-{index}", SecretStr(url))
            for index, url in enumerate(proxy_urls, start=1)
        ]
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._direct_fallback = direct_fallback
        self._clock = clock or SystemClock()
        self._leases: dict[str, ProxyLease] = {}
        self._next_index = 0
        self._lock = RLock()

    @property
    def endpoints(self) -> tuple[ProxyEndpoint, ...]:
        with self._lock:
            return tuple(self._endpoints)

    def acquire(self, context_id: str, *, excluded_proxy_id: str | None = None) -> ProxyLease:
        with self._lock:
            existing = self._leases.get(context_id)
            if existing is not None and not existing.released:
                return existing
            now = self._clock.monotonic()
            endpoint = self._select(now, excluded_proxy_id)
            if endpoint is None and not self._direct_fallback:
                raise ProxyUnavailable("no usable proxy is available")
            lease = ProxyLease(context_id=context_id, endpoint=endpoint, acquired_at=now)
            self._leases[context_id] = lease
            return lease

    def rotate(self, lease: ProxyLease, context_id: str) -> ProxyLease:
        with self._lock:
            old_proxy_id = lease.proxy_id
            self.release(lease)
            return self.acquire(context_id, excluded_proxy_id=old_proxy_id)

    def record_success(self, lease: ProxyLease) -> None:
        with self._lock:
            if lease.endpoint is not None:
                lease.endpoint.record_success()

    def record_failure(self, lease: ProxyLease) -> bool:
        with self._lock:
            if lease.endpoint is None:
                return False
            return lease.endpoint.record_failure(
                self._threshold,
                self._cooldown,
                self._clock.monotonic(),
            )

    def start_cooldown(self, lease: ProxyLease) -> None:
        with self._lock:
            if lease.endpoint is not None:
                lease.endpoint.start_cooldown(self._cooldown, self._clock.monotonic())

    def mark_unhealthy(self, lease: ProxyLease) -> None:
        with self._lock:
            if lease.endpoint is not None:
                lease.endpoint.mark_unhealthy()

    def is_usable(self, lease: ProxyLease) -> bool:
        with self._lock:
            if lease.released:
                return False
            if lease.endpoint is None:
                return True
            return lease.endpoint.usable(self._clock.monotonic())

    def release(self, lease: ProxyLease) -> None:
        with self._lock:
            lease.release()
            if self._leases.get(lease.context_id) is lease:
                del self._leases[lease.context_id]

    def _select(self, now: float, excluded_proxy_id: str | None) -> ProxyEndpoint | None:
        count = len(self._endpoints)
        for offset in range(count):
            index = (self._next_index + offset) % count
            endpoint = self._endpoints[index]
            if endpoint.proxy_id != excluded_proxy_id and endpoint.usable(now):
                self._next_index = (index + 1) % count
                return endpoint
        return None
