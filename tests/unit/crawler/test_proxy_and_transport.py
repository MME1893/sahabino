from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from sahabino.crawler.application.policies.adapter import AdapterFallbackPolicy
from sahabino.crawler.application.policies.network import NetworkPolicy
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    AppGone,
    AppNotFound,
    ClientRequestFailure,
    GatewayFailure,
    LegalRestriction,
    LocalRateLimitWaitExceeded,
    NetworkTimeout,
    ProxyAuthenticationFailure,
    ProxyConnectionFailure,
    ProxyUnavailable,
    RateLimited,
    UpstreamFailure,
)
from sahabino.crawler.infrastructure.proxy.models import (
    NetworkContext,
    ProxyLease,
    ProxyState,
)
from sahabino.crawler.infrastructure.proxy.pool import ProxyPool
from sahabino.crawler.infrastructure.proxy.providers import (
    NoProxyProvider,
    PoolProxyProvider,
    StaticProxyProvider,
)
from sahabino.crawler.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
)
from sahabino.crawler.infrastructure.resilience.token_bucket import TokenBucketRateLimiter
from sahabino.crawler.infrastructure.transport.curl_cffi import CurlCffiTransport

from .fakes import FakeClock


class CountingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


class FakeResponse:
    def __init__(
        self, status_code: int, text: str = "body", headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def _pool(
    urls: list[str], *, direct: bool = False, threshold: int = 2, clock: FakeClock | None = None
) -> ProxyPool:
    return ProxyPool(
        urls,
        failure_threshold=threshold,
        cooldown_seconds=10,
        direct_fallback=direct,
        clock=clock or FakeClock(),
    )


def test_no_proxy_and_static_proxy_providers() -> None:
    direct = NoProxyProvider().acquire("app")
    static = StaticProxyProvider(
        "http://user:secret@proxy.test:8080",
        failure_threshold=1,
        cooldown_seconds=1,
        direct_fallback=True,
    ).acquire("app")

    assert direct.is_direct is True
    assert static.proxy_id == "proxy-1"
    assert "secret" not in repr(static)


def test_pool_selection_sticky_release_failure_cooldown_and_rotation() -> None:
    clock = FakeClock()
    pool = _pool(["http://one", "http://two"], clock=clock)
    first = pool.acquire("app")

    assert pool.acquire("app") is first
    assert pool.record_failure(first) is False
    assert pool.record_failure(first) is True
    assert first.endpoint is not None
    assert first.endpoint.state == ProxyState.COOLDOWN

    second = pool.rotate(first, "app")
    assert second.proxy_id != first.proxy_id
    pool.record_success(second)
    pool.release(second)
    assert second.released is True

    clock.advance(10)
    recovered = pool.acquire("another")
    assert recovered.proxy_id == "proxy-1"


def test_disabled_and_unhealthy_proxies_are_excluded() -> None:
    pool = _pool(["http://one", "http://two"])
    endpoints = pool.endpoints
    endpoints[0].disable()
    endpoints[1].mark_unhealthy()

    with pytest.raises(ProxyUnavailable):
        pool.acquire("app")


def test_direct_fallback_and_disabled_direct_fallback() -> None:
    assert _pool([], direct=True).acquire("app").is_direct is True
    with pytest.raises(ProxyUnavailable):
        _pool([], direct=False).acquire("app")


def test_pool_acquisition_is_thread_safe_and_credentials_are_secret() -> None:
    pool = _pool(["http://user:password@proxy.example:8080"])

    with ThreadPoolExecutor(max_workers=10) as executor:
        leases = list(executor.map(lambda i: pool.acquire(f"app-{i}"), range(10)))

    assert {lease.proxy_id for lease in leases} == {"proxy-1"}
    assert all("password" not in repr(lease) for lease in leases)
    assert "password" not in repr(pool.endpoints[0])


def test_network_policy_does_not_rotate_first_proxy_failure() -> None:
    provider = PoolProxyProvider(_pool(["http://one", "http://two"], threshold=2))
    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    lease = provider.acquire("app")

    assert policy.handle_failure(ProxyConnectionFailure("one"), lease, "app") is lease
    rotated = policy.handle_failure(ProxyConnectionFailure("two"), lease, "app")
    assert rotated.proxy_id != lease.proxy_id


def test_rate_limit_rotation_is_threshold_based() -> None:
    provider = PoolProxyProvider(_pool(["http://one", "http://two"], threshold=2))
    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    lease = provider.acquire("app")

    assert policy.handle_failure(RateLimited("first"), lease, "app") is lease
    rotated = policy.handle_failure(RateLimited("second"), lease, "app")
    assert rotated is not lease
    assert lease.endpoint is not None
    assert lease.endpoint.state == ProxyState.COOLDOWN


def test_access_forbidden_cools_proxy_and_rotation_is_operation_bounded() -> None:
    provider = PoolProxyProvider(_pool(["http://one", "http://two"], threshold=2))
    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    lease = provider.acquire("app")

    rotated = policy.handle_failure(AccessForbidden("403"), lease, "app")

    assert rotated.proxy_id != lease.proxy_id
    assert lease.endpoint is not None
    assert lease.endpoint.state == ProxyState.COOLDOWN

    final_lease = provider.acquire("final")
    unchanged = policy.handle_failure(
        AccessForbidden("403"),
        final_lease,
        "final",
        allow_rotation=False,
    )
    assert unchanged is final_lease
    assert final_lease.endpoint is not None
    assert final_lease.endpoint.state == ProxyState.COOLDOWN


def test_proxy_authentication_marks_endpoint_unhealthy_and_rotates() -> None:
    provider = PoolProxyProvider(_pool(["http://one", "http://two"], threshold=2))
    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    lease = provider.acquire("app")

    rotated = policy.handle_failure(ProxyAuthenticationFailure("407"), lease, "app")

    assert rotated.proxy_id != lease.proxy_id
    assert lease.endpoint is not None
    assert lease.endpoint.state == ProxyState.UNHEALTHY
    assert lease.endpoint.usable(1_000) is False


def test_transport_surfaces_429_retry_after_and_consumes_token() -> None:
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"})])
    limiter = CountingLimiter()
    transport = CurlCffiTransport(
        NetworkContext("app", ProxyLease("app")),
        limiter,
        timeout_seconds=20,
        session_factory=lambda: session,
    )

    with pytest.raises(RateLimited) as captured:
        transport.request("GET", "https://play.google.test")

    assert captured.value.retry_after_seconds == 7
    assert limiter.calls == 1


@pytest.mark.parametrize(
    ("status", "expected_type"),
    [
        (400, ClientRequestFailure),
        (401, ClientRequestFailure),
        (403, AccessForbidden),
        (407, ProxyAuthenticationFailure),
        (408, NetworkTimeout),
        (404, AppNotFound),
        (410, AppGone),
        (451, LegalRestriction),
        (500, UpstreamFailure),
        (502, GatewayFailure),
        (503, UpstreamFailure),
        (504, GatewayFailure),
    ],
)
def test_transport_classifies_http_status_by_network_semantics(
    status: int, expected_type: type[Exception]
) -> None:
    session = FakeSession([FakeResponse(status)])
    transport = CurlCffiTransport(
        NetworkContext("app", ProxyLease("app")),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: session,
    )

    with pytest.raises(expected_type):
        transport.request("GET", "https://play.google.test")


def test_proxy_403_and_407_errors_are_retryable_only_after_egress_change() -> None:
    pool = _pool(["http://user:password@proxy.test:8080"])
    lease = pool.acquire("app")

    for status, expected_type in (
        (403, AccessForbidden),
        (407, ProxyAuthenticationFailure),
    ):
        transport = CurlCffiTransport(
            NetworkContext("app", lease),
            CountingLimiter(),
            timeout_seconds=20,
            session_factory=lambda status=status: FakeSession([FakeResponse(status)]),
        )
        with pytest.raises(expected_type) as captured:
            transport.request("GET", "https://play.google.test")
        assert captured.value.retry_with_new_egress is True
        assert "password" not in str(captured.value)
        assert "password" not in repr(captured.value)

    direct_transport = CurlCffiTransport(
        NetworkContext("direct", ProxyLease("direct")),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: FakeSession([FakeResponse(403)]),
    )
    with pytest.raises(AccessForbidden) as direct:
        direct_transport.request("GET", "https://play.google.test")
    assert direct.value.retry_with_new_egress is False


def test_503_retry_after_is_preserved() -> None:
    transport = CurlCffiTransport(
        NetworkContext("app", ProxyLease("app")),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: FakeSession([FakeResponse(503, headers={"Retry-After": "9"})]),
    )

    with pytest.raises(UpstreamFailure) as captured:
        transport.request("GET", "https://play.google.test")

    assert captured.value.retry_after_seconds == 9


def test_local_rate_limit_wait_failure_has_no_network_side_effects() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(
        0.01,
        1,
        clock=clock,
        maximum_wait_seconds=1,
    )
    limiter.acquire()
    pool = _pool(["http://user:password@proxy.test:8080"], threshold=1)
    provider = PoolProxyProvider(pool)
    lease = provider.acquire("app")
    session = FakeSession([FakeResponse(200)])
    transport = CurlCffiTransport(
        NetworkContext("app", lease),
        limiter,
        timeout_seconds=20,
        session_factory=lambda: session,
    )

    with pytest.raises(LocalRateLimitWaitExceeded) as captured:
        transport.request("GET", "https://play.google.test")

    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    circuit = CircuitBreaker(1, 10, clock=clock)
    assert policy.handle_failure(captured.value, lease, "app") is lease
    circuit.record_failure(captured.value, proxied=True)
    assert circuit.state == CircuitState.CLOSED
    assert AdapterFallbackPolicy().should_use_secondary(captured.value) is False
    assert lease.endpoint is not None
    assert lease.endpoint.state == ProxyState.HEALTHY
    assert lease.endpoint.failure_count == 0
    assert session.requests == []


def test_each_failed_physical_attempt_consumes_a_new_token_and_is_not_refunded() -> None:
    session = FakeSession([TimeoutError("timeout"), FakeResponse(200)])
    limiter = CountingLimiter()
    transport = CurlCffiTransport(
        NetworkContext("app", ProxyLease("app")),
        limiter,
        timeout_seconds=20,
        session_factory=lambda: session,
    )

    with pytest.raises(NetworkTimeout):
        transport.request("GET", "https://play.google.test")
    assert transport.request("GET", "https://play.google.test").text == "body"
    assert limiter.calls == 2


def test_transport_applies_proxy_timeout_headers_body_and_closes() -> None:
    pool = _pool(["http://user:secret@proxy.test:8080"])
    lease = pool.acquire("app")
    session = FakeSession([FakeResponse(200)])
    transport = CurlCffiTransport(
        NetworkContext("app", lease),
        CountingLimiter(),
        timeout_seconds=12,
        session_factory=lambda: session,
    )

    transport.request(
        "POST",
        "https://play.google.test",
        headers={"x-test": "yes"},
        data="body",
        params={"hl": "en"},
    )
    transport.close()

    kwargs = session.requests[0][2]
    assert kwargs["timeout"] == 12
    assert kwargs["headers"] == {"x-test": "yes"}
    assert kwargs["data"] == "body"
    assert kwargs["params"] == {"hl": "en"}
    assert kwargs["proxy"].endswith("@proxy.test:8080")
    assert session.closed is True
