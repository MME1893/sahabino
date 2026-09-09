from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from sahabino.crawler.application.policies.retry import RetryPolicy
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    AppGone,
    AppNotFound,
    CircuitOpen,
    InvalidPackage,
    LegalRestriction,
    LocalRateLimitWaitExceeded,
    NetworkTimeout,
    ParseFailure,
    ProxyAuthenticationFailure,
    ProxyConnectionFailure,
    RateLimited,
    UpstreamFailure,
)
from sahabino.crawler.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
)
from sahabino.crawler.infrastructure.resilience.token_bucket import TokenBucketRateLimiter

from .fakes import FakeClock


def test_token_bucket_capacity_consumption_refill_and_wait() -> None:
    clock = FakeClock()
    bucket = TokenBucketRateLimiter(1.0, 2, clock=clock)

    bucket.acquire()
    bucket.acquire()
    assert bucket.available_tokens == 0

    bucket.acquire()

    assert clock.sleeps == [1.0]
    assert bucket.available_tokens == 0
    clock.advance(0.5)
    assert bucket.available_tokens == pytest.approx(0.5)


def test_token_bucket_ignores_backward_monotonic_movement() -> None:
    clock = FakeClock(5)
    bucket = TokenBucketRateLimiter(1.0, 1, clock=clock)
    bucket.acquire()
    clock.value = 1

    assert bucket.available_tokens == 0


def test_token_bucket_is_thread_safe_for_initial_burst() -> None:
    clock = FakeClock()
    capacity = 20
    bucket = TokenBucketRateLimiter(1.0, capacity, clock=clock)
    barrier = Barrier(capacity)

    def acquire() -> None:
        barrier.wait()
        bucket.acquire()

    with ThreadPoolExecutor(max_workers=capacity) as executor:
        list(executor.map(lambda _: acquire(), range(capacity)))

    assert bucket.available_tokens == 0
    assert clock.sleeps == []


def test_circuit_breaker_full_state_machine() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(2, 10, clock=clock)

    circuit.record_failure(UpstreamFailure("one"))
    circuit.record_failure(UpstreamFailure("two"))
    assert circuit.state == CircuitState.OPEN
    with pytest.raises(CircuitOpen):
        circuit.before_call()

    clock.advance(10)
    circuit.before_call()
    assert circuit.state == CircuitState.HALF_OPEN
    circuit.record_success()
    assert circuit.state == CircuitState.CLOSED

    circuit.record_failure(UpstreamFailure("one"))
    circuit.record_failure(UpstreamFailure("two"))
    clock.advance(10)
    circuit.before_call()
    circuit.record_failure(NetworkTimeout("probe"))
    assert circuit.state == CircuitState.OPEN


@pytest.mark.parametrize(
    "error",
    [
        ProxyConnectionFailure("proxy"),
        ProxyAuthenticationFailure("proxy auth"),
        LocalRateLimitWaitExceeded("local wait"),
        ParseFailure("parse"),
        InvalidPackage("bad"),
        AppNotFound("missing"),
        AppGone("gone"),
        LegalRestriction("legal"),
    ],
)
def test_non_upstream_failures_do_not_open_circuit(error: Exception) -> None:
    circuit = CircuitBreaker(1, 10, clock=FakeClock())

    circuit.record_failure(error)

    assert circuit.state == CircuitState.CLOSED


def test_proxied_timeout_does_not_open_global_upstream_circuit() -> None:
    circuit = CircuitBreaker(1, 10, clock=FakeClock())

    circuit.record_failure(NetworkTimeout("timeout"), proxied=True)

    assert circuit.state == CircuitState.CLOSED


def test_access_forbidden_counts_only_for_direct_egress() -> None:
    proxied = CircuitBreaker(1, 10, clock=FakeClock())
    direct = CircuitBreaker(1, 10, clock=FakeClock())

    proxied.record_failure(AccessForbidden("403"), proxied=True)
    direct.record_failure(AccessForbidden("403"), proxied=False)

    assert proxied.state == CircuitState.CLOSED
    assert direct.state == CircuitState.OPEN


def test_retry_is_bounded_and_uses_exponential_jitter() -> None:
    clock = FakeClock()
    policy = RetryPolicy(3, 30, clock=clock, random_value=lambda: 0.25)
    attempts: list[int] = []

    def operation(attempt: int) -> None:
        attempts.append(attempt)
        raise NetworkTimeout("timeout")

    with pytest.raises(NetworkTimeout):
        policy.execute(operation)

    assert attempts == [1, 2, 3]
    assert clock.sleeps == [1.25, 2.25]


def test_retry_after_takes_precedence_and_parse_does_not_retry() -> None:
    clock = FakeClock()
    policy = RetryPolicy(3, 30, clock=clock, random_value=lambda: 0)
    attempts = 0

    def throttled(_: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimited("slow", retry_after_seconds=7)

    policy.execute(throttled)
    assert clock.sleeps == [7]

    with pytest.raises(ParseFailure):
        policy.execute(lambda _: (_ for _ in ()).throw(ParseFailure("bad")))
    assert attempts == 2


def test_503_retry_after_and_egress_change_retry_eligibility() -> None:
    clock = FakeClock()
    policy = RetryPolicy(2, 30, clock=clock, random_value=lambda: 0)
    attempts = 0

    def unavailable(_: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise UpstreamFailure("503", retry_after_seconds=11)

    policy.execute(unavailable)

    assert clock.sleeps == [11]
    assert policy.is_retryable(AccessForbidden("proxy 403", retry_with_new_egress=True))
    assert not policy.is_retryable(AccessForbidden("direct 403"))
    assert policy.is_retryable(ProxyAuthenticationFailure("proxy 407", retry_with_new_egress=True))
    assert not policy.is_retryable(ProxyAuthenticationFailure("direct 407"))
    assert not policy.is_retryable(LocalRateLimitWaitExceeded("local"))


def test_5xx_and_timeout_are_retryable_but_not_404() -> None:
    policy = RetryPolicy(2, 5, clock=FakeClock())

    assert policy.is_retryable(UpstreamFailure("500")) is True
    assert policy.is_retryable(NetworkTimeout("timeout")) is True
    assert policy.is_retryable(AppNotFound("404")) is False
