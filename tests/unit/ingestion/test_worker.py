from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from confluent_kafka import Message

from sahabino.ingestion import bootstrap as bootstrap_module
from sahabino.ingestion import worker as worker_module
from sahabino.ingestion.exceptions import InvalidIngestionMessage
from sahabino.ingestion.worker import IngestionWorker, decode_message
from sahabino.messaging.events import EventEnvelope, serialize_event
from sahabino.messaging.playstore_events import (
    APP_STATS_EVENT_TYPE,
    REVIEW_OBSERVED_EVENT_TYPE,
    AppStatsCollectedV1,
    ReviewObservedV1,
    app_stats_envelope,
    review_observed_envelope,
)
from sahabino.messaging.topics import (
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
)


class FakeMessage:
    def __init__(
        self,
        *,
        topic: str,
        value: bytes | None,
        key: bytes | None,
        partition: int = 0,
        offset: int = 4,
    ) -> None:
        self._topic = topic
        self._value = value
        self._key = key
        self._partition = partition
        self._offset = offset

    def topic(self) -> str:
        return self._topic

    def value(self) -> bytes | None:
        return self._value

    def key(self) -> bytes | None:
        return self._key

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset


class FakeConsumer:
    def __init__(self, responses: list[FakeMessage | None]) -> None:
        self.responses = responses
        self.commits: list[FakeMessage] = []
        self.closed = False
        self.commit_error: BaseException | None = None
        self.on_poll: Callable[[], None] | None = None

    def poll(self, timeout: float) -> FakeMessage | None:
        _ = timeout
        if self.on_poll is not None:
            self.on_poll()
        return self.responses.pop(0)

    def commit(self, message: FakeMessage) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits.append(message)

    def close(self) -> None:
        self.closed = True


class FakeTransaction(AbstractContextManager[object]):
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def __enter__(self) -> object:
        self.events.append("db.begin")
        return object()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        _ = exc_value
        _ = traceback
        self.events.append("db.rollback" if exc_type is not None else "db.commit")
        return False


class FakeSessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.begin_calls = 0

    def begin(self) -> FakeTransaction:
        self.begin_calls += 1
        return FakeTransaction(self.events)


class FakeRepository:
    def __init__(self, events: list[str], *, claimed: bool = True) -> None:
        self.events = events
        self.claimed = claimed

    def claim_event(self, **values: object) -> bool:
        _ = values
        self.events.append("db.claim")
        return self.claimed


def _app_payload() -> AppStatsCollectedV1:
    return AppStatsCollectedV1(
        crawl_task_id=uuid4(),
        application_id=uuid4(),
        package_name="com.example.app",
        collected_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        min_installs=100,
        score=4.2,
        ratings_count=50,
        reviews_count=20,
        store_updated_on=date(2026, 9, 10),
        version="1.2.3",
        ad_supported=True,
        source_adapter="google-play-scraper",
    )


def _review_payload(*, content: str = "review text") -> ReviewObservedV1:
    return ReviewObservedV1(
        crawl_task_id=uuid4(),
        application_id=uuid4(),
        package_name="com.example.app",
        observed_at=datetime(2026, 9, 11, 13, tzinfo=UTC),
        position=1,
        external_review_id="review-1",
        source_at=datetime(2026, 9, 10, 8, tzinfo=UTC),
        author_name="Reviewer",
        thumbs_up_count=3,
        score=5,
        content=content,
        source_adapter="google-play-scraper",
    )


def _message_for(
    event: EventEnvelope[Any],
    *,
    topic: str,
    key: bytes | None = None,
) -> FakeMessage:
    application_id = cast(UUID, event.payload.application_id)
    return FakeMessage(
        topic=topic,
        value=serialize_event(event),
        key=key if key is not None else str(application_id).encode(),
    )


def _app_message() -> FakeMessage:
    return _message_for(app_stats_envelope(_app_payload()), topic=PLAYSTORE_APP_STATS_TOPIC)


def _worker(consumer: FakeConsumer, session_factory: FakeSessionFactory) -> IngestionWorker:
    return IngestionWorker(
        consumer=cast(Any, consumer),
        session_factory=cast(Any, session_factory),
        consumer_group="test-ingestion",
    )


def test_valid_app_stats_message_routes_to_typed_contract() -> None:
    message = _app_message()

    event = decode_message(cast(Message, message))

    assert event.event_type == APP_STATS_EVENT_TYPE
    assert isinstance(event.payload, AppStatsCollectedV1)


def test_valid_review_message_routes_to_typed_contract() -> None:
    payload = _review_payload()
    message = _message_for(review_observed_envelope(payload), topic=PLAYSTORE_REVIEW_OBSERVED_TOPIC)

    event = decode_message(cast(Message, message))

    assert event.event_type == REVIEW_OBSERVED_EVENT_TYPE
    assert isinstance(event.payload, ReviewObservedV1)


def _mutated_app_message(**changes: object) -> FakeMessage:
    message = _app_message()
    assert message.value() is not None
    raw = json.loads(message.value())
    raw.update(changes)
    return FakeMessage(
        topic=message.topic(),
        value=json.dumps(raw).encode(),
        key=message.key(),
    )


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            FakeMessage(
                topic=PLAYSTORE_APP_STATS_TOPIC,
                value=b"{not-json",
                key=b"application",
            ),
            "invalid_json",
        ),
        (
            FakeMessage(
                topic=PLAYSTORE_APP_STATS_TOPIC,
                value=None,
                key=b"application",
            ),
            "missing_value",
        ),
        (_mutated_app_message(event_id=None), "invalid_envelope_or_payload"),
        (_mutated_app_message(payload=None), "invalid_envelope_or_payload"),
        (
            _mutated_app_message(
                payload={
                    **json.loads(_app_message().value())["payload"],
                    "score": 6,
                }
            ),
            "invalid_envelope_or_payload",
        ),
        (_mutated_app_message(event_type="unknown.event"), "unsupported_event_type"),
        (_mutated_app_message(schema_version=2), "unsupported_schema_version"),
        (
            FakeMessage(
                topic=PLAYSTORE_REVIEW_OBSERVED_TOPIC,
                value=_app_message().value(),
                key=_app_message().key(),
            ),
            "topic_event_mismatch",
        ),
        (
            FakeMessage(
                topic=PLAYSTORE_APP_STATS_TOPIC,
                value=_app_message().value(),
                key=None,
            ),
            "missing_key",
        ),
        (
            FakeMessage(
                topic=PLAYSTORE_APP_STATS_TOPIC,
                value=_app_message().value(),
                key=b"\xff",
            ),
            "invalid_utf8_key",
        ),
        (
            FakeMessage(
                topic=PLAYSTORE_APP_STATS_TOPIC,
                value=_app_message().value(),
                key=str(uuid4()).encode(),
            ),
            "application_key_mismatch",
        ),
    ],
)
def test_invalid_messages_are_classified(message: FakeMessage, reason: str) -> None:
    with pytest.raises(InvalidIngestionMessage, match=reason):
        decode_message(cast(Message, message))


def test_missing_envelope_field_is_rejected() -> None:
    message = _app_message()
    assert message.value() is not None
    raw = json.loads(message.value())
    del raw["event_id"]
    message = FakeMessage(topic=message.topic(), value=json.dumps(raw).encode(), key=message.key())

    with pytest.raises(InvalidIngestionMessage, match="invalid_envelope"):
        decode_message(cast(Message, message))


def test_invalid_message_logs_skips_and_commits_without_database_work(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = FakeMessage(topic=PLAYSTORE_APP_STATS_TOPIC, value=b"not-json", key=b"application")
    consumer = FakeConsumer([message])
    session_factory = FakeSessionFactory([])
    worker = _worker(consumer, session_factory)

    with caplog.at_level(logging.WARNING, logger=worker_module.__name__):
        assert worker.process_next()

    assert consumer.commits == [message]
    assert session_factory.begin_calls == 0
    assert caplog.records[-1].event == "ingestion.message.skipped"
    assert caplog.records[-1].reason == "invalid_json"


def test_valid_message_commits_database_before_kafka(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    message = _app_message()
    consumer = FakeConsumer([message])
    repository = FakeRepository(events)
    session_factory = FakeSessionFactory(events)
    monkeypatch.setattr(worker_module, "IngestionRepository", lambda session: repository)
    monkeypatch.setattr(
        worker_module,
        "handle_event",
        lambda event, selected_repository: events.append("db.handle"),
    )
    original_commit = consumer.commit

    def record_commit(selected_message: FakeMessage) -> None:
        events.append("kafka.commit")
        original_commit(selected_message)

    consumer.commit = record_commit

    assert _worker(consumer, session_factory).process_next()

    assert events == ["db.begin", "db.claim", "db.handle", "db.commit", "kafka.commit"]


def test_duplicate_event_skips_handler_and_commits_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    message = _app_message()
    consumer = FakeConsumer([message])
    repository = FakeRepository(events, claimed=False)
    monkeypatch.setattr(worker_module, "IngestionRepository", lambda session: repository)
    monkeypatch.setattr(
        worker_module,
        "handle_event",
        lambda event, selected_repository: events.append("db.handle"),
    )

    assert _worker(consumer, FakeSessionFactory(events)).process_next()

    assert "db.handle" not in events
    assert events[-1] == "db.commit"
    assert consumer.commits == [message]


def test_processing_failure_rolls_back_does_not_commit_and_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_content = "PRIVATE REVIEW CONTENT"
    payload = _review_payload(content=secret_content)
    message = _message_for(review_observed_envelope(payload), topic=PLAYSTORE_REVIEW_OBSERVED_TOPIC)
    events: list[str] = []
    consumer = FakeConsumer([message])
    repository = FakeRepository(events)
    monkeypatch.setattr(worker_module, "IngestionRepository", lambda session: repository)

    def fail_handler(event: object, selected_repository: object) -> None:
        _ = event
        _ = selected_repository
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(worker_module, "handle_event", fail_handler)

    with (
        caplog.at_level(logging.ERROR, logger=worker_module.__name__),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        _worker(consumer, FakeSessionFactory(events)).process_next()

    assert events[-1] == "db.rollback"
    assert consumer.commits == []
    assert caplog.records[-1].event == "ingestion.message.failed"
    assert caplog.records[-1].failure_stage == "database_processing"
    assert caplog.records[-1].error_type == "RuntimeError"
    assert secret_content not in caplog.text


def test_kafka_commit_failure_preserves_database_commit_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    consumer = FakeConsumer([_app_message()])
    consumer.commit_error = RuntimeError("commit failed")
    repository = FakeRepository(events)
    monkeypatch.setattr(worker_module, "IngestionRepository", lambda session: repository)
    monkeypatch.setattr(worker_module, "handle_event", lambda event, repository: None)

    with pytest.raises(RuntimeError, match="commit failed"):
        _worker(consumer, FakeSessionFactory(events)).process_next()

    assert events[-1] == "db.commit"


def test_poll_timeout_is_harmless() -> None:
    consumer = FakeConsumer([None])

    assert not _worker(consumer, FakeSessionFactory([])).process_next()
    assert consumer.commits == []


def test_run_loop_stops_cleanly_and_closes_consumer() -> None:
    consumer = FakeConsumer([None])
    worker = _worker(consumer, FakeSessionFactory([]))
    consumer.on_poll = worker.stop

    worker.run_forever()

    assert consumer.closed


def test_bootstrap_configures_ingestion_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = bootstrap_module.Settings(database_url="postgresql+psycopg://localhost/sahabino")
    configured: dict[str, object] = {}

    class FakeWorker:
        def run_forever(self) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(bootstrap_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        bootstrap_module,
        "configure_logging",
        lambda **values: configured.update(values),
    )
    monkeypatch.setattr(bootstrap_module, "build_worker", lambda selected: FakeWorker())
    monkeypatch.setattr(bootstrap_module, "_install_signal_handlers", lambda worker: None)

    bootstrap_module.main()

    assert configured["service_name"] == "sahabino-ingestion"


def test_bootstrap_builds_one_consumer_for_both_playstore_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = bootstrap_module.Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        ingestion_consumer_group_id="custom-group",
    )
    captured: dict[str, object] = {}
    session_factory = object()

    class FakeKafkaConsumer:
        @classmethod
        def from_settings(
            cls,
            selected_settings: object,
            group_id: str,
            topics: tuple[str, str],
        ) -> object:
            captured.update(
                settings=selected_settings,
                group_id=group_id,
                topics=topics,
            )
            return object()

    monkeypatch.setattr(
        bootstrap_module, "create_sync_session_factory", lambda database_url: session_factory
    )
    monkeypatch.setattr(bootstrap_module, "KafkaConsumer", FakeKafkaConsumer)

    worker = bootstrap_module.build_worker(settings)

    assert isinstance(worker, IngestionWorker)
    assert captured == {
        "settings": settings,
        "group_id": "custom-group",
        "topics": (PLAYSTORE_APP_STATS_TOPIC, PLAYSTORE_REVIEW_OBSERVED_TOPIC),
    }
