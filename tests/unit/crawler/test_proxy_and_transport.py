from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest

from sahabino.crawler.application.policies.adapter import AdapterFallbackPolicy
from sahabino.crawler.application.policies.network import NetworkPolicy
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    AdapterFailure,
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
    TemporaryConnectionFailure,
    UpstreamFailure,
)
from sahabino.crawler.infrastructure.adapters import factory as factory_module
from sahabino.crawler.infrastructure.adapters.classifier import ErrorClassifier
from sahabino.crawler.infrastructure.adapters.factory import PrimaryAdapterFactory
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
        self.close_calls = 0

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class SecretCheckingSession(FakeSession):
    def __init__(self, responses: list[FakeResponse | Exception], expected_proxy: str) -> None:
        super().__init__(responses)
        self._expected_proxy = expected_proxy
        self.proxy_matched = False

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.proxy_matched = kwargs.get("proxy") == self._expected_proxy
        if "proxy" in kwargs:
            kwargs["proxy"] = "[redacted proxy]"
        return super().request(method, url, **kwargs)


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


def test_no_proxy_provider_reuses_one_active_direct_lease_per_context() -> None:
    provider = NoProxyProvider()

    first = provider.acquire("app")
    second = provider.acquire("app")

    assert first is second
    assert first.is_direct is True


def test_no_proxy_provider_release_then_acquire_creates_a_new_direct_lease() -> None:
    provider = NoProxyProvider()
    first = provider.acquire("app")

    provider.release(first)
    second = provider.acquire("app")

    assert first.released is True
    assert second is not first
    assert second.released is False
    assert second.is_direct is True


def test_no_proxy_provider_rotation_releases_and_replaces_direct_lease() -> None:
    provider = NoProxyProvider()
    first = provider.acquire("app")

    second = provider.rotate(first, "app")

    assert first.released is True
    assert second is not first
    assert second.is_direct is True
    assert provider.is_usable(second) is True


def test_no_proxy_provider_concurrent_same_context_acquire_is_sticky() -> None:
    provider = NoProxyProvider()
    callers = 16
    barrier = Barrier(callers)

    def acquire() -> ProxyLease:
        barrier.wait()
        return provider.acquire("shared-app")

    with ThreadPoolExecutor(max_workers=callers) as executor:
        leases = list(executor.map(lambda _: acquire(), range(callers)))

    assert all(lease is leases[0] for lease in leases)
    assert leases[0].released is False


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


def test_proxy_pool_same_context_lease_has_single_owner_lifecycle_semantics() -> None:
    """Characterize the one-application-context invariant: overlapping owners share a lease."""
    pool = _pool(["http://one", "http://two"])
    consumer_a = pool.acquire("app")
    consumer_b = pool.acquire("app")

    pool.release(consumer_a)

    assert consumer_b is consumer_a
    assert consumer_b.released is True
    replacement = pool.acquire("app")
    assert replacement is not consumer_b
    assert replacement.released is False


def test_proxy_pool_failed_rotation_is_destructive_and_does_not_restore_old_lease() -> None:
    pool = _pool(["http://one"], direct=False)
    old_lease = pool.acquire("app")

    with pytest.raises(ProxyUnavailable):
        pool.rotate(old_lease, "app")

    assert old_lease.released is True
    replacement = pool.acquire("app")
    assert replacement is not old_lease
    assert replacement.proxy_id == old_lease.proxy_id


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


def test_rate_limit_counter_is_cleared_by_success_before_threshold() -> None:
    provider = PoolProxyProvider(_pool(["http://one", "http://two"], threshold=2))
    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    lease = provider.acquire("app")

    assert policy.handle_failure(RateLimited("first"), lease, "app") is lease
    policy.record_success(lease)
    assert policy.handle_failure(RateLimited("after success"), lease, "app") is lease
    rotated = policy.handle_failure(RateLimited("threshold"), lease, "app")

    assert rotated is not lease


def test_direct_rate_limit_identity_is_cleared_by_success() -> None:
    provider = NoProxyProvider()
    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    lease = provider.acquire("app")

    policy.handle_failure(RateLimited("first"), lease, "app")
    policy.record_success(lease)
    policy.handle_failure(RateLimited("after success"), lease, "app")

    assert policy._rate_limits == {"direct": 1}  # type: ignore[attr-defined]


def test_rate_limit_does_not_increment_generic_proxy_failure_accounting() -> None:
    provider = PoolProxyProvider(_pool(["http://one", "http://two"], threshold=1))
    policy = NetworkPolicy(provider, rate_limit_rotate_after=2)
    lease = provider.acquire("app")

    policy.handle_failure(RateLimited("first"), lease, "app")

    assert lease.endpoint is not None
    assert lease.endpoint.failure_count == 0
    assert lease.endpoint.state == ProxyState.HEALTHY


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
    proxy_url = lease.secret_url()
    assert proxy_url is not None
    session = SecretCheckingSession([FakeResponse(200)], proxy_url)
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
    assert "proxy" in kwargs
    assert session.proxy_matched is True
    assert session.closed is True


@pytest.mark.parametrize(
    ("proxied", "error", "expected_type"),
    [
        (False, TimeoutError("deadline"), NetworkTimeout),
        (True, RuntimeError("connect failed"), ProxyConnectionFailure),
        (True, RuntimeError("resolve failed"), ProxyConnectionFailure),
        (False, RuntimeError("connect failed"), TemporaryConnectionFailure),
        (False, RuntimeError("network failed"), TemporaryConnectionFailure),
        (False, RuntimeError("ordinary transport failure"), TemporaryConnectionFailure),
    ],
)
def test_transport_exception_translation_matrix(
    proxied: bool,
    error: Exception,
    expected_type: type[Exception],
) -> None:
    lease = _pool(["http://proxy.test:8080"]).acquire("app") if proxied else ProxyLease("app")
    transport = CurlCffiTransport(
        NetworkContext("app", lease),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: FakeSession([error]),
    )

    with pytest.raises(expected_type):
        transport.request("GET", "https://play.google.test")


def test_proxy_timeout_taxonomy_is_characterized_at_both_boundaries() -> None:
    lease = _pool(["http://proxy.test:8080"]).acquire("app")
    transport = CurlCffiTransport(
        NetworkContext("app", lease),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: FakeSession([RuntimeError("proxy timeout")]),
    )

    with pytest.raises(NetworkTimeout):
        transport.request("GET", "https://play.google.test")

    assert isinstance(
        ErrorClassifier().classify(RuntimeError("proxy timeout")), ProxyConnectionFailure
    )


def test_direct_transport_does_not_pass_a_proxy_argument() -> None:
    session = FakeSession([FakeResponse(200)])
    transport = CurlCffiTransport(
        NetworkContext("direct", ProxyLease("direct")),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: session,
    )

    transport.request("GET", "https://play.google.test")

    assert "proxy" not in session.requests[0][2]


def test_transport_close_is_idempotent_and_requests_after_close_fail() -> None:
    session = FakeSession([FakeResponse(200)])
    transport = CurlCffiTransport(
        NetworkContext("app", ProxyLease("app")),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: session,
    )

    transport.close()
    transport.close()

    assert session.close_calls == 1
    with pytest.raises(AdapterFailure, match="closed"):
        transport.request("GET", "https://play.google.test")
    assert session.requests == []


def test_transport_preserves_existing_crawler_error_instance() -> None:
    original = RateLimited("already classified", retry_after_seconds=12)
    transport = CurlCffiTransport(
        NetworkContext("app", ProxyLease("app")),
        CountingLimiter(),
        timeout_seconds=20,
        session_factory=lambda: FakeSession([original]),
    )

    with pytest.raises(RateLimited) as captured:
        transport.request("GET", "https://play.google.test")

    assert captured.value is original


def test_rebuilt_primary_transports_share_one_global_limiter_after_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = CountingLimiter()
    captured_limiters: list[object] = []

    class RecordingTransport:
        def __init__(
            self,
            _context: NetworkContext,
            actual_limiter: object,
            *,
            timeout_seconds: float,
        ) -> None:
            assert timeout_seconds == 20
            captured_limiters.append(actual_limiter)

    class RecordingClient:
        @classmethod
        def from_gplay_config(cls, transport: object) -> object:
            return transport

    monkeypatch.setattr(factory_module, "ControlledGPlayHttpClient", RecordingClient)
    monkeypatch.setattr(factory_module, "GPlayScraperAdapter", lambda client: client)
    factory = PrimaryAdapterFactory(
        limiter,
        timeout_seconds=20,
        transport_factory=RecordingTransport,  # type: ignore[arg-type]
    )

    factory.create("app", ProxyLease("app"))
    factory.create("app", ProxyLease("app"))

    assert captured_limiters == [limiter, limiter]
