from __future__ import annotations

from threading import Lock

from sahabino.crawler.application.ports.proxy import ProxyLeasePort, ProxyProvider
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    GatewayFailure,
    NetworkTimeout,
    ProxyAuthenticationFailure,
    ProxyConnectionFailure,
    RateLimited,
    TemporaryConnectionFailure,
)


class NetworkPolicy:
    """Owns bounded egress health accounting and lease rotation decisions."""

    def __init__(self, provider: ProxyProvider, *, rate_limit_rotate_after: int) -> None:
        if rate_limit_rotate_after < 1:
            raise ValueError("rate-limit rotation threshold must be at least one")
        self._provider = provider
        self._rate_limit_threshold = rate_limit_rotate_after
        self._rate_limits: dict[str, int] = {}
        self._lock = Lock()

    def handle_failure(
        self,
        error: BaseException,
        lease: ProxyLeasePort,
        context_id: str,
        *,
        allow_rotation: bool = True,
    ) -> ProxyLeasePort:
        if isinstance(error, AccessForbidden):
            if lease.is_direct:
                return lease
            self._provider.start_cooldown(lease)
            return self._provider.rotate(lease, context_id) if allow_rotation else lease

        if isinstance(error, ProxyAuthenticationFailure):
            if lease.is_direct:
                return lease
            self._provider.mark_unhealthy(lease)
            return self._provider.rotate(lease, context_id) if allow_rotation else lease

        should_record = isinstance(
            error,
            (
                ProxyConnectionFailure,
                NetworkTimeout,
                TemporaryConnectionFailure,
                GatewayFailure,
            ),
        )
        if isinstance(error, RateLimited):
            identity = lease.proxy_id or "direct"
            with self._lock:
                count = self._rate_limits.get(identity, 0) + 1
                self._rate_limits[identity] = count
            if count >= self._rate_limit_threshold and not lease.is_direct:
                self._provider.start_cooldown(lease)
                if allow_rotation:
                    return self._provider.rotate(lease, context_id)
            should_record = False

        if should_record and self._provider.record_failure(lease):
            return self._provider.rotate(lease, context_id) if allow_rotation else lease
        return lease

    def record_success(self, lease: ProxyLeasePort) -> None:
        self._provider.record_success(lease)
        identity = lease.proxy_id or "direct"
        with self._lock:
            self._rate_limits.pop(identity, None)
