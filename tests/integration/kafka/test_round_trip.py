from __future__ import annotations

import time
from uuid import uuid4

import pytest
from confluent_kafka import Message
from pydantic import BaseModel, ConfigDict

from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.events import EventEnvelope, deserialize_event
from sahabino.messaging.producer import KafkaProducer

POLL_DEADLINE_SECONDS = 30.0
COMMITTED_OFFSET_OBSERVATION_SECONDS = 2.0


class SamplePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    observation_count: int


def _poll_required_message(consumer: KafkaConsumer) -> Message:
    deadline = time.monotonic() + POLL_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        message = consumer.poll(timeout=1.0)
        if message is not None:
            return message
    pytest.fail("timed out waiting for the Kafka integration event")


def test_real_kafka_round_trip_and_committed_group_resume(
    kafka_bootstrap_servers: str,
    kafka_test_topic: str,
) -> None:
    run_id = uuid4().hex
    key = f"com.example.{run_id}"
    group_id = f"sahabino-integration-{run_id}"
    envelope = EventEnvelope[SamplePayload](
        event_type="test.messaging.observed",
        schema_version=1,
        payload=SamplePayload(application_id=key, observation_count=7),
    )

    producer = KafkaProducer(kafka_bootstrap_servers)
    producer.publish(topic=kafka_test_topic, key=key, event=envelope)
    producer.close(timeout=15.0)

    consumer = KafkaConsumer(
        kafka_bootstrap_servers,
        group_id=group_id,
        topics=[kafka_test_topic],
    )
    try:
        message = _poll_required_message(consumer)
        value = message.value()
        assert value is not None
        restored = deserialize_event(value, EventEnvelope[SamplePayload])

        assert message.key() == key.encode("utf-8")
        assert restored.event_id == envelope.event_id
        assert restored.event_type == envelope.event_type
        assert restored.payload == envelope.payload

        consumer.commit(message)
    finally:
        consumer.close()

    resumed_consumer = KafkaConsumer(
        kafka_bootstrap_servers,
        group_id=group_id,
        topics=[kafka_test_topic],
    )
    assignment_deadline = time.monotonic() + POLL_DEADLINE_SECONDS
    try:
        while time.monotonic() < assignment_deadline:
            redelivered = resumed_consumer.poll(timeout=0.5)
            if redelivered is not None:
                pytest.fail("committed Kafka event was delivered again to the same group")
            if resumed_consumer.assignment():
                break
        else:
            pytest.fail("timed out waiting for resumed Kafka consumer assignment")

        observation_deadline = time.monotonic() + COMMITTED_OFFSET_OBSERVATION_SECONDS
        while time.monotonic() < observation_deadline:
            redelivered = resumed_consumer.poll(timeout=0.25)
            if redelivered is not None:
                pytest.fail("committed Kafka event was delivered again to the same group")
    finally:
        resumed_consumer.close()
