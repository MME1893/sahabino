from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import psycopg
import pytest
from confluent_kafka import Message
from sqlalchemy import select

from sahabino.crawler.application.client import ResilientPlayStoreClient
from sahabino.crawler.application.executor import CrawlerService
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
from sahabino.crawler.domain.errors import AppNotFound
from sahabino.crawler.domain.results import CrawlRunStatus, CrawlTaskStatus, TriggerType
from sahabino.crawler.infrastructure.adapters.classifier import ErrorClassifier
from sahabino.crawler.infrastructure.messaging.kafka import KafkaCollectedEventPublisher
from sahabino.crawler.infrastructure.persistence.models import CrawlRun, CrawlTask
from sahabino.crawler.infrastructure.persistence.repository import (
    SqlAlchemyLifecycleRepository,
)
from sahabino.crawler.infrastructure.proxy.providers import NoProxyProvider
from sahabino.crawler.infrastructure.resilience.circuit_breaker import CircuitBreaker
from sahabino.crawler.infrastructure.resilience.clock import SystemClock
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.producer import KafkaProducer

from .conftest import psycopg_dsn

NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)


class FakeRegistry:
    def __init__(self, applications: list[ApplicationRef]) -> None:
        self._applications = applications

    def list_active_applications(self) -> list[ApplicationRef]:
        return self._applications

    def close(self) -> None:
        return


class FakePlayStoreAdapter:
    capabilities = AdapterCapabilities(True, True, True)

    def __init__(self, failed_review_package: str | None = None) -> None:
        self._failed_review_package = failed_review_package

    def get_app(self, package_name: str, *_: object) -> AppDetailsDTO:
        return AppDetailsDTO(
            min_installs=100,
            score=4.5,
            ratings_count=90,
            reviews_count=40,
            store_updated_on=date(2026, 9, 5),
            version="1.0",
            ad_supported=False,
            collected_at=NOW,
            source_adapter="fake-primary",
        )

    def get_reviews(self, package_name: str, *_: object) -> ReviewsDTO:
        if package_name == self._failed_review_package:
            raise AppNotFound("reviews unavailable")
        return ReviewsDTO(
            reviews=(
                ReviewDTO(
                    external_review_id=f"review-{package_name}",
                    source_at=NOW,
                    author_name="Integration User",
                    thumbs_up_count=2,
                    score=5,
                    content="Useful",
                    position=1,
                    observed_at=NOW,
                    source_adapter="fake-primary",
                ),
            )
        )

    def close(self) -> None:
        return


class FakePrimaryFactory:
    def __init__(self, failed_review_package: str | None = None) -> None:
        self._failed_review_package = failed_review_package

    def create(self, _context_id: str, _lease: object) -> FakePlayStoreAdapter:
        return FakePlayStoreAdapter(self._failed_review_package)


def _applications(database_url: str, count: int = 2) -> list[ApplicationRef]:
    applications = [
        ApplicationRef(uuid4(), f"Application {index}", f"com.example.integration{index}")
        for index in range(1, count + 1)
    ]
    with psycopg.connect(psycopg_dsn(database_url), autocommit=True) as connection:
        for application in applications:
            connection.execute(
                "INSERT INTO applications (id, name, package_name, is_active) "
                "VALUES (%s, %s, %s, true)",
                (
                    application.application_id,
                    application.name,
                    application.package_name,
                ),
            )
    return applications


def _crawler(
    database_url: str,
    kafka_servers: str,
    applications: list[ApplicationRef],
    failed_review_package: str | None = None,
) -> tuple[CrawlerService, KafkaCollectedEventPublisher]:
    lifecycle = SqlAlchemyLifecycleRepository(create_sync_session_factory(database_url))
    provider = NoProxyProvider()
    clock = SystemClock()
    playstore = ResilientPlayStoreClient(
        proxy_provider=provider,
        primary_factory=FakePrimaryFactory(failed_review_package),
        secondary_adapter=FakePlayStoreAdapter(),
        retry_policy=RetryPolicy(2, 1, clock=clock, random_value=lambda: 0),
        network_policy=NetworkPolicy(provider, rate_limit_rotate_after=2),
        fallback_policy=AdapterFallbackPolicy(),
        circuit_breaker=CircuitBreaker(5, 60, clock=clock),
        classifier=ErrorClassifier(),
    )
    publisher = KafkaCollectedEventPublisher(KafkaProducer(kafka_servers, flush_timeout=15))
    command = ApplicationCrawlCommand(
        playstore=playstore,
        lifecycle=lifecycle,
        publisher=publisher,
        language_code="en",
        country_code="us",
    )
    return (
        CrawlerService(
            registry=FakeRegistry(applications),
            lifecycle=lifecycle,
            application_command=command,
            max_concurrent_apps=2,
            language_code="en",
            country_code="us",
            crawler_version="integration",
        ),
        publisher,
    )


def _poll_messages(consumer: KafkaConsumer, count: int) -> list[Message]:
    messages: list[Message] = []
    deadline = time.monotonic() + 30
    while len(messages) < count and time.monotonic() < deadline:
        message = consumer.poll(timeout=1)
        if message is not None:
            messages.append(message)
    if len(messages) != count:
        pytest.fail(f"expected {count} crawler events, received {len(messages)}")
    return messages


def _assert_database_state(
    database_url: str,
    run_id: UUID,
    expected_run_status: CrawlRunStatus,
    expected_task_statuses: list[CrawlTaskStatus],
) -> None:
    session_factory = create_sync_session_factory(database_url)
    with session_factory() as session:
        run = session.get(CrawlRun, run_id)
        tasks = session.scalars(select(CrawlTask).where(CrawlTask.crawl_run_id == run_id)).all()
        assert run is not None
        assert run.status == expected_run_status.value
        assert run.finished_at is not None
        assert sorted(task.status for task in tasks) == sorted(
            status.value for status in expected_task_statuses
        )


def test_real_postgres_and_kafka_complete_crawl_flow(
    crawler_database_url: str,
    crawler_kafka_bootstrap_servers: str,
    crawler_topics: tuple[str, str],
) -> None:
    applications = _applications(crawler_database_url)
    crawler, publisher = _crawler(
        crawler_database_url, crawler_kafka_bootstrap_servers, applications
    )

    run_id = crawler.crawl_once(TriggerType.MANUAL)
    publisher.close()

    _assert_database_state(
        crawler_database_url,
        run_id,
        CrawlRunStatus.SUCCEEDED,
        [CrawlTaskStatus.SUCCEEDED] * 4,
    )
    consumer = KafkaConsumer(
        crawler_kafka_bootstrap_servers,
        f"crawler-complete-{uuid4().hex}",
        list(crawler_topics),
    )
    try:
        messages = _poll_messages(consumer, 4)
    finally:
        consumer.close()
    event_types = {json.loads(message.value())["event_type"] for message in messages}
    assert event_types == {"playstore.app_stats.collected", "playstore.review.observed"}
    assert {message.key().decode() for message in messages} == {
        str(application.application_id) for application in applications
    }


def test_partial_failure_preserves_successful_events_and_task_results(
    crawler_database_url: str,
    crawler_kafka_bootstrap_servers: str,
    crawler_topics: tuple[str, str],
) -> None:
    applications = _applications(crawler_database_url)
    crawler, publisher = _crawler(
        crawler_database_url,
        crawler_kafka_bootstrap_servers,
        applications,
        failed_review_package=applications[1].package_name,
    )

    run_id = crawler.crawl_once(TriggerType.MANUAL)
    publisher.close()

    _assert_database_state(
        crawler_database_url,
        run_id,
        CrawlRunStatus.PARTIALLY_FAILED,
        [CrawlTaskStatus.SUCCEEDED] * 3 + [CrawlTaskStatus.FAILED],
    )
    consumer = KafkaConsumer(
        crawler_kafka_bootstrap_servers,
        f"crawler-partial-{uuid4().hex}",
        list(crawler_topics),
    )
    try:
        messages = _poll_messages(consumer, 3)
    finally:
        consumer.close()
    assert len(messages) == 3
    assert all(message.key() is not None for message in messages)
