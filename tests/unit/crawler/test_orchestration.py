from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from inspect import signature
from threading import Barrier, Event, Lock, Thread
from typing import Any
from uuid import UUID, uuid4

import pytest

from sahabino.common.config import Settings
from sahabino.crawler import crawl_once
from sahabino.crawler.application.executor import CrawlerService
from sahabino.crawler.application.tasks import ApplicationCrawlCommand
from sahabino.crawler.bootstrap import container as container_module
from sahabino.crawler.bootstrap.container import build_container
from sahabino.crawler.domain.dto import AppDetailsDTO, ApplicationRef, ReviewDTO, ReviewsDTO
from sahabino.crawler.domain.errors import MessagingPublishFailure
from sahabino.crawler.domain.results import CrawlRunStatus, CrawlTaskType, TriggerType
from sahabino.crawler.infrastructure.messaging.kafka import KafkaCollectedEventPublisher
from sahabino.crawler.infrastructure.registry.http import HttpApplicationRegistry
from sahabino.crawler.scheduler.scheduler import CrawlerScheduler
from sahabino.messaging.exceptions import ProducerDeliveryError
from sahabino.messaging.topics import (
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
)

from .fakes import FakeClock

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


class OrchestrationLifecycle:
    def __init__(
        self,
        *,
        create_tasks_error: Exception | None = None,
        finish_errors: list[Exception] | None = None,
    ) -> None:
        self.run_id = uuid4()
        self.finished: CrawlRunStatus | None | str = "not-called"
        self.finished_at_set = False
        self.create_tasks_error = create_tasks_error
        self.finish_errors = finish_errors or []

    @contextmanager
    def transaction(self) -> Any:
        yield self

    def create_run(self, *_: object, **__: object) -> UUID:
        return self.run_id

    def create_tasks(
        self,
        _run_id: UUID,
        applications: list[ApplicationRef],
        _language: str,
        _country: str,
    ) -> dict[tuple[UUID, CrawlTaskType], UUID]:
        if self.create_tasks_error is not None:
            raise self.create_tasks_error
        return {
            (application.application_id, task_type): uuid4()
            for application in applications
            for task_type in CrawlTaskType
        }

    def finish_run(self, _run_id: UUID, forced: CrawlRunStatus | None = None) -> None:
        if self.finish_errors:
            raise self.finish_errors.pop(0)
        self.finished = forced
        self.finished_at_set = True

    def commit(self) -> None:
        return


class FakeRegistry:
    def __init__(self, applications: list[ApplicationRef], error: Exception | None = None) -> None:
        self.applications = applications
        self.error = error

    def list_active_applications(self) -> list[ApplicationRef]:
        if self.error is not None:
            raise self.error
        return self.applications

    def close(self) -> None:
        return


class TrackingCommand:
    def __init__(self, expected_initial_workers: int) -> None:
        self._expected = expected_initial_workers
        self._release = Event()
        self._lock = Lock()
        self.active = 0
        self.maximum_active = 0
        self.entered = 0

    def execute(self, *_: object) -> None:
        with self._lock:
            self.active += 1
            self.entered += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.entered >= self._expected:
                self._release.set()
        self._release.wait(timeout=2)
        with self._lock:
            self.active -= 1


def _service(
    registry: FakeRegistry,
    lifecycle: OrchestrationLifecycle,
    command: object,
    workers: int = 2,
    executor_factory: object | None = None,
) -> CrawlerService:
    kwargs: dict[str, object] = {}
    if executor_factory is not None:
        kwargs["executor_factory"] = executor_factory
    return CrawlerService(
        registry=registry,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        application_command=command,  # type: ignore[arg-type]
        max_concurrent_apps=workers,
        language_code="en",
        country_code="us",
        **kwargs,  # type: ignore[arg-type]
    )


def test_application_concurrency_is_bounded_without_timing_assertions() -> None:
    applications = [
        ApplicationRef(uuid4(), f"App {index}", f"com.example.app{index}") for index in range(5)
    ]
    lifecycle = OrchestrationLifecycle()
    command = TrackingCommand(expected_initial_workers=2)

    _service(FakeRegistry(applications), lifecycle, command, workers=2).crawl_once(
        TriggerType.MANUAL
    )

    assert command.maximum_active == 2
    assert lifecycle.finished is None


def test_serial_executor_mode_never_runs_more_than_one_application() -> None:
    applications = [
        ApplicationRef(uuid4(), f"App {index}", f"com.example.serial{index}") for index in range(4)
    ]
    lifecycle = OrchestrationLifecycle()
    command = TrackingCommand(expected_initial_workers=1)

    _service(FakeRegistry(applications), lifecycle, command, workers=1).crawl_once(
        TriggerType.MANUAL
    )

    assert command.maximum_active == 1
    assert command.entered == len(applications)


def test_each_application_finishes_details_before_reviews_while_apps_overlap() -> None:
    applications = [
        ApplicationRef(uuid4(), f"App {index}", f"com.example.sequence{index}")
        for index in range(2)
    ]
    event_lock = Lock()
    both_details_started = Barrier(2)
    events: list[tuple[str, str]] = []

    class TaskLifecycle(OrchestrationLifecycle):
        def begin_attempt(self, _task_id: UUID) -> None:
            return

        def mark_retrying(self, _task_id: UUID) -> None:
            return

        def mark_task_succeeded(self, _task_id: UUID) -> None:
            return

        def mark_task_failed(self, *_: object) -> None:
            return

    class Client:
        def __init__(self, context_id: str) -> None:
            self.context_id = context_id

        def get_app(self, *_: object, hooks: Any) -> AppDetailsDTO:
            hooks.before_attempt(1)
            with event_lock:
                events.append((self.context_id, "details-started"))
            both_details_started.wait(timeout=2)
            with event_lock:
                events.append((self.context_id, "details-finished"))
            return AppDetailsDTO(
                min_installs=1,
                score=4,
                ratings_count=1,
                reviews_count=0,
                store_updated_on=None,
                version=None,
                ad_supported=False,
                collected_at=NOW,
                source_adapter="fake",
            )

        def get_reviews(self, *_: object, hooks: Any) -> ReviewsDTO:
            hooks.before_attempt(1)
            with event_lock:
                events.append((self.context_id, "reviews-started"))
            return ReviewsDTO(reviews=())

    class PlayStore:
        @contextmanager
        def open_application(self, context_id: str) -> Any:
            yield Client(context_id)

    class Publisher:
        def publish_app_stats(self, **_: object) -> None:
            return

        def publish_reviews(self, **_: object) -> None:
            return

    lifecycle = TaskLifecycle()
    command = ApplicationCrawlCommand(
        playstore=PlayStore(),  # type: ignore[arg-type]
        lifecycle=lifecycle,  # type: ignore[arg-type]
        publisher=Publisher(),  # type: ignore[arg-type]
        language_code="en",
        country_code="us",
    )

    _service(FakeRegistry(applications), lifecycle, command, workers=2).crawl_once(
        TriggerType.MANUAL
    )

    for application in applications:
        context_id = str(application.application_id)
        assert events.index((context_id, "details-finished")) < events.index(
            (context_id, "reviews-started")
        )


def test_unexpected_worker_failure_waits_for_active_worker_cleanup_and_fails_run() -> None:
    applications = [
        ApplicationRef(uuid4(), "Failing", "com.example.failing"),
        ApplicationRef(uuid4(), "Active", "com.example.active"),
    ]
    original = RuntimeError("unexpected worker failure")
    active_entered = Event()
    allow_active_to_finish = Event()
    active_completed = Event()
    resource_closed = Event()

    class CleanupCommand:
        def execute(self, application: ApplicationRef, *_: object) -> None:
            if application.name == "Failing":
                assert active_entered.wait(timeout=2)
                allow_active_to_finish.set()
                raise original
            active_entered.set()
            try:
                assert allow_active_to_finish.wait(timeout=2)
                active_completed.set()
            finally:
                resource_closed.set()

    lifecycle = OrchestrationLifecycle()

    with pytest.raises(RuntimeError) as captured:
        _service(
            FakeRegistry(applications),
            lifecycle,
            CleanupCommand(),
            workers=2,
        ).crawl_once(TriggerType.MANUAL)

    assert captured.value is original
    assert active_completed.is_set()
    assert resource_closed.is_set()
    assert lifecycle.finished == CrawlRunStatus.FAILED


def test_registry_failure_marks_run_failed_without_commands() -> None:
    lifecycle = OrchestrationLifecycle()
    command = TrackingCommand(expected_initial_workers=1)

    _service(FakeRegistry([], error=RuntimeError("registry down")), lifecycle, command).crawl_once(
        TriggerType.MANUAL
    )

    assert lifecycle.finished == CrawlRunStatus.FAILED
    assert command.entered == 0


def test_zero_active_applications_finishes_normally() -> None:
    lifecycle = OrchestrationLifecycle()

    _service(FakeRegistry([]), lifecycle, TrackingCommand(1)).crawl_once(TriggerType.MANUAL)

    assert lifecycle.finished is None


def test_task_creation_failure_marks_run_failed_and_preserves_original_error() -> None:
    original = RuntimeError("task creation failed")
    lifecycle = OrchestrationLifecycle(create_tasks_error=original)
    application = ApplicationRef(uuid4(), "Example", "com.example.app")

    with pytest.raises(RuntimeError) as captured:
        _service(FakeRegistry([application]), lifecycle, TrackingCommand(1)).crawl_once(
            TriggerType.MANUAL
        )

    assert captured.value is original
    assert lifecycle.finished == CrawlRunStatus.FAILED
    assert lifecycle.finished_at_set is True


def test_executor_setup_failure_marks_run_failed_and_preserves_original_error() -> None:
    original = RuntimeError("executor setup failed")
    lifecycle = OrchestrationLifecycle()
    application = ApplicationRef(uuid4(), "Example", "com.example.app")

    def broken_executor(**_: object) -> object:
        raise original

    with pytest.raises(RuntimeError) as captured:
        _service(
            FakeRegistry([application]),
            lifecycle,
            TrackingCommand(1),
            executor_factory=broken_executor,
        ).crawl_once(TriggerType.MANUAL)

    assert captured.value is original
    assert lifecycle.finished == CrawlRunStatus.FAILED
    assert lifecycle.finished_at_set is True


def test_future_failure_marks_run_failed_and_preserves_original_error() -> None:
    original = RuntimeError("future collection failed")
    lifecycle = OrchestrationLifecycle()
    application = ApplicationRef(uuid4(), "Example", "com.example.app")

    class FailingCommand:
        def execute(self, *_: object) -> None:
            raise original

    with pytest.raises(RuntimeError) as captured:
        _service(FakeRegistry([application]), lifecycle, FailingCommand()).crawl_once(
            TriggerType.MANUAL
        )

    assert captured.value is original
    assert lifecycle.finished == CrawlRunStatus.FAILED
    assert lifecycle.finished_at_set is True


def test_orchestration_and_finalization_errors_are_both_preserved() -> None:
    original = RuntimeError("task creation failed")
    finalization = RuntimeError("run finalization failed")
    lifecycle = OrchestrationLifecycle(
        create_tasks_error=original,
        finish_errors=[finalization],
    )
    application = ApplicationRef(uuid4(), "Example", "com.example.app")

    with pytest.raises(ExceptionGroup) as captured:
        _service(FakeRegistry([application]), lifecycle, TrackingCommand(1)).crawl_once(
            TriggerType.MANUAL
        )

    assert captured.value.exceptions == (original, finalization)


def test_crawler_container_does_not_provision_topics_by_default() -> None:
    assert signature(build_container).parameters["provision_topics"].default is False


class TriggerRecorder:
    def __init__(self, block: bool = False) -> None:
        self.calls: list[tuple[TriggerType, datetime | None]] = []
        self.entered = Event()
        self.release = Event()
        self.block = block

    def crawl_once(self, trigger: TriggerType, *, scheduled_for: datetime | None = None) -> UUID:
        self.calls.append((trigger, scheduled_for))
        self.entered.set()
        if self.block:
            self.release.wait(timeout=2)
        return uuid4()


class FakeSchedulerBackend:
    def __init__(self) -> None:
        self.job: tuple[object, str, dict[str, object]] | None = None
        self.started = False

    def add_job(self, function: object, trigger: str, **kwargs: object) -> object:
        self.job = (function, trigger, kwargs)
        return object()

    def start(self) -> None:
        self.started = True


def test_manual_and_scheduled_trigger_types_and_schedule_configuration() -> None:
    recorder = TriggerRecorder()
    crawl_once(recorder)  # type: ignore[arg-type]
    backend = FakeSchedulerBackend()
    scheduler = CrawlerScheduler(
        recorder,  # type: ignore[arg-type]
        interval_minutes=75,
        scheduler_factory=lambda: backend,
    )
    scheduler.run_scheduled_once()
    scheduler.start()

    assert recorder.calls[0] == (TriggerType.MANUAL, None)
    assert recorder.calls[1][0] == TriggerType.SCHEDULED
    assert recorder.calls[1][1] is not None
    assert backend.job is not None
    assert backend.job[1] == "interval"
    assert backend.job[2]["minutes"] == 75
    assert backend.job[2]["max_instances"] == 1
    assert backend.job[2]["coalesce"] is True
    assert backend.job[2]["next_run_time"] is not None


def test_overlapping_scheduled_runs_are_prevented() -> None:
    recorder = TriggerRecorder(block=True)
    scheduler = CrawlerScheduler(recorder, interval_minutes=60)  # type: ignore[arg-type]
    thread = Thread(target=scheduler.run_scheduled_once)
    thread.start()
    assert recorder.entered.wait(timeout=1)

    assert scheduler.run_scheduled_once() is None

    recorder.release.set()
    thread.join(timeout=1)
    assert len(recorder.calls) == 1


def test_scheduler_releases_overlap_lock_after_crawl_failure() -> None:
    class FailsOnceCrawler:
        def __init__(self) -> None:
            self.calls = 0

        def crawl_once(self, *_: object, **__: object) -> UUID:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("scheduled crawl failed")
            return uuid4()

    crawler = FailsOnceCrawler()
    scheduler = CrawlerScheduler(crawler, interval_minutes=60)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="scheduled crawl failed"):
        scheduler.run_scheduled_once()

    assert scheduler.run_scheduled_once() is not None
    assert crawler.calls == 2


class RegistryResponse:
    def __init__(self, status: int, body: object) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> object:
        return self._body


class RegistryClient:
    def __init__(self, responses: list[RegistryResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> RegistryResponse:
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        return


def test_registry_http_adapter_uses_active_endpoint_and_bounded_retry() -> None:
    application_id = uuid4()
    client = RegistryClient(
        [
            RegistryResponse(503, {}),
            RegistryResponse(
                200,
                [
                    {
                        "id": str(application_id),
                        "name": "Example",
                        "package_name": "com.example.app",
                        "ignored": "response internals",
                    }
                ],
            ),
        ]
    )
    clock = FakeClock()
    registry = HttpApplicationRegistry(
        "http://registry:8000",
        clock=clock,
        client_factory=lambda *_: client,
    )

    applications = registry.list_active_applications()

    assert applications == [ApplicationRef(application_id, "Example", "com.example.app")]
    assert client.calls == [
        ("/applications", {"params": {"active": "true"}}),
        ("/applications", {"params": {"active": "true"}}),
    ]
    assert clock.sleeps == [1]


class BatchProducer:
    def __init__(self) -> None:
        self.batches: list[tuple[object, ...]] = []
        self.closed = False

    def publish_batch(self, messages: object) -> None:
        self.batches.append(tuple(messages))  # type: ignore[arg-type]

    def close(self) -> None:
        self.closed = True


def test_kafka_publisher_uses_application_key_and_one_event_per_review() -> None:
    producer = BatchProducer()
    publisher = KafkaCollectedEventPublisher(producer)  # type: ignore[arg-type]
    application = ApplicationRef(uuid4(), "Example", "com.example.app")
    app_task_id = uuid4()
    review_task_id = uuid4()
    app_details = AppDetailsDTO(
        min_installs=10,
        score=4,
        ratings_count=8,
        reviews_count=2,
        store_updated_on=date(2026, 9, 5),
        version="1",
        ad_supported=False,
        collected_at=NOW,
        source_adapter="primary",
    )
    review_set = ReviewsDTO(
        reviews=tuple(
            ReviewDTO(
                external_review_id=f"r-{index}",
                source_at=NOW,
                author_name="Ada",
                thumbs_up_count=0,
                score=5,
                content="ok",
                position=index,
                observed_at=NOW,
                source_adapter="primary",
            )
            for index in (1, 2)
        )
    )

    publisher.publish_app_stats(
        crawl_task_id=app_task_id, application=application, details=app_details
    )
    publisher.publish_reviews(
        crawl_task_id=review_task_id, application=application, reviews=review_set
    )

    assert len(producer.batches[0]) == 1
    assert producer.batches[0][0].topic == PLAYSTORE_APP_STATS_TOPIC
    assert producer.batches[0][0].key == str(application.application_id)
    assert len(producer.batches[1]) == 2
    assert all(
        message.topic == PLAYSTORE_REVIEW_OBSERVED_TOPIC
        and message.key == str(application.application_id)
        for message in producer.batches[1]
    )
    assert [message.event.payload.position for message in producer.batches[1]] == [1, 2]
    assert {message.event.payload.crawl_task_id for message in producer.batches[1]} == {
        review_task_id
    }
    assert {message.event.payload.observed_at for message in producer.batches[1]} == {NOW}
    assert {message.event.payload.source_adapter for message in producer.batches[1]} == {"primary"}


def test_kafka_publisher_treats_empty_reviews_as_an_empty_successful_batch() -> None:
    producer = BatchProducer()
    publisher = KafkaCollectedEventPublisher(producer)  # type: ignore[arg-type]

    publisher.publish_reviews(
        crawl_task_id=uuid4(),
        application=ApplicationRef(uuid4(), "Example", "com.example.app"),
        reviews=ReviewsDTO(reviews=()),
    )

    assert producer.batches == [()]


def test_kafka_delivery_failure_is_translated_to_domain_publishing_error() -> None:
    class FailingProducer(BatchProducer):
        def publish_batch(self, messages: object) -> None:
            tuple(messages)  # type: ignore[arg-type]
            raise ProducerDeliveryError(["delivery callback failed"])

    publisher = KafkaCollectedEventPublisher(FailingProducer())  # type: ignore[arg-type]
    application = ApplicationRef(uuid4(), "Example", "com.example.app")
    details = AppDetailsDTO(
        min_installs=1,
        score=4,
        ratings_count=1,
        reviews_count=0,
        store_updated_on=None,
        version=None,
        ad_supported=False,
        collected_at=NOW,
        source_adapter="primary",
    )

    with pytest.raises(MessagingPublishFailure) as captured:
        publisher.publish_app_stats(
            crawl_task_id=uuid4(),
            application=application,
            details=details,
        )

    assert isinstance(captured.value.__cause__, ProducerDeliveryError)


def test_composition_root_selects_runtime_implementations_from_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: dict[str, list[Any]] = {
        "playstores": [],
        "limiters": [],
        "fallback_enabled": [],
        "circuit_enabled": [],
        "workers": [],
        "pools": [],
    }
    direct_provider = object()
    pooled_provider = object()
    no_op_limiter = object()
    token_limiter = object()

    class StubRegistry:
        def close(self) -> None:
            return

    class StubPublisher:
        def close(self) -> None:
            return

    class StubKafkaProducer:
        @classmethod
        def from_settings(cls, _settings: Settings) -> object:
            return object()

    def pool_factory(urls: list[str], **kwargs: object) -> object:
        captures["pools"].append((urls, kwargs))
        return object()

    def primary_factory(limiter: object, **_: object) -> object:
        captures["limiters"].append(limiter)
        return object()

    def fallback_factory(*, secondary_enabled: bool) -> object:
        captures["fallback_enabled"].append(secondary_enabled)
        return object()

    def circuit_factory(*_: object, enabled: bool, **__: object) -> object:
        captures["circuit_enabled"].append(enabled)
        return object()

    def playstore_factory(**kwargs: object) -> object:
        captures["playstores"].append(kwargs)
        return object()

    def crawler_factory(**kwargs: object) -> object:
        captures["workers"].append(kwargs["max_concurrent_apps"])
        return object()

    monkeypatch.setattr(container_module, "SystemClock", FakeClock)
    monkeypatch.setattr(container_module, "create_sync_session_factory", lambda _: object())
    monkeypatch.setattr(container_module, "SqlAlchemyLifecycleRepository", lambda _: object())
    monkeypatch.setattr(
        container_module, "HttpApplicationRegistry", lambda *_args, **_kw: StubRegistry()
    )
    monkeypatch.setattr(container_module, "KafkaProducer", StubKafkaProducer)
    monkeypatch.setattr(
        container_module,
        "KafkaCollectedEventPublisher",
        lambda _producer: StubPublisher(),
    )
    monkeypatch.setattr(container_module, "NoOpRateLimiter", lambda: no_op_limiter)
    monkeypatch.setattr(
        container_module, "TokenBucketRateLimiter", lambda *_a, **_kw: token_limiter
    )
    monkeypatch.setattr(container_module, "NoProxyProvider", lambda: direct_provider)
    monkeypatch.setattr(container_module, "ProxyPool", pool_factory)
    monkeypatch.setattr(container_module, "PoolProxyProvider", lambda _pool: pooled_provider)
    monkeypatch.setattr(container_module, "PrimaryAdapterFactory", primary_factory)
    monkeypatch.setattr(container_module, "GooglePlayScraperAdapter", lambda: object())
    monkeypatch.setattr(container_module, "AdapterFallbackPolicy", fallback_factory)
    monkeypatch.setattr(container_module, "CircuitBreaker", circuit_factory)
    monkeypatch.setattr(container_module, "ResilientPlayStoreClient", playstore_factory)
    monkeypatch.setattr(container_module, "CrawlerService", crawler_factory)

    disabled = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        playstore_proxy_enabled=False,
        playstore_rate_limit_enabled=False,
        playstore_secondary_adapter_enabled=False,
        playstore_circuit_breaker_enabled=False,
        playstore_max_concurrent_apps=1,
    )
    build_container(disabled)

    enabled_direct_fallback = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        playstore_proxy_enabled=True,
        playstore_proxy_urls=[],
        playstore_proxy_direct_fallback=True,
        playstore_rate_limit_enabled=True,
        playstore_secondary_adapter_enabled=True,
        playstore_circuit_breaker_enabled=True,
        playstore_max_concurrent_apps=3,
    )
    build_container(enabled_direct_fallback)

    assert captures["playstores"][0]["proxy_provider"] is direct_provider
    assert captures["playstores"][1]["proxy_provider"] is pooled_provider
    assert captures["limiters"] == [no_op_limiter, token_limiter]
    assert captures["fallback_enabled"] == [False, True]
    assert captures["circuit_enabled"] == [False, True]
    assert captures["workers"] == [1, 3]
    assert captures["pools"][0][0] == []
    assert captures["pools"][0][1]["direct_fallback"] is True
