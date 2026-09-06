from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from confluent_kafka import KafkaError, KafkaException

from sahabino.messaging import admin as admin_module
from sahabino.messaging.admin import ensure_topics
from sahabino.messaging.exceptions import (
    TopicProvisioningError,
    TopicTopologyMismatchError,
)
from sahabino.messaging.topics import SAHABINO_TOPICS, TopicTopology

EXPECTED_TOPOLOGY = TopicTopology(partitions=3, replication_factor=1)


@dataclass
class FakeFuture:
    error: BaseException | None = None
    timeouts: list[float] | None = None

    def result(self, timeout: float) -> None:
        if self.timeouts is not None:
            self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error


class FakeAdminClient:
    def __init__(self, metadata_responses: list[object]) -> None:
        self.metadata_responses = metadata_responses
        self.metadata_calls = 0
        self.create_calls: list[list[Any]] = []
        self.creation_errors: dict[str, BaseException] = {}
        self.create_request_error: BaseException | None = None
        self.omitted_creation_results: set[str] = set()

    def list_topics(self, *, timeout: float) -> object:
        response = self.metadata_responses[
            min(self.metadata_calls, len(self.metadata_responses) - 1)
        ]
        self.metadata_calls += 1
        if isinstance(response, BaseException):
            raise response
        return response

    def create_topics(self, topics: list[Any], **kwargs: object) -> dict[str, FakeFuture]:
        if self.create_request_error is not None:
            raise self.create_request_error
        self.create_calls.append(topics)
        return {
            topic.topic: FakeFuture(self.creation_errors.get(topic.topic))
            for topic in topics
            if topic.topic not in self.omitted_creation_results
        }


def _metadata(topologies: dict[str, tuple[int, int]]) -> object:
    topics: dict[str, object] = {}
    for name, (partitions, replication_factor) in topologies.items():
        topics[name] = SimpleNamespace(
            error=None,
            partitions={
                partition_id: SimpleNamespace(
                    error=None,
                    replicas=list(range(replication_factor)),
                )
                for partition_id in range(partitions)
            },
        )
    return SimpleNamespace(topics=topics)


def _install_client(monkeypatch: pytest.MonkeyPatch, client: FakeAdminClient) -> FakeAdminClient:
    monkeypatch.setattr(admin_module, "AdminClient", lambda config: client)
    return client


def _created_names(client: FakeAdminClient) -> list[str]:
    return [topic.topic for call in client.create_calls for topic in call]


def test_all_missing_topics_are_created_with_configured_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_client(monkeypatch, FakeAdminClient([_metadata({})]))

    created = ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY)

    assert created == SAHABINO_TOPICS
    assert _created_names(client) == list(SAHABINO_TOPICS)
    assert all(
        topic.num_partitions == 3 and topic.replication_factor == 1
        for topic in client.create_calls[0]
    )


def test_only_missing_topics_are_created(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = SAHABINO_TOPICS[0]
    client = _install_client(
        monkeypatch,
        FakeAdminClient([_metadata({existing: (3, 1)})]),
    )

    created = ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY)

    assert created == SAHABINO_TOPICS[1:]
    assert _created_names(client) == list(SAHABINO_TOPICS[1:])


def test_repeated_provisioning_of_correct_topics_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = _metadata({name: (3, 1) for name in SAHABINO_TOPICS})
    client = _install_client(monkeypatch, FakeAdminClient([metadata]))

    assert ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY) == ()
    assert ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY) == ()
    assert client.create_calls == []
    assert client.metadata_calls == 2


def test_metadata_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    kafka_error = KafkaException(KafkaError(KafkaError._TRANSPORT, "unavailable"))
    _install_client(monkeypatch, FakeAdminClient([kafka_error]))

    with pytest.raises(TopicProvisioningError, match="read Kafka topic metadata") as exc_info:
        ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY)

    assert exc_info.value.__cause__ is kafka_error


def test_metadata_timeout_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    timeout = TimeoutError("deadline")
    _install_client(monkeypatch, FakeAdminClient([timeout]))

    with pytest.raises(TopicProvisioningError, match="timed out reading") as exc_info:
        ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY)

    assert exc_info.value.__cause__ is timeout


def test_incomplete_cluster_metadata_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_client(monkeypatch, FakeAdminClient([SimpleNamespace()]))

    with pytest.raises(TopicProvisioningError, match="no valid topic map"):
        ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY)


def test_topic_creation_failure_is_wrapped_with_topic_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic = SAHABINO_TOPICS[0]
    client = _install_client(monkeypatch, FakeAdminClient([_metadata({})]))
    failure = KafkaException(KafkaError(KafkaError.INVALID_CONFIG, "invalid"))
    client.creation_errors[topic] = failure

    with pytest.raises(TopicProvisioningError, match=topic) as exc_info:
        ensure_topics("broker:9092", [topic], EXPECTED_TOPOLOGY)

    assert exc_info.value.__cause__ is failure


def test_topic_creation_request_failure_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install_client(monkeypatch, FakeAdminClient([_metadata({})]))
    failure = KafkaException(KafkaError(KafkaError._TRANSPORT, "unavailable"))
    client.create_request_error = failure

    with pytest.raises(TopicProvisioningError, match="request.*creation") as exc_info:
        ensure_topics("broker:9092", [SAHABINO_TOPICS[0]], EXPECTED_TOPOLOGY)

    assert exc_info.value.__cause__ is failure


def test_topic_creation_timeout_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    topic = SAHABINO_TOPICS[0]
    client = _install_client(monkeypatch, FakeAdminClient([_metadata({})]))
    timeout = TimeoutError("deadline")
    client.creation_errors[topic] = timeout

    with pytest.raises(TopicProvisioningError, match=f"timed out.*{topic}") as exc_info:
        ensure_topics("broker:9092", [topic], EXPECTED_TOPOLOGY, timeout=2.0)

    assert exc_info.value.__cause__ is timeout


def test_topic_already_exists_race_is_rechecked_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic = SAHABINO_TOPICS[0]
    client = _install_client(
        monkeypatch,
        FakeAdminClient([_metadata({}), _metadata({topic: (3, 1)})]),
    )
    client.creation_errors[topic] = KafkaException(
        KafkaError(KafkaError.TOPIC_ALREADY_EXISTS, "created concurrently")
    )

    assert ensure_topics("broker:9092", [topic], EXPECTED_TOPOLOGY) == ()
    assert client.metadata_calls == 2


def test_missing_creation_future_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    topic = SAHABINO_TOPICS[0]
    client = _install_client(monkeypatch, FakeAdminClient([_metadata({})]))
    client.omitted_creation_results.add(topic)

    with pytest.raises(TopicProvisioningError, match="did not return.*creation"):
        ensure_topics("broker:9092", [topic], EXPECTED_TOPOLOGY)


@pytest.mark.parametrize(
    ("actual", "expected_fragment"),
    [
        ((1, 1), "actual partitions=1, replication_factor=1"),
        ((3, 2), "actual partitions=3, replication_factor=2"),
    ],
)
def test_existing_topic_topology_mismatch_has_actionable_details(
    monkeypatch: pytest.MonkeyPatch,
    actual: tuple[int, int],
    expected_fragment: str,
) -> None:
    topic = SAHABINO_TOPICS[0]
    _install_client(monkeypatch, FakeAdminClient([_metadata({topic: actual})]))

    with pytest.raises(TopicTopologyMismatchError) as exc_info:
        ensure_topics("broker:9092", [topic], EXPECTED_TOPOLOGY)

    message = str(exc_info.value)
    assert topic in message
    assert "expected partitions=3, replication_factor=1" in message
    assert expected_fragment in message


def test_mixed_correct_and_incorrect_topics_fails_without_mutating_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correct, incorrect = SAHABINO_TOPICS[:2]
    client = _install_client(
        monkeypatch,
        FakeAdminClient([_metadata({correct: (3, 1), incorrect: (1, 1)})]),
    )

    with pytest.raises(TopicTopologyMismatchError, match=incorrect):
        ensure_topics("broker:9092", SAHABINO_TOPICS, EXPECTED_TOPOLOGY)

    assert client.create_calls == []


def test_inconsistent_partition_replication_metadata_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topic = SAHABINO_TOPICS[0]
    malformed = _metadata({topic: (2, 1)})
    malformed.topics[topic].partitions[1].replicas = [0, 1]
    _install_client(monkeypatch, FakeAdminClient([malformed]))

    with pytest.raises(TopicProvisioningError, match="inconsistent replication factors"):
        ensure_topics("broker:9092", [topic], EXPECTED_TOPOLOGY)


@pytest.mark.parametrize(
    "topic_metadata",
    [
        SimpleNamespace(error=None, partitions={}),
        SimpleNamespace(
            error=None,
            partitions={0: SimpleNamespace(error=None)},
        ),
    ],
)
def test_incomplete_topic_metadata_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    topic_metadata: object,
) -> None:
    topic = SAHABINO_TOPICS[0]
    metadata = SimpleNamespace(topics={topic: topic_metadata})
    _install_client(monkeypatch, FakeAdminClient([metadata]))

    with pytest.raises(TopicProvisioningError, match="metadata"):
        ensure_topics("broker:9092", [topic], EXPECTED_TOPOLOGY)
