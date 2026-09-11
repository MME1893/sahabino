from __future__ import annotations

import time
from collections import Counter
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select

from sahabino.crawler.domain.results import CrawlRunStatus, CrawlTaskStatus, TriggerType
from sahabino.crawler.infrastructure.persistence.models import CrawlRun, CrawlTask
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.ingestion.models import (
    IngestedEvent,
    PlaystoreAppSnapshot,
    Review,
    ReviewObservation,
)
from sahabino.ingestion.worker import IngestionWorker
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.topics import (
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
)
from tests.integration.crawler.test_crawler_flow import NOW, _applications, _crawler

EXPECTED_EVENT_COUNT = 4
POLL_DEADLINE_SECONDS = 30.0
COMMITTED_OFFSET_OBSERVATION_SECONDS = 2.0


def test_crawler_to_ingestion_full_flow(
    crawler_database_url: str,
    crawler_kafka_bootstrap_servers: str,
    crawler_topics: tuple[str, str],
) -> None:
    assert set(crawler_topics) == {
        PLAYSTORE_APP_STATS_TOPIC,
        PLAYSTORE_REVIEW_OBSERVED_TOPIC,
    }
    applications = _applications(crawler_database_url)
    application_ids = {application.application_id for application in applications}
    session_factory = create_sync_session_factory(crawler_database_url)
    crawler, publisher = _crawler(
        crawler_database_url,
        crawler_kafka_bootstrap_servers,
        applications,
    )

    try:
        run_id = crawler.crawl_once(TriggerType.MANUAL)
    finally:
        publisher.close()

    with session_factory() as session:
        run = session.get(CrawlRun, run_id)
        tasks = session.scalars(select(CrawlTask).where(CrawlTask.crawl_run_id == run_id)).all()

        assert run is not None
        assert run.trigger_type == TriggerType.MANUAL.value
        assert run.status == CrawlRunStatus.SUCCEEDED.value
        assert run.finished_at is not None
        assert len(tasks) == 4
        assert Counter(task.task_type for task in tasks) == {
            "app_details": 2,
            "reviews": 2,
        }
        assert all(task.status == CrawlTaskStatus.SUCCEEDED.value for task in tasks)
        task_ids = {(task.application_id, task.task_type): task.id for task in tasks}
        assert set(task_ids) == {
            (application_id, task_type)
            for application_id in application_ids
            for task_type in ("app_details", "reviews")
        }

    group_id = f"sahabino-full-flow-{uuid4().hex}"
    consumer = KafkaConsumer(
        crawler_kafka_bootstrap_servers,
        group_id=group_id,
        topics=crawler_topics,
    )
    worker = IngestionWorker(
        consumer=consumer,
        session_factory=session_factory,
        consumer_group=group_id,
    )
    processed = 0
    deadline = time.monotonic() + POLL_DEADLINE_SECONDS
    try:
        while processed < EXPECTED_EVENT_COUNT and time.monotonic() < deadline:
            processed += int(worker.process_next(timeout=1.0))
    finally:
        worker.close()

    assert processed == EXPECTED_EVENT_COUNT, (
        f"expected {EXPECTED_EVENT_COUNT} crawler events, processed {processed} before deadline"
    )

    with session_factory() as session:
        ingested_events = session.scalars(select(IngestedEvent)).all()
        snapshots = session.scalars(select(PlaystoreAppSnapshot)).all()
        reviews = session.scalars(select(Review)).all()
        observations = session.scalars(select(ReviewObservation)).all()

        assert len(ingested_events) == 4
        assert len(snapshots) == 2
        assert len(reviews) == 2
        assert len(observations) == 2

        assert all(event.event_id is not None for event in ingested_events)
        assert all(event.schema_version == 1 for event in ingested_events)
        assert all(event.partition >= 0 and event.offset >= 0 for event in ingested_events)
        assert Counter(event.topic for event in ingested_events) == {
            PLAYSTORE_APP_STATS_TOPIC: 2,
            PLAYSTORE_REVIEW_OBSERVED_TOPIC: 2,
        }
        assert Counter(event.event_type for event in ingested_events) == {
            "playstore.app_stats.collected": 2,
            "playstore.review.observed": 2,
        }

        snapshots_by_application = {snapshot.application_id: snapshot for snapshot in snapshots}
        reviews_by_application = {review.application_id: review for review in reviews}
        observations_by_review = {
            observation.review_id: observation for observation in observations
        }
        assert set(snapshots_by_application) == application_ids
        assert set(reviews_by_application) == application_ids

        for application in applications:
            snapshot = snapshots_by_application[application.application_id]
            assert snapshot.package_name == application.package_name
            assert snapshot.crawl_task_id == task_ids[(application.application_id, "app_details")]
            assert snapshot.collected_at == NOW
            assert snapshot.min_installs == 100
            assert snapshot.score == 4.5
            assert snapshot.ratings_count == 90
            assert snapshot.reviews_count == 40
            assert snapshot.store_updated_on == date(2026, 9, 5)
            assert snapshot.version == "1.0"
            assert snapshot.ad_supported is False
            assert snapshot.source_adapter == "fake-primary"

            review = reviews_by_application[application.application_id]
            assert review.external_review_id == f"review-{application.package_name}"
            assert review.source_at == NOW
            assert review.score == 5
            assert review.content == "Useful"
            assert review.thumbs_up_count == 2
            assert review.source_adapter == "fake-primary"
            assert review.first_observed_at == NOW
            assert review.last_observed_at == NOW

            observation = observations_by_review[review.id]
            assert observation.crawl_task_id == task_ids[(application.application_id, "reviews")]
            assert observation.observed_at == NOW
            assert observation.position == 1
            assert observation.score == 5
            assert observation.thumbs_up_count == 2
            assert observation.source_adapter == "fake-primary"

    resumed_consumer = KafkaConsumer(
        crawler_kafka_bootstrap_servers,
        group_id=group_id,
        topics=crawler_topics,
    )
    assignment_deadline = time.monotonic() + POLL_DEADLINE_SECONDS
    try:
        while time.monotonic() < assignment_deadline:
            if resumed_consumer.poll(timeout=0.5) is not None:
                pytest.fail("committed crawler event was delivered again to the same group")
            if resumed_consumer.assignment():
                break
        else:
            pytest.fail("timed out waiting for resumed full-flow consumer assignment")

        observation_deadline = time.monotonic() + COMMITTED_OFFSET_OBSERVATION_SECONDS
        while time.monotonic() < observation_deadline:
            if resumed_consumer.poll(timeout=0.25) is not None:
                pytest.fail("committed crawler event was delivered again to the same group")
    finally:
        resumed_consumer.close()
