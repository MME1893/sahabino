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
    TemporaryConnectionFailure,
    UpstreamFailure,
)
from sahabino.crawler.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
)
from sahabino.crawler.infrastructure.resilience.token_bucket import (
    NoOpRateLimiter,
    TokenBucketRateLimiter,
)

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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"refill_per_second": 0, "capacity": 1},
        {"refill_per_second": -1, "capacity": 1},
        {"refill_per_second": 1, "capacity": 0},
        {"refill_per_second": 1, "capacity": -1},
        {"refill_per_second": 1, "capacity": 1, "maximum_wait_seconds": 0},
        {"refill_per_second": 1, "capacity": 1, "maximum_wait_seconds": -1},
    ],
)
def test_token_bucket_rejects_non_positive_configuration(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(clock=FakeClock(), **kwargs)  # type: ignore[arg-type]


def test_token_bucket_refill_is_capped_at_capacity_after_long_idle_time() -> None:
    clock = FakeClock()
    bucket = TokenBucketRateLimiter(2, 3, clock=clock)
    bucket.acquire()

    clock.advance(10_000)

    assert bucket.available_tokens == 3


def test_token_bucket_allows_wait_exactly_equal_to_deadline() -> None:
    clock = FakeClock()
    bucket = TokenBucketRateLimiter(1, 1, clock=clock, maximum_wait_seconds=1)
    bucket.acquire()

    bucket.acquire()

    assert clock.sleeps == [1]


def test_token_bucket_rejects_wait_beyond_deadline_without_sleeping() -> None:
    clock = FakeClock()
    bucket = TokenBucketRateLimiter(0.5, 1, clock=clock, maximum_wait_seconds=1)
    bucket.acquire()

    with pytest.raises(LocalRateLimitWaitExceeded):
        bucket.acquire()

    assert clock.sleeps == []


def test_noop_rate_limiter_returns_immediately_without_state() -> None:
    limiter = NoOpRateLimiter()

    assert limiter.acquire() is None
    assert vars(limiter) == {}


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


def test_circuit_success_resets_consecutive_relevant_failures() -> None:
    circuit = CircuitBreaker(3, 10, clock=FakeClock())

    circuit.record_failure(UpstreamFailure("one"))
    circuit.record_failure(UpstreamFailure("two"))
    circuit.record_success()
    circuit.record_failure(UpstreamFailure("three"))
    circuit.record_failure(UpstreamFailure("four"))

    assert circuit.state == CircuitState.CLOSED


def test_disabled_circuit_does_not_gate_or_account_for_failures() -> None:
    circuit = CircuitBreaker(1, 10, enabled=False, clock=FakeClock())

    assert circuit.before_call() is None
    circuit.record_failure(UpstreamFailure("down"))
    circuit.record_success()

    assert circuit.state == CircuitState.CLOSED


def test_exactly_one_concurrent_half_open_probe_is_permitted() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(1, 10, clock=clock)
    circuit.record_failure(UpstreamFailure("open"))
    clock.advance(10)
    callers = 12
    barrier = Barrier(callers)

    def enter() -> bool:
        barrier.wait()
        try:
            circuit.before_call()
        except CircuitOpen:
            return False
        return True

    with ThreadPoolExecutor(max_workers=callers) as executor:
        permitted = list(executor.map(lambda _: enter(), range(callers)))

    assert permitted.count(True) == 1
    assert permitted.count(False) == callers - 1
    assert circuit.state == CircuitState.HALF_OPEN


def test_irrelevant_half_open_failure_releases_probe_for_next_caller() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(1, 10, clock=clock)
    circuit.record_failure(UpstreamFailure("open"))
    clock.advance(10)
    token = circuit.before_call()

    circuit.record_failure(ParseFailure("bad payload"), call_token=token)

    assert circuit.state == CircuitState.HALF_OPEN
    assert circuit.before_call() is not None


def test_relevant_half_open_failure_reopens_immediately_below_normal_threshold() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(3, 10, clock=clock)
    for _ in range(3):
        circuit.record_failure(UpstreamFailure("open"))
    clock.advance(10)
    token = circuit.before_call()

    circuit.record_failure(RateLimited("probe throttled"), call_token=token)

    assert circuit.state == CircuitState.OPEN


@pytest.mark.parametrize(
    ("error", "proxied", "expected_state"),
    [
        (AccessForbidden("proxy 403"), True, CircuitState.CLOSED),
        (NetworkTimeout("proxy timeout"), True, CircuitState.CLOSED),
        (TemporaryConnectionFailure("proxy network"), True, CircuitState.CLOSED),
        (ProxyConnectionFailure("proxy"), True, CircuitState.CLOSED),
        (ProxyAuthenticationFailure("proxy auth"), True, CircuitState.CLOSED),
        (AccessForbidden("direct 403"), False, CircuitState.OPEN),
        (NetworkTimeout("direct timeout"), False, CircuitState.OPEN),
        (TemporaryConnectionFailure("direct network"), False, CircuitState.OPEN),
        (UpstreamFailure("proxy upstream"), True, CircuitState.OPEN),
        (RateLimited("proxy 429"), True, CircuitState.OPEN),
    ],
)
def test_circuit_failure_relevance_matrix(
    error: Exception,
    proxied: bool,
    expected_state: CircuitState,
) -> None:
    circuit = CircuitBreaker(1, 10, clock=FakeClock())

    circuit.record_failure(error, proxied=proxied)

    assert circuit.state == expected_state


def test_stale_inflight_success_cannot_close_newly_opened_circuit() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(1, 10, clock=clock)
    stale_call = circuit.before_call()
    opening_call = circuit.before_call()

    circuit.record_failure(UpstreamFailure("down"), call_token=opening_call)
    circuit.record_success(call_token=stale_call)

    assert circuit.state == CircuitState.OPEN
    with pytest.raises(CircuitOpen):
        circuit.before_call()


def test_stale_inflight_failure_cannot_extend_newly_opened_cooldown() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(1, 10, clock=clock)
    stale_call = circuit.before_call()
    opening_call = circuit.before_call()
    circuit.record_failure(UpstreamFailure("down"), call_token=opening_call)
    clock.advance(5)

    circuit.record_failure(UpstreamFailure("stale"), call_token=stale_call)
    clock.advance(5)

    assert circuit.before_call() is not None
    assert circuit.state == CircuitState.HALF_OPEN


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


def test_retry_after_may_exceed_local_backoff_cap() -> None:
    policy = RetryPolicy(2, 30, clock=FakeClock(), random_value=lambda: 0)

    assert policy.delay_for(8, RateLimited("slow", retry_after_seconds=120)) == 120


def test_retry_after_never_shortens_local_backoff() -> None:
    policy = RetryPolicy(2, 30, clock=FakeClock(), random_value=lambda: 0)

    assert policy.delay_for(4, RateLimited("slow", retry_after_seconds=2)) == 8


def test_before_retry_runs_only_when_another_attempt_will_occur() -> None:
    policy = RetryPolicy(3, 30, clock=FakeClock(), random_value=lambda: 0)
    retry_notifications: list[BaseException] = []

    with pytest.raises(NetworkTimeout):
        policy.execute(
            lambda _: (_ for _ in ()).throw(NetworkTimeout("timeout")),
            before_retry=retry_notifications.append,
        )

    assert len(retry_notifications) == 2

    retry_notifications.clear()
    with pytest.raises(ParseFailure):
        policy.execute(
            lambda _: (_ for _ in ()).throw(ParseFailure("parse")),
            before_retry=retry_notifications.append,
        )

    assert retry_notifications == []


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
