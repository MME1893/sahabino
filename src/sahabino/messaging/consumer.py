from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from confluent_kafka import Consumer, KafkaException, Message, TopicPartition

from sahabino.common.config import Settings
from sahabino.messaging.exceptions import (
    ConsumerClosedError,
    ConsumerCommitError,
    ConsumerMessageError,
)

OffsetResetPolicy = Literal["earliest", "latest"]
SUPPORTED_OFFSET_RESET_POLICIES = frozenset({"earliest", "latest"})


def consumer_config(
    bootstrap_servers: str,
    group_id: str,
    auto_offset_reset: OffsetResetPolicy = "earliest",
    *,
    max_poll_interval_ms: int | None = None,
) -> dict[str, Any]:
    """Build consumer settings for explicit post-processing offset commits."""
    if auto_offset_reset not in SUPPORTED_OFFSET_RESET_POLICIES:
        raise ValueError("auto.offset.reset must be 'earliest' or 'latest'")
    config: dict[str, Any] = {
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "enable.auto.commit": False,
        "enable.auto.offset.store": False,
        "auto.offset.reset": auto_offset_reset,
    }
    if max_poll_interval_ms is not None:
        if max_poll_interval_ms <= 0:
            raise ValueError("max.poll.interval.ms must be positive")
        config["max.poll.interval.ms"] = max_poll_interval_ms
    return config


def _validated_topics(topics: Sequence[str]) -> list[str]:
    selected_topics = list(dict.fromkeys(topics))
    if not selected_topics:
        raise ValueError("at least one Kafka topic is required")
    if any(not isinstance(topic, str) or not topic.strip() for topic in selected_topics):
        raise ValueError("Kafka topic names must not be blank")
    return selected_topics


class KafkaConsumer:
    """Small synchronous consumer foundation for future ingestion workers."""

    def __init__(
        self,
        bootstrap_servers: str,
        group_id: str,
        topics: Sequence[str],
        *,
        auto_offset_reset: OffsetResetPolicy = "earliest",
        max_poll_interval_ms: int | None = None,
    ) -> None:
        selected_topics = _validated_topics(topics)
        self._consumer = Consumer(
            consumer_config(
                bootstrap_servers,
                group_id,
                auto_offset_reset,
                max_poll_interval_ms=max_poll_interval_ms,
            )
        )
        self._closed = False
        self._consumer.subscribe(selected_topics)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        group_id: str,
        topics: Sequence[str],
        *,
        max_poll_interval_ms: int | None = None,
    ) -> KafkaConsumer:
        """Construct a consumer from centralized application settings."""
        return cls(
            settings.kafka_bootstrap_servers,
            group_id,
            topics,
            auto_offset_reset=settings.kafka_consumer_auto_offset_reset,
            max_poll_interval_ms=max_poll_interval_ms,
        )

    def subscribe(self, topics: Sequence[str]) -> None:
        """Replace the consumer's topic subscription."""
        self._ensure_open()
        self._consumer.subscribe(_validated_topics(topics))

    def poll(self, timeout: float = 1.0) -> Message | None:
        """Return None on timeout, raise on message errors, or return a valid message."""
        self._ensure_open()
        try:
            message = self._consumer.poll(timeout)
        except KafkaException as exc:
            raise ConsumerMessageError("Kafka polling failed") from exc

        if message is None:
            return None
        message_error = message.error()
        if message_error is not None:
            raise ConsumerMessageError(f"Kafka message error: {message_error}")
        return message

    def commit(self, message: Message) -> None:
        """Synchronously commit a successfully processed message's next offset."""
        self._ensure_open()
        try:
            committed = self._consumer.commit(message=message, asynchronous=False)
        except KafkaException as exc:
            raise ConsumerCommitError("Kafka offset commit failed") from exc

        if committed is None:
            raise ConsumerCommitError("Kafka returned no result for synchronous offset commit")
        failures = [partition.error for partition in committed if partition.error is not None]
        if failures:
            details = "; ".join(str(error) for error in failures)
            raise ConsumerCommitError(f"Kafka offset commit failed: {details}")

    def assignment(self) -> list[TopicPartition]:
        """Return the current assignment for bounded group-initialization checks."""
        self._ensure_open()
        try:
            return self._consumer.assignment()
        except KafkaException as exc:
            raise ConsumerMessageError("Kafka assignment lookup failed") from exc

    def close(self) -> None:
        """Close the underlying consumer and leave its group."""
        if self._closed:
            return
        self._consumer.close()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise ConsumerClosedError("Kafka consumer is closed")
