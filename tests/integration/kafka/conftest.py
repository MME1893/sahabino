from __future__ import annotations

import os
import warnings
from collections.abc import Iterator
from uuid import uuid4

import pytest
from testcontainers.community.kafka import KafkaContainer

from sahabino.messaging.admin import delete_topics, ensure_topics
from sahabino.messaging.exceptions import TopicProvisioningError
from sahabino.messaging.topics import TopicTopology

TESTCONTAINERS_KAFKA_IMAGE = "confluentinc/cp-kafka:7.6.0"
TEST_TOPIC_TOPOLOGY = TopicTopology(partitions=1, replication_factor=1)


@pytest.fixture(scope="session")
def kafka_bootstrap_servers() -> Iterator[str]:
    """Use CI's Kafka service or start an isolated KRaft broker."""
    supplied_servers = os.getenv("TEST_KAFKA_BOOTSTRAP_SERVERS")
    if supplied_servers:
        yield supplied_servers
        return

    container = KafkaContainer(TESTCONTAINERS_KAFKA_IMAGE).with_kraft()
    container.with_env("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false")
    with container as kafka:
        yield kafka.get_bootstrap_server()


@pytest.fixture
def kafka_test_topic(kafka_bootstrap_servers: str) -> Iterator[str]:
    """Create one isolated topic and reliably remove only that topic after the test."""
    topic = f"sahabino.integration.{uuid4().hex}.v1"
    assert ensure_topics(
        kafka_bootstrap_servers,
        [topic],
        TEST_TOPIC_TOPOLOGY,
    ) == (topic,)
    assert (
        ensure_topics(
            kafka_bootstrap_servers,
            [topic],
            TEST_TOPIC_TOPOLOGY,
        )
        == ()
    )

    try:
        yield topic
    except BaseException:
        try:
            delete_topics(kafka_bootstrap_servers, [topic])
        except TopicProvisioningError as cleanup_error:
            warnings.warn(
                f"could not clean up Kafka test topic {topic!r}: {cleanup_error}",
                RuntimeWarning,
                stacklevel=2,
            )
        raise
    else:
        delete_topics(kafka_bootstrap_servers, [topic])
