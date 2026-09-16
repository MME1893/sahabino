from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from sahabino.db.sync_session import create_sync_session_factory
from sahabino.ingestion.models import IngestedEvent, PlaystoreAppSnapshot, Review, ReviewObservation
from sahabino.ingestion.worker import IngestionWorker
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.playstore_events import app_stats_envelope, review_observed_envelope
from sahabino.messaging.producer import KafkaProducer
from sahabino.messaging.topics import PLAYSTORE_APP_STATS_TOPIC, PLAYSTORE_REVIEW_OBSERVED_TOPIC
from tests.integration.ingestion.test_repository import (
    _app_payload,
    _application,
    _review_payload,
    _task,
)

POLL_DEADLINE_SECONDS = 30.0
COMMITTED_OFFSET_OBSERVATION_SECONDS = 2.0


def test_real_kafka_to_postgres_flow_and_committed_group_resume(
    crawler_database_url: str,
    crawler_kafka_bootstrap_servers: str,
    crawler_topics: tuple[str, str],
) -> None:
    assert set(crawler_topics) == {
        PLAYSTORE_APP_STATS_TOPIC,
        PLAYSTORE_REVIEW_OBSERVED_TOPIC,
    }
    session_factory = create_sync_session_factory(crawler_database_url)
    application = _application(session_factory)
    app_task = _task(session_factory, application.id, "app_details")
    review_task = _task(session_factory, application.id, "reviews")
    app_event = app_stats_envelope(_app_payload(application, app_task))
    review_event = review_observed_envelope(
        _review_payload(
            application,
            review_task,
            observed_at=datetime(2026, 9, 11, 18, tzinfo=UTC),
            score=5,
            content="persisted by real Kafka flow",
            position=1000,
        )
    )

    producer = KafkaProducer(crawler_kafka_bootstrap_servers)
    producer.publish(
        topic=PLAYSTORE_APP_STATS_TOPIC,
        key=str(application.id),
        event=app_event,
    )
    producer.publish(
        topic=PLAYSTORE_REVIEW_OBSERVED_TOPIC,
        key=str(application.id),
        event=review_event,
    )
    producer.close(timeout=15.0)

    group_id = f"sahabino-ingestion-integration-{uuid4().hex}"
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
        while processed < 2 and time.monotonic() < deadline:
            processed += int(worker.process_next(timeout=1.0))
    finally:
        worker.close()
    assert processed == 2

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IngestedEvent)) == 2
        assert session.scalar(select(func.count()).select_from(PlaystoreAppSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(Review)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewObservation)) == 1
        assert session.scalars(select(ReviewObservation)).one().position == 1000
        offsets = session.execute(
            select(IngestedEvent.topic, IngestedEvent.partition, IngestedEvent.offset)
        ).all()
        assert {topic for topic, _, _ in offsets} == set(crawler_topics)
        assert all(partition == 0 and offset == 0 for _, partition, offset in offsets)

    resumed_consumer = KafkaConsumer(
        crawler_kafka_bootstrap_servers,
        group_id=group_id,
        topics=crawler_topics,
    )
    assignment_deadline = time.monotonic() + POLL_DEADLINE_SECONDS
    try:
        while time.monotonic() < assignment_deadline:
            redelivered = resumed_consumer.poll(timeout=0.5)
            if redelivered is not None:
                pytest.fail("committed ingestion event was delivered again to the same group")
            if resumed_consumer.assignment():
                break
        else:
            pytest.fail("timed out waiting for resumed ingestion consumer assignment")

        observation_deadline = time.monotonic() + COMMITTED_OFFSET_OBSERVATION_SECONDS
        while time.monotonic() < observation_deadline:
            if resumed_consumer.poll(timeout=0.25) is not None:
                pytest.fail("committed ingestion event was delivered again to the same group")
    finally:
        resumed_consumer.close()
