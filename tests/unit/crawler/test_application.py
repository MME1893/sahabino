from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from sahabino.crawler.application.client import ResilientPlayStoreClient
from sahabino.crawler.application.policies.adapter import AdapterFallbackPolicy
from sahabino.crawler.application.policies.network import NetworkPolicy
from sahabino.crawler.application.policies.retry import RetryPolicy
from sahabino.crawler.application.tasks import ApplicationCrawlCommand
from sahabino.crawler.domain.dto import (
    AdapterCapabilities,
    AppDetailsDTO,
    ApplicationRef,
    ReviewDTO,
    ReviewsDTO,
)
from sahabino.crawler.domain.errors import (
    AccessForbidden,
    AppNotFound,
    LocalRateLimitWaitExceeded,
    MessagingPublishFailure,
    NetworkTimeout,
    ParseFailure,
    ProxyAuthenticationFailure,
    RateLimited,
    UpstreamFailure,
)
from sahabino.crawler.domain.results import CrawlTaskStatus, CrawlTaskType
from sahabino.crawler.infrastructure.adapters.classifier import ErrorClassifier
from sahabino.crawler.infrastructure.proxy.models import ProxyState
from sahabino.crawler.infrastructure.proxy.pool import ProxyPool
from sahabino.crawler.infrastructure.proxy.providers import NoProxyProvider, PoolProxyProvider
from sahabino.crawler.infrastructure.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitState,
)

from .fakes import FakeClock

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


def details(source: str = "primary") -> AppDetailsDTO:
    return AppDetailsDTO(
        min_installs=100,
        score=4.5,
        ratings_count=90,
        reviews_count=40,
        store_updated_on=date(2026, 9, 5),
        version="1.0",
        ad_supported=False,
        collected_at=NOW,
        source_adapter=source,
    )


def reviews(source: str = "primary") -> ReviewsDTO:
    return ReviewsDTO(
        reviews=(
            ReviewDTO(
                external_review_id="review-1",
                source_at=NOW,
                author_name="Ada",
                thumbs_up_count=1,
                score=5,
                content="Excellent",
                position=1,
                observed_at=NOW,
                source_adapter=source,
            ),
        )
    )


class FakeAdapter:
    capabilities = AdapterCapabilities(True, True, True)

    def __init__(
        self,
        app_outcomes: list[AppDetailsDTO | Exception] | None = None,
        review_outcomes: list[ReviewsDTO | Exception] | None = None,
    ) -> None:
        self.app_outcomes = app_outcomes or [details()]
        self.review_outcomes = review_outcomes or [reviews()]
        self.app_calls = 0
        self.review_calls = 0
        self.closed = False

    def get_app(self, *_: object) -> AppDetailsDTO:
        self.app_calls += 1
        value = self.app_outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def get_reviews(self, *_: object) -> ReviewsDTO:
        self.review_calls += 1
        value = self.review_outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter = adapter
        self.direct_values: list[bool] = []
        self.proxy_ids: list[str | None] = []

    def create(self, _context_id: str, lease: object) -> FakeAdapter:
        self.direct_values.append(bool(lease.is_direct))
        self.proxy_ids.append(lease.proxy_id)
        return self.adapter


class MemoryLifecycle:
    def __init__(self, task_ids: dict[CrawlTaskType, UUID]) -> None:
        self.tasks = {
            task_id: {"status": CrawlTaskStatus.PENDING, "attempts": 0, "error": None}
            for task_id in task_ids.values()
        }

    @contextmanager
    def transaction(self) -> Any:
        yield self

    def begin_attempt(self, task_id: UUID) -> None:
        self.tasks[task_id]["status"] = CrawlTaskStatus.RUNNING
        self.tasks[task_id]["attempts"] += 1

    def mark_retrying(self, task_id: UUID) -> None:
        self.tasks[task_id]["status"] = CrawlTaskStatus.RETRYING

    def mark_task_succeeded(self, task_id: UUID) -> None:
        self.tasks[task_id]["status"] = CrawlTaskStatus.SUCCEEDED

    def mark_task_failed(self, task_id: UUID, error_code: str, error_message: str) -> None:
        status = self.tasks[task_id]["status"]
        if status in {CrawlTaskStatus.SUCCEEDED, CrawlTaskStatus.FAILED}:
            raise ValueError("terminal")
        self.tasks[task_id]["status"] = CrawlTaskStatus.FAILED
        self.tasks[task_id]["error"] = (error_code, error_message)

    def commit(self) -> None:
        return


class FakePublisher:
    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.app_events = 0
        self.review_events = 0

    def publish_app_stats(self, **_: object) -> None:
        if self.fail == "app":
            raise MessagingPublishFailure("app publish failed")
        if self.fail == "unexpected_app":
            raise RuntimeError("publisher programming failure")
        self.app_events += 1

    def publish_reviews(self, **_: object) -> None:
        if self.fail == "reviews":
            raise MessagingPublishFailure("review publish failed")
        self.review_events += 1

    def close(self) -> None:
        return


def _command(
    primary: FakeAdapter,
    *,
    secondary: FakeAdapter | None = None,
    publisher: FakePublisher | None = None,
    provider: object | None = None,
    circuit: CircuitBreaker | None = None,
    retry_attempts: int = 3,
) -> tuple[ApplicationCrawlCommand, MemoryLifecycle, FakeFactory, FakeAdapter]:
    task_ids = {
        CrawlTaskType.APP_DETAILS: uuid4(),
        CrawlTaskType.REVIEWS: uuid4(),
    }
    lifecycle = MemoryLifecycle(task_ids)
    factory = FakeFactory(primary)
    actual_provider = provider or NoProxyProvider()
    secondary = secondary or FakeAdapter(
        app_outcomes=[details("secondary")],
        review_outcomes=[reviews("secondary")],
    )
    clock = FakeClock()
    playstore = ResilientPlayStoreClient(
        proxy_provider=actual_provider,  # type: ignore[arg-type]
        primary_factory=factory,
        secondary_adapter=secondary,
        retry_policy=RetryPolicy(retry_attempts, 30, clock=clock, random_value=lambda: 0),
        network_policy=NetworkPolicy(actual_provider, rate_limit_rotate_after=2),  # type: ignore[arg-type]
        fallback_policy=AdapterFallbackPolicy(),
        circuit_breaker=circuit or CircuitBreaker(5, 60, clock=clock),
        classifier=ErrorClassifier(),
    )
    command = ApplicationCrawlCommand(
        playstore=playstore,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        publisher=publisher or FakePublisher(),
        language_code="en",
        country_code="us",
    )
    command.task_ids = task_ids  # type: ignore[attr-defined]
    return command, lifecycle, factory, secondary


def _execute(command: ApplicationCrawlCommand) -> None:
    command.execute(
        ApplicationRef(uuid4(), "Example", "com.example.app"),
        command.task_ids,  # type: ignore[attr-defined]
    )


def test_details_and_reviews_succeed_independently() -> None:
    command, lifecycle, _, _ = _command(FakeAdapter())

    _execute(command)

    assert {task["status"] for task in lifecycle.tasks.values()} == {CrawlTaskStatus.SUCCEEDED}


def test_details_failure_does_not_prevent_reviews() -> None:
    primary = FakeAdapter(app_outcomes=[AppNotFound("missing")])
    command, lifecycle, _, _ = _command(primary)

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    review_id = command.task_ids[CrawlTaskType.REVIEWS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.FAILED
    assert lifecycle.tasks[review_id]["status"] == CrawlTaskStatus.SUCCEEDED
    assert primary.review_calls == 1


def test_reviews_failure_preserves_details_success() -> None:
    primary = FakeAdapter(review_outcomes=[AppNotFound("missing")])
    command, lifecycle, _, _ = _command(primary)

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    review_id = command.task_ids[CrawlTaskType.REVIEWS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.SUCCEEDED
    assert lifecycle.tasks[review_id]["status"] == CrawlTaskStatus.FAILED


def test_parse_failure_uses_secondary_but_429_never_does() -> None:
    secondary = FakeAdapter()
    parse_primary = FakeAdapter(app_outcomes=[ParseFailure("parse")])
    command, lifecycle, _, _ = _command(parse_primary, secondary=secondary)
    _execute(command)
    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.SUCCEEDED
    assert lifecycle.tasks[app_id]["attempts"] == 2
    assert secondary.app_calls == 1

    throttled_secondary = FakeAdapter()
    throttle_primary = FakeAdapter(
        app_outcomes=[RateLimited("slow"), RateLimited("slow"), RateLimited("slow")]
    )
    command, lifecycle, _, _ = _command(throttle_primary, secondary=throttled_secondary)
    _execute(command)
    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.FAILED
    assert lifecycle.tasks[app_id]["attempts"] == 3
    assert throttled_secondary.app_calls == 0


def test_repeated_429_cooldown_is_honored_by_the_next_operation() -> None:
    pool = ProxyPool(
        ["http://one", "http://two"],
        failure_threshold=2,
        cooldown_seconds=10,
        direct_fallback=False,
        clock=FakeClock(),
    )
    provider = PoolProxyProvider(pool)
    secondary = FakeAdapter()
    command, lifecycle, factory, _ = _command(
        FakeAdapter(app_outcomes=[RateLimited("slow"), RateLimited("slow")]),
        secondary=secondary,
        provider=provider,
        retry_attempts=2,
    )

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.FAILED
    assert pool.endpoints[0].state == ProxyState.COOLDOWN
    assert factory.proxy_ids == ["proxy-1", "proxy-2"]
    assert secondary.app_calls == 0


def test_primary_retry_and_secondary_fallback_each_count_as_logical_attempts() -> None:
    retry_primary = FakeAdapter(app_outcomes=[NetworkTimeout("timeout"), details()])
    command, lifecycle, _, _ = _command(retry_primary)
    _execute(command)
    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["attempts"] == 2

    secondary = FakeAdapter(app_outcomes=[details("secondary")])
    fallback_primary = FakeAdapter(
        app_outcomes=[NetworkTimeout("one"), NetworkTimeout("two"), ParseFailure("parse")]
    )
    command, lifecycle, _, _ = _command(fallback_primary, secondary=secondary)
    _execute(command)
    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.SUCCEEDED
    assert lifecycle.tasks[app_id]["attempts"] == 4
    assert secondary.app_calls == 1


def test_proxy_403_rotates_without_secondary_and_direct_403_is_terminal() -> None:
    provider = PoolProxyProvider(
        ProxyPool(
            ["http://one", "http://two"],
            failure_threshold=2,
            cooldown_seconds=10,
            direct_fallback=False,
            clock=FakeClock(),
        )
    )
    secondary = FakeAdapter()
    primary = FakeAdapter(
        app_outcomes=[AccessForbidden("403", retry_with_new_egress=True), details()]
    )
    command, lifecycle, factory, _ = _command(
        primary,
        secondary=secondary,
        provider=provider,
    )

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["attempts"] == 2
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.SUCCEEDED
    assert factory.proxy_ids[:2] == ["proxy-1", "proxy-2"]
    assert secondary.app_calls == 0

    direct_secondary = FakeAdapter()
    direct_command, direct_lifecycle, _, _ = _command(
        FakeAdapter(app_outcomes=[AccessForbidden("403")]),
        secondary=direct_secondary,
    )
    _execute(direct_command)
    direct_id = direct_command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert direct_lifecycle.tasks[direct_id]["attempts"] == 1
    assert direct_lifecycle.tasks[direct_id]["status"] == CrawlTaskStatus.FAILED
    assert direct_secondary.app_calls == 0


def test_repeated_403_rotation_is_bounded_by_operation_attempts() -> None:
    provider = PoolProxyProvider(
        ProxyPool(
            ["http://one", "http://two", "http://three", "http://four"],
            failure_threshold=2,
            cooldown_seconds=10,
            direct_fallback=False,
            clock=FakeClock(),
        )
    )
    secondary = FakeAdapter()
    primary = FakeAdapter(
        app_outcomes=[
            AccessForbidden("403", retry_with_new_egress=True),
            AccessForbidden("403", retry_with_new_egress=True),
            AccessForbidden("403", retry_with_new_egress=True),
        ]
    )
    command, lifecycle, factory, _ = _command(
        primary,
        secondary=secondary,
        provider=provider,
    )

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["attempts"] == 3
    assert lifecycle.tasks[app_id]["status"] == CrawlTaskStatus.FAILED
    assert factory.proxy_ids[:3] == ["proxy-1", "proxy-2", "proxy-3"]
    assert "proxy-4" not in factory.proxy_ids[:3]
    assert secondary.app_calls == 0


def test_repeated_403_with_no_remaining_egress_preserves_access_error() -> None:
    provider = PoolProxyProvider(
        ProxyPool(
            ["http://one", "http://two"],
            failure_threshold=2,
            cooldown_seconds=10,
            direct_fallback=False,
            clock=FakeClock(),
        )
    )
    secondary = FakeAdapter()
    command, lifecycle, factory, _ = _command(
        FakeAdapter(
            app_outcomes=[
                AccessForbidden("403", retry_with_new_egress=True),
                AccessForbidden("403", retry_with_new_egress=True),
            ]
        ),
        secondary=secondary,
        provider=provider,
    )

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["attempts"] == 2
    assert lifecycle.tasks[app_id]["error"] == ("ACCESS_FORBIDDEN", "403")
    assert factory.proxy_ids == ["proxy-1", "proxy-2"]
    assert secondary.app_calls == 0


def test_proxy_407_is_unhealthy_rotates_and_does_not_open_circuit() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(1, 60, clock=clock)
    pool = ProxyPool(
        ["http://one", "http://two"],
        failure_threshold=2,
        cooldown_seconds=10,
        direct_fallback=False,
        clock=clock,
    )
    provider = PoolProxyProvider(pool)
    secondary = FakeAdapter()
    command, lifecycle, factory, _ = _command(
        FakeAdapter(
            app_outcomes=[
                ProxyAuthenticationFailure("407", retry_with_new_egress=True),
                details(),
            ]
        ),
        secondary=secondary,
        provider=provider,
        circuit=circuit,
    )

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["attempts"] == 2
    assert factory.proxy_ids[:2] == ["proxy-1", "proxy-2"]
    assert pool.endpoints[0].state == ProxyState.UNHEALTHY
    assert circuit.state == CircuitState.CLOSED
    assert secondary.app_calls == 0


def test_persisted_task_error_redacts_proxy_credentials() -> None:
    password = "task-secret-password"
    command, lifecycle, _, _ = _command(
        FakeAdapter(
            app_outcomes=[
                AccessForbidden(f"rejected via http://fake-user:{password}@proxy.example:8080/path")
            ]
        )
    )

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    error = lifecycle.tasks[app_id]["error"]
    assert error is not None
    assert password not in repr(error)
    assert "fake-user" not in repr(error)
    assert "proxy.example" not in repr(error)


def test_local_rate_limit_failure_changes_no_proxy_circuit_or_adapter_policy() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(1, 60, clock=clock)
    pool = ProxyPool(
        ["http://one", "http://two"],
        failure_threshold=1,
        cooldown_seconds=10,
        direct_fallback=False,
        clock=clock,
    )
    provider = PoolProxyProvider(pool)
    secondary = FakeAdapter()
    command, lifecycle, factory, _ = _command(
        FakeAdapter(app_outcomes=[LocalRateLimitWaitExceeded("local")]),
        secondary=secondary,
        provider=provider,
        circuit=circuit,
    )

    _execute(command)

    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["attempts"] == 1
    assert factory.proxy_ids[0] == "proxy-1"
    assert pool.endpoints[0].state == ProxyState.HEALTHY
    assert pool.endpoints[0].failure_count == 0
    assert circuit.state == CircuitState.CLOSED
    assert secondary.app_calls == 0


def test_open_circuit_fails_tasks_cleanly_without_attempts() -> None:
    clock = FakeClock()
    circuit = CircuitBreaker(1, 60, clock=clock)
    circuit.record_failure(UpstreamFailure("down"))
    command, lifecycle, _, _ = _command(FakeAdapter(), circuit=circuit)

    _execute(command)

    assert {task["status"] for task in lifecycle.tasks.values()} == {CrawlTaskStatus.FAILED}
    assert {task["attempts"] for task in lifecycle.tasks.values()} == {0}


def test_proxy_exhaustion_with_direct_fallback_uses_direct_primary() -> None:
    provider = PoolProxyProvider(
        ProxyPool(
            [],
            failure_threshold=1,
            cooldown_seconds=10,
            direct_fallback=True,
            clock=FakeClock(),
        )
    )
    command, lifecycle, factory, _ = _command(FakeAdapter(), provider=provider)

    _execute(command)

    assert factory.direct_values == [True]
    assert {task["status"] for task in lifecycle.tasks.values()} == {CrawlTaskStatus.SUCCEEDED}


@pytest.mark.parametrize("failure", ["app", "reviews"])
def test_kafka_failure_marks_corresponding_task_failed(failure: str) -> None:
    command, lifecycle, _, _ = _command(FakeAdapter(), publisher=FakePublisher(fail=failure))

    _execute(command)

    task_type = CrawlTaskType.APP_DETAILS if failure == "app" else CrawlTaskType.REVIEWS
    task_id = command.task_ids[task_type]  # type: ignore[attr-defined]
    assert lifecycle.tasks[task_id]["status"] == CrawlTaskStatus.FAILED
    assert lifecycle.tasks[task_id]["error"][0] == "KAFKA_PUBLISH_FAILED"


def test_unexpected_application_error_is_visible_and_open_tasks_are_finalized() -> None:
    command, lifecycle, _, _ = _command(
        FakeAdapter(),
        publisher=FakePublisher(fail="unexpected_app"),
    )

    with pytest.raises(RuntimeError, match="publisher programming failure"):
        _execute(command)

    assert {task["status"] for task in lifecycle.tasks.values()} == {CrawlTaskStatus.FAILED}
    app_id = command.task_ids[CrawlTaskType.APP_DETAILS]  # type: ignore[attr-defined]
    assert lifecycle.tasks[app_id]["error"][0] == "CRAWLER_ERROR"
