from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from sahabino.common.config import Settings
from sahabino.messaging.topics import SAHABINO_TOPICS, TopicTopology


def test_required_topics_are_unique_versioned_domain_topics() -> None:
    assert list(SAHABINO_TOPICS) == [
        "playstore.app-stats.v1",
        "playstore.review-observed.v1",
        "network.analysis-collected.v1",
        "network.capture-ready.v1",
    ]
    assert len(SAHABINO_TOPICS) == len(set(SAHABINO_TOPICS))
    assert all(name.endswith(".v1") for name in SAHABINO_TOPICS)


def test_topic_identity_is_separate_from_deployment_topology() -> None:
    topology = TopicTopology(partitions=6, replication_factor=3)

    assert topology.partitions == 6
    assert topology.replication_factor == 3
    assert all(isinstance(name, str) for name in SAHABINO_TOPICS)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"partitions": 0}, "partitions"),
        ({"replication_factor": 0}, "replication factor"),
    ],
)
def test_topic_topology_rejects_invalid_values(changes: dict[str, int], message: str) -> None:
    values = {"partitions": 3, "replication_factor": 1}
    values.update(changes)

    with pytest.raises(ValueError, match=message):
        TopicTopology(**values)


def test_topic_topology_is_immutable() -> None:
    topology = TopicTopology(partitions=3, replication_factor=1)

    with pytest.raises(FrozenInstanceError):
        topology.partitions = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kafka_topic_partitions", 0),
        ("kafka_topic_replication_factor", 0),
    ],
)
def test_settings_reject_invalid_topic_topology(field: str, value: int) -> None:
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://localhost/sahabino",
        field: value,
    }

    with pytest.raises(ValidationError):
        Settings(**values)  # type: ignore[arg-type]
