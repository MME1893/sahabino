from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import SecretStr


class ProxyState(StrEnum):
    HEALTHY = "healthy"
    COOLDOWN = "cooldown"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


@dataclass(slots=True)
class ProxyEndpoint:
    proxy_id: str
    _url: SecretStr = field(repr=False)
    state: ProxyState = ProxyState.HEALTHY
    failure_count: int = 0
    cooldown_until: float | None = None

    def usable(self, now: float) -> bool:
        if (
            self.state == ProxyState.COOLDOWN
            and self.cooldown_until is not None
            and now >= self.cooldown_until
        ):
            self.record_success()
        return self.state == ProxyState.HEALTHY

    def record_success(self) -> None:
        if self.state not in {ProxyState.DISABLED, ProxyState.UNHEALTHY}:
            self.state = ProxyState.HEALTHY
            self.failure_count = 0
            self.cooldown_until = None

    def record_failure(self, threshold: int, cooldown_seconds: float, now: float) -> bool:
        if self.state in {ProxyState.DISABLED, ProxyState.UNHEALTHY}:
            return False
        self.failure_count += 1
        if self.failure_count < threshold:
            return False
        self.start_cooldown(cooldown_seconds, now)
        return True

    def start_cooldown(self, cooldown_seconds: float, now: float) -> None:
        if self.state not in {ProxyState.DISABLED, ProxyState.UNHEALTHY}:
            self.state = ProxyState.COOLDOWN
            self.cooldown_until = now + cooldown_seconds

    def disable(self) -> None:
        self.state = ProxyState.DISABLED

    def mark_unhealthy(self) -> None:
        if self.state != ProxyState.DISABLED:
            self.state = ProxyState.UNHEALTHY
            self.cooldown_until = None

    def secret_url(self) -> str:
        return self._url.get_secret_value()


@dataclass(slots=True)
class ProxyLease:
    context_id: str
    endpoint: ProxyEndpoint | None = field(default=None, repr=False)
    acquired_at: float = 0.0
    _released: bool = field(default=False, repr=False)

    @property
    def proxy_id(self) -> str | None:
        return self.endpoint.proxy_id if self.endpoint is not None else None

    @property
    def is_direct(self) -> bool:
        return self.endpoint is None

    @property
    def released(self) -> bool:
        return self._released

    def secret_url(self) -> str | None:
        return self.endpoint.secret_url() if self.endpoint is not None else None

    def release(self) -> None:
        self._released = True


@dataclass(frozen=True, slots=True)
class NetworkContext:
    context_id: str
    lease: ProxyLease
