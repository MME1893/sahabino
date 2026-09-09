from __future__ import annotations

from threading import RLock

from sahabino.crawler.application.ports.proxy import ProxyLeasePort
from sahabino.crawler.infrastructure.proxy.models import ProxyLease
from sahabino.crawler.infrastructure.proxy.pool import ProxyPool


def _concrete(lease: ProxyLeasePort) -> ProxyLease:
    if not isinstance(lease, ProxyLease):
        raise TypeError("unsupported proxy lease implementation")
    return lease


class NoProxyProvider:
    def __init__(self) -> None:
        self._leases: dict[str, ProxyLease] = {}
        self._lock = RLock()

    def acquire(self, context_id: str) -> ProxyLease:
        with self._lock:
            lease = self._leases.get(context_id)
            if lease is None or lease.released:
                lease = ProxyLease(context_id=context_id)
                self._leases[context_id] = lease
            return lease

    def rotate(self, lease: ProxyLeasePort, context_id: str) -> ProxyLease:
        self.release(lease)
        return self.acquire(context_id)

    def record_success(self, lease: ProxyLeasePort) -> None:
        _concrete(lease)

    def record_failure(self, lease: ProxyLeasePort) -> bool:
        _concrete(lease)
        return False

    def start_cooldown(self, lease: ProxyLeasePort) -> None:
        _concrete(lease)

    def mark_unhealthy(self, lease: ProxyLeasePort) -> None:
        _concrete(lease)

    def is_usable(self, lease: ProxyLeasePort) -> bool:
        return not _concrete(lease).released

    def release(self, lease: ProxyLeasePort) -> None:
        with self._lock:
            concrete = _concrete(lease)
            concrete.release()
            self._leases.pop(concrete.context_id, None)


class PoolProxyProvider:
    def __init__(self, pool: ProxyPool) -> None:
        self.pool = pool

    def acquire(self, context_id: str) -> ProxyLease:
        return self.pool.acquire(context_id)

    def rotate(self, lease: ProxyLeasePort, context_id: str) -> ProxyLease:
        return self.pool.rotate(_concrete(lease), context_id)

    def record_success(self, lease: ProxyLeasePort) -> None:
        self.pool.record_success(_concrete(lease))

    def record_failure(self, lease: ProxyLeasePort) -> bool:
        return self.pool.record_failure(_concrete(lease))

    def start_cooldown(self, lease: ProxyLeasePort) -> None:
        self.pool.start_cooldown(_concrete(lease))

    def mark_unhealthy(self, lease: ProxyLeasePort) -> None:
        self.pool.mark_unhealthy(_concrete(lease))

    def is_usable(self, lease: ProxyLeasePort) -> bool:
        return self.pool.is_usable(_concrete(lease))

    def release(self, lease: ProxyLeasePort) -> None:
        self.pool.release(_concrete(lease))


class StaticProxyProvider(PoolProxyProvider):
    def __init__(
        self,
        proxy_url: str,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        direct_fallback: bool,
    ) -> None:
        super().__init__(
            ProxyPool(
                [proxy_url],
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                direct_fallback=direct_fallback,
            )
        )
