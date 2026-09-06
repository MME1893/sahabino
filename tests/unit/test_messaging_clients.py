from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from sahabino.common.config import Settings
from sahabino.messaging import consumer as consumer_module
from sahabino.messaging import producer as producer_module
from sahabino.messaging.consumer import KafkaConsumer, consumer_config
from sahabino.messaging.events import EventEnvelope
from sahabino.messaging.exceptions import (
    ConsumerMessageError,
    ProducerDeliveryError,
    ProducerPublishError,
)
from sahabino.messaging.producer import KafkaProducer, encode_message_key, producer_config


class FakeMessage:
    def __init__(self, error: object | None = None) -> None:
        self._error = error

    def error(self) -> object | None:
        return self._error

    def topic(self) -> str:
        return "test.events.v1"

    def partition(self) -> int:
        return 0


class FakeProducerClient:
    delivery_error: object | None = None
    produce_outcomes: list[BaseException | None] = []
    instances: list[FakeProducerClient] = []

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.messages: list[dict[str, Any]] = []
        self.callbacks: list[Any] = []
        self.flush_calls: list[float] = []
        self.poll_calls: list[float] = []
        self.produce_calls = 0
        self._produce_outcomes = list(type(self).produce_outcomes)
        self.instances.append(self)

    def produce(self, **message: Any) -> None:
        self.produce_calls += 1
        if self._produce_outcomes:
            outcome = self._produce_outcomes.pop(0)
            if outcome is not None:
                raise outcome
        self.messages.append(message)
        self.callbacks.append(message["on_delivery"])

    def poll(self, timeout: float) -> int:
        self.poll_calls.append(timeout)
        return 0

    def flush(self, timeout: float) -> int:
        self.flush_calls.append(timeout)
        for callback in self.callbacks:
            callback(self.delivery_error, FakeMessage())
        self.callbacks.clear()
        return 0


class FakeConsumerClient:
    instance: FakeConsumerClient
    instances: list[FakeConsumerClient] = []

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.topics: list[str] = []
        self.responses: list[FakeMessage | None] = []
        self.commits: list[tuple[FakeMessage, bool]] = []
        self.closed = False
        type(self).instance = self
        type(self).instances.append(self)

    def subscribe(self, topics: list[str]) -> None:
        self.topics = topics

    def poll(self, timeout: float) -> FakeMessage | None:
        return self.responses.pop(0)

    def commit(self, *, message: FakeMessage, asynchronous: bool) -> list[Any]:
        self.commits.append((message, asynchronous))
        return []

    def assignment(self) -> list[Any]:
        return []

    def close(self) -> None:
        self.closed = True


def _sample_envelope() -> EventEnvelope[dict[str, int]]:
    return EventEnvelope[dict[str, int]](
        event_type="test.observed",
        schema_version=1,
        payload={"count": 1},
    )


def _reset_fake_producer() -> None:
    FakeProducerClient.instances.clear()
    FakeProducerClient.delivery_error = None
    FakeProducerClient.produce_outcomes = []


def test_producer_configuration_enables_safe_retries() -> None:
    config = producer_config("broker-one:9092,broker-two:9092")

    assert config == {
        "bootstrap.servers": "broker-one:9092,broker-two:9092",
        "enable.idempotence": True,
        "acks": "all",
    }


def test_message_keys_are_encoded_as_utf8() -> None:
    assert encode_message_key("com.example.سحابینو") == "com.example.سحابینو".encode()


@pytest.mark.parametrize("key", ["", " ", "\t"])
def test_blank_message_keys_are_rejected(key: str) -> None:
    with pytest.raises(ProducerPublishError, match="must not be blank"):
        encode_message_key(key)


def test_consumer_configuration_requires_explicit_offset_management() -> None:
    config = consumer_config("broker:9092", "ingestion-group", "latest")

    assert config["bootstrap.servers"] == "broker:9092"
    assert config["group.id"] == "ingestion-group"
    assert config["enable.auto.commit"] is False
    assert config["enable.auto.offset.store"] is False
    assert config["auto.offset.reset"] == "latest"


def test_consumer_configuration_rejects_unknown_offset_reset_policy() -> None:
    with pytest.raises(ValueError, match="earliest.*latest"):
        consumer_config("broker:9092", "ingestion-group", "invalid")  # type: ignore[arg-type]


def test_settings_centralize_kafka_configuration() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        kafka_bootstrap_servers="kafka:19092",
        kafka_topic_partitions=6,
        kafka_topic_replication_factor=3,
        kafka_consumer_auto_offset_reset="latest",
        kafka_producer_queue_full_max_retries=5,
        kafka_producer_queue_full_poll_timeout_seconds=0.25,
    )

    assert settings.kafka_bootstrap_servers == "kafka:19092"
    assert settings.kafka_topic_partitions == 6
    assert settings.kafka_topic_replication_factor == 3
    assert settings.kafka_consumer_auto_offset_reset == "latest"
    assert settings.kafka_producer_queue_full_max_retries == 5
    assert settings.kafka_producer_queue_full_poll_timeout_seconds == 0.25


def test_kafka_configuration_has_local_development_defaults() -> None:
    settings = Settings(database_url="postgresql+psycopg://localhost/sahabino")

    assert settings.kafka_bootstrap_servers == "localhost:9092"
    assert settings.kafka_topic_partitions == 3
    assert settings.kafka_topic_replication_factor == 1
    assert settings.kafka_consumer_auto_offset_reset == "earliest"
    assert settings.kafka_producer_queue_full_max_retries == 3
    assert settings.kafka_producer_queue_full_poll_timeout_seconds == 0.1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kafka_consumer_auto_offset_reset", "middle"),
        ("kafka_producer_queue_full_max_retries", -1),
        ("kafka_producer_queue_full_poll_timeout_seconds", 0),
    ],
)
def test_settings_reject_invalid_kafka_client_configuration(field: str, value: object) -> None:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://localhost/sahabino",
        field: value,
    }

    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]


def test_producer_is_reused_and_flushes_delivery_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_producer()
    monkeypatch.setattr(producer_module, "Producer", FakeProducerClient)
    producer = KafkaProducer("broker:9092", flush_timeout=4.0)
    envelope = _sample_envelope()

    producer.publish(topic="test.events.v1", key="application-id", event=envelope)
    producer.publish(topic="test.events.v1", key="application-id", event=envelope)
    producer.close()

    assert len(FakeProducerClient.instances) == 1
    client = FakeProducerClient.instances[0]
    assert len(client.messages) == 2
    assert client.messages[0]["key"] == b"application-id"
    assert client.messages[0]["value"].startswith(b'{"event_id":')
    assert client.flush_calls == [4.0]


def test_producer_retries_after_queue_full_and_services_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_producer()
    FakeProducerClient.produce_outcomes = [BufferError("queue full"), None]
    monkeypatch.setattr(producer_module, "Producer", FakeProducerClient)
    producer = KafkaProducer(
        "broker:9092",
        queue_full_max_retries=1,
        queue_full_poll_timeout=0.25,
    )

    producer.publish(
        topic="test.events.v1",
        key="application-id",
        event=_sample_envelope(),
    )

    client = FakeProducerClient.instances[0]
    assert client.produce_calls == 2
    assert client.poll_calls == [0.25, 0]
    assert len(client.messages) == 1


def test_producer_stops_after_bounded_persistent_queue_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_producer()
    FakeProducerClient.produce_outcomes = [BufferError("queue full")] * 3
    monkeypatch.setattr(producer_module, "Producer", FakeProducerClient)
    producer = KafkaProducer(
        "broker:9092",
        queue_full_max_retries=2,
        queue_full_poll_timeout=0.2,
    )

    with pytest.raises(ProducerPublishError, match="after 3 attempt"):
        producer.publish(
            topic="test.events.v1",
            key="application-id",
            event=_sample_envelope(),
        )

    client = FakeProducerClient.instances[0]
    assert client.produce_calls == 3
    assert client.poll_calls == [0.2, 0.2]


def test_producer_can_be_configured_for_immediate_queue_full_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_producer()
    FakeProducerClient.produce_outcomes = [BufferError("queue full")]
    monkeypatch.setattr(producer_module, "Producer", FakeProducerClient)
    producer = KafkaProducer("broker:9092", queue_full_max_retries=0)

    with pytest.raises(ProducerPublishError, match="after 1 attempt"):
        producer.publish(
            topic="test.events.v1",
            key="application-id",
            event=_sample_envelope(),
        )

    client = FakeProducerClient.instances[0]
    assert client.produce_calls == 1
    assert client.poll_calls == []


def test_producer_uses_centralized_backpressure_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_producer()
    FakeProducerClient.produce_outcomes = [BufferError("queue full"), None]
    monkeypatch.setattr(producer_module, "Producer", FakeProducerClient)
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        kafka_producer_queue_full_max_retries=1,
        kafka_producer_queue_full_poll_timeout_seconds=0.3,
    )
    producer = KafkaProducer.from_settings(settings)

    producer.publish(
        topic="test.events.v1",
        key="application-id",
        event=_sample_envelope(),
    )

    assert FakeProducerClient.instances[0].poll_calls == [0.3, 0]


def test_producer_reports_delivery_callback_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_fake_producer()
    FakeProducerClient.delivery_error = RuntimeError("broker rejected message")
    monkeypatch.setattr(producer_module, "Producer", FakeProducerClient)
    producer = KafkaProducer("broker:9092")
    producer.publish(
        topic="test.events.v1",
        key="application-id",
        event=_sample_envelope(),
    )

    with pytest.raises(ProducerDeliveryError, match="broker rejected message"):
        producer.flush()


@pytest.mark.parametrize("topics", [[], [""], [" "], ["\t"]])
def test_invalid_topics_do_not_construct_native_consumer(
    monkeypatch: pytest.MonkeyPatch, topics: list[str]
) -> None:
    FakeConsumerClient.instances.clear()
    monkeypatch.setattr(consumer_module, "Consumer", FakeConsumerClient)

    with pytest.raises(ValueError, match="topic"):
        KafkaConsumer("broker:9092", "test-group", topics)

    assert FakeConsumerClient.instances == []


def test_consumer_uses_centralized_offset_reset_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeConsumerClient.instances.clear()
    monkeypatch.setattr(consumer_module, "Consumer", FakeConsumerClient)
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        kafka_consumer_auto_offset_reset="latest",
    )

    KafkaConsumer.from_settings(settings, "test-group", ["test.events.v1"])

    assert FakeConsumerClient.instance.config["auto.offset.reset"] == "latest"


def test_consumer_distinguishes_timeout_message_error_and_explicit_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeConsumerClient.instances.clear()
    monkeypatch.setattr(consumer_module, "Consumer", FakeConsumerClient)
    consumer = KafkaConsumer("broker:9092", "test-group", ["test.events.v1"])
    client = FakeConsumerClient.instance
    valid_message = FakeMessage()
    client.responses = [None, valid_message, FakeMessage(RuntimeError("message failed"))]

    assert consumer.poll() is None
    assert consumer.poll() is valid_message
    consumer.commit(valid_message)  # type: ignore[arg-type]
    with pytest.raises(ConsumerMessageError, match="message failed"):
        consumer.poll()
    consumer.close()

    assert client.topics == ["test.events.v1"]
    assert client.commits == [(valid_message, False)]
    assert client.closed is True
