from __future__ import annotations

import json
import logging
from typing import Any, cast

from confluent_kafka import Message
from sqlalchemy.orm import Session, sessionmaker

from sahabino.ingestion.exceptions import InvalidIngestionMessage
from sahabino.ingestion.handlers import SupportedEvent, handle_event
from sahabino.ingestion.repository import IngestionRepository
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.events import EventEnvelope, deserialize_event
from sahabino.messaging.exceptions import EventDeserializationError
from sahabino.messaging.network_events import (
    NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE,
    NETWORK_SCHEMA_VERSION,
    NetworkAnalysisCollectedV1,
)
from sahabino.messaging.playstore_events import (
    APP_STATS_EVENT_TYPE,
    PLAYSTORE_SCHEMA_VERSION,
    REVIEW_OBSERVED_EVENT_TYPE,
    AppStatsCollectedV1,
    ReviewObservedV1,
)
from sahabino.messaging.topics import (
    NETWORK_ANALYSIS_COLLECTED_TOPIC,
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
)

logger = logging.getLogger(__name__)

POLL_TIMEOUT_SECONDS = 1.0
ENVELOPE_FIELDS = frozenset({"event_id", "event_type", "schema_version", "occurred_at", "payload"})


def _safe_metadata(value: object) -> object | None:
    if isinstance(value, str) and len(value) <= 100:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _invalid(reason: str, **context: object) -> InvalidIngestionMessage:
    return InvalidIngestionMessage(
        reason,
        **{key: value for key, value in context.items() if _safe_metadata(value) is not None},
    )


def decode_message(message: Message) -> SupportedEvent:
    """Validate and route one Kafka record to an existing typed event contract."""
    value = message.value()
    if value is None:
        raise _invalid("missing_value")
    if not isinstance(value, bytes):
        raise _invalid("invalid_json")

    try:
        raw: Any = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _invalid("invalid_json") from None
    if not isinstance(raw, dict):
        raise _invalid("invalid_envelope")
    if set(raw) != ENVELOPE_FIELDS:
        raise _invalid("invalid_envelope")

    event_type = raw.get("event_type")
    schema_version = raw.get("schema_version")
    if not isinstance(event_type, str) or not event_type:
        raise _invalid("invalid_envelope")
    if event_type not in {
        APP_STATS_EVENT_TYPE,
        REVIEW_OBSERVED_EVENT_TYPE,
        NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE,
    }:
        raise _invalid("unsupported_event_type", event_type=event_type)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version
        != (
            NETWORK_SCHEMA_VERSION
            if event_type == NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE
            else PLAYSTORE_SCHEMA_VERSION
        )
    ):
        raise _invalid(
            "unsupported_schema_version",
            event_type=event_type,
            schema_version=schema_version,
        )

    expected_topic = {
        APP_STATS_EVENT_TYPE: PLAYSTORE_APP_STATS_TOPIC,
        REVIEW_OBSERVED_EVENT_TYPE: PLAYSTORE_REVIEW_OBSERVED_TOPIC,
        NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE: NETWORK_ANALYSIS_COLLECTED_TOPIC,
    }[event_type]
    if message.topic() != expected_topic:
        raise _invalid(
            "topic_event_mismatch",
            event_type=event_type,
            schema_version=schema_version,
        )

    try:
        if event_type == APP_STATS_EVENT_TYPE:
            event: SupportedEvent = deserialize_event(value, EventEnvelope[AppStatsCollectedV1])
        elif event_type == REVIEW_OBSERVED_EVENT_TYPE:
            event = deserialize_event(value, EventEnvelope[ReviewObservedV1])
        else:
            event = deserialize_event(value, EventEnvelope[NetworkAnalysisCollectedV1])
    except EventDeserializationError:
        raise _invalid(
            "invalid_envelope_or_payload",
            event_type=event_type,
            schema_version=schema_version,
        ) from None

    key = message.key()
    if key is None:
        raise _invalid("missing_key", event_type=event_type, schema_version=schema_version)
    if not isinstance(key, bytes):
        raise _invalid("invalid_utf8_key", event_type=event_type, schema_version=schema_version)
    try:
        decoded_key = key.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid(
            "invalid_utf8_key", event_type=event_type, schema_version=schema_version
        ) from None
    if decoded_key != str(event.payload.application_id):
        raise _invalid(
            "application_key_mismatch",
            event_type=event_type,
            schema_version=schema_version,
        )
    return event


class IngestionWorker:
    """Serial Kafka-to-PostgreSQL ingestion with explicit offset commits."""

    def __init__(
        self,
        *,
        consumer: KafkaConsumer,
        session_factory: sessionmaker[Session],
        consumer_group: str,
    ) -> None:
        self._consumer = consumer
        self._session_factory = session_factory
        self._consumer_group = consumer_group
        self._stopping = False
        self._closed = False

    def process_next(self, timeout: float = POLL_TIMEOUT_SECONDS) -> bool:
        try:
            message = self._consumer.poll(timeout=timeout)
        except Exception:
            logger.exception(
                "ingestion consumer poll failed",
                extra={
                    "event": "ingestion.message.failed",
                    "consumer_group": self._consumer_group,
                    "failure_stage": "kafka_poll",
                },
            )
            raise
        if message is None:
            return False

        message_context = self._message_context(message)
        try:
            event = decode_message(message)
        except InvalidIngestionMessage as error:
            skipped_context = {
                **message_context,
                **error.context,
                "event": "ingestion.message.skipped",
                "reason": error.reason,
            }
            logger.warning("invalid ingestion message skipped", extra=skipped_context)
            self._commit_offset(message, message_context)
            return True

        event_context: dict[str, object] = {
            **message_context,
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "application_id": str(event.payload.application_id),
        }
        crawl_task_id = getattr(event.payload, "crawl_task_id", None)
        capture_id = getattr(event.payload, "capture_id", None)
        analysis_id = getattr(event.payload, "analysis_id", None)
        if crawl_task_id is not None:
            event_context["crawl_task_id"] = str(crawl_task_id)
        if capture_id is not None:
            event_context["capture_id"] = str(capture_id)
        if analysis_id is not None:
            event_context["analysis_id"] = str(analysis_id)
        try:
            with self._session_factory.begin() as session:
                repository = IngestionRepository(session)
                claimed = repository.claim_event(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    schema_version=event.schema_version,
                    topic=cast(str, message.topic()),
                    partition=cast(int, message.partition()),
                    offset=cast(int, message.offset()),
                )
                if claimed:
                    handle_event(event, repository)
        except Exception as error:
            logger.error(
                "ingestion message processing failed",
                extra={
                    **event_context,
                    "event": "ingestion.message.failed",
                    "failure_stage": "database_processing",
                    "error_type": type(error).__name__,
                },
            )
            raise

        self._commit_offset(message, event_context)
        if claimed:
            logger.info(
                "ingestion message processed",
                extra={**event_context, "event": "ingestion.message.processed"},
            )
        else:
            logger.info(
                "duplicate ingestion message acknowledged",
                extra={**event_context, "event": "ingestion.message.duplicate"},
            )
        return True

    def run_forever(self) -> None:
        try:
            while not self._stopping:
                self.process_next()
        finally:
            self.close()

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        if self._closed:
            return
        self._consumer.close()
        self._closed = True

    def _message_context(self, message: Message) -> dict[str, object]:
        return {
            "topic": message.topic(),
            "partition": message.partition(),
            "offset": message.offset(),
            "consumer_group": self._consumer_group,
        }

    def _commit_offset(self, message: Message, context: dict[str, object]) -> None:
        try:
            self._consumer.commit(message)
        except Exception:
            logger.exception(
                "ingestion Kafka offset commit failed",
                extra={
                    **context,
                    "event": "ingestion.message.failed",
                    "failure_stage": "kafka_offset_commit",
                },
            )
            raise
