from __future__ import annotations

from typing import Any

from confluent_kafka import KafkaError, KafkaException, Message, Producer

from sahabino.common.config import Settings
from sahabino.messaging.events import EventEnvelope, serialize_event
from sahabino.messaging.exceptions import (
    ProducerClosedError,
    ProducerDeliveryError,
    ProducerFlushError,
    ProducerPublishError,
)

DEFAULT_FLUSH_TIMEOUT_SECONDS = 10.0
DEFAULT_QUEUE_FULL_MAX_RETRIES = 3
DEFAULT_QUEUE_FULL_POLL_TIMEOUT_SECONDS = 0.1


def producer_config(bootstrap_servers: str) -> dict[str, Any]:
    """Build the reliability-focused configuration used by every producer."""
    return {
        "bootstrap.servers": bootstrap_servers,
        "enable.idempotence": True,
        "acks": "all",
    }


def encode_message_key(key: str) -> bytes:
    """Encode caller-selected Kafka keys consistently as UTF-8."""
    if not isinstance(key, str):
        raise ProducerPublishError("message key must be text")
    if not key.strip():
        raise ProducerPublishError("message key must not be blank")
    try:
        return key.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ProducerPublishError("message key is not valid UTF-8 text") from error


class KafkaProducer:
    """Reusable synchronous confluent-kafka producer with explicit flushing."""

    def __init__(
        self,
        bootstrap_servers: str,
        *,
        flush_timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
        queue_full_max_retries: int = DEFAULT_QUEUE_FULL_MAX_RETRIES,
        queue_full_poll_timeout: float = DEFAULT_QUEUE_FULL_POLL_TIMEOUT_SECONDS,
    ) -> None:
        if queue_full_max_retries < 0:
            raise ValueError("queue-full retries must not be negative")
        if queue_full_poll_timeout <= 0:
            raise ValueError("queue-full poll timeout must be positive")
        self._producer = Producer(producer_config(bootstrap_servers))
        self._flush_timeout = flush_timeout
        self._queue_full_max_retries = queue_full_max_retries
        self._queue_full_poll_timeout = queue_full_poll_timeout
        self._delivery_errors: list[str] = []
        self._closed = False

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        flush_timeout: float = DEFAULT_FLUSH_TIMEOUT_SECONDS,
    ) -> KafkaProducer:
        """Construct a producer from centralized application settings."""
        return cls(
            settings.kafka_bootstrap_servers,
            flush_timeout=flush_timeout,
            queue_full_max_retries=settings.kafka_producer_queue_full_max_retries,
            queue_full_poll_timeout=(settings.kafka_producer_queue_full_poll_timeout_seconds),
        )

    def publish(self, *, topic: str, key: str, event: EventEnvelope[Any]) -> None:
        """Serialize and queue one keyed envelope for delivery."""
        self._ensure_open()
        value = serialize_event(event)
        encoded_key = encode_message_key(key)

        for attempt in range(self._queue_full_max_retries + 1):
            try:
                self._producer.produce(
                    topic=topic,
                    key=encoded_key,
                    value=value,
                    on_delivery=self._on_delivery,
                )
                break
            except BufferError as error:
                if attempt == self._queue_full_max_retries:
                    raise ProducerPublishError(
                        f"producer queue remained full for topic {topic!r} after "
                        f"{attempt + 1} attempt(s)"
                    ) from error
                try:
                    self._producer.poll(self._queue_full_poll_timeout)
                except KafkaException as poll_error:
                    raise ProducerPublishError(
                        f"could not service producer queue for topic {topic!r}"
                    ) from poll_error
                self._raise_delivery_errors()
            except (KafkaException, TypeError, ValueError) as error:
                raise ProducerPublishError(f"could not publish event to topic {topic!r}") from error

        try:
            self._producer.poll(0)
        except KafkaException as error:
            raise ProducerPublishError(
                f"could not service delivery callbacks for topic {topic!r}"
            ) from error

        self._raise_delivery_errors()

    def flush(self, timeout: float | None = None) -> None:
        """Wait for queued deliveries and report failures or remaining messages."""
        self._ensure_open()
        effective_timeout = self._flush_timeout if timeout is None else timeout
        try:
            remaining = self._producer.flush(effective_timeout)
        except KafkaException as error:
            raise ProducerFlushError("Kafka producer flush failed") from error
        if remaining:
            raise ProducerFlushError(
                f"{remaining} Kafka message(s) remained undelivered after flush"
            )
        self._raise_delivery_errors()

    def close(self, timeout: float | None = None) -> None:
        """Flush successfully before marking this producer closed."""
        if self._closed:
            return
        self.flush(timeout)
        self._closed = True

    def _on_delivery(self, error: KafkaError | None, message: Message) -> None:
        if error is not None:
            self._delivery_errors.append(
                f"{message.topic()}[{message.partition()}] delivery failed: {error}"
            )

    def _raise_delivery_errors(self) -> None:
        if not self._delivery_errors:
            return
        errors = tuple(self._delivery_errors)
        self._delivery_errors.clear()
        raise ProducerDeliveryError(errors)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ProducerClosedError("Kafka producer is closed")
