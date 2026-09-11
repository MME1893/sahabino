from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from confluent_kafka import KafkaError, KafkaException

# NewTopic is a documented public re-export that confluent-kafka does not mark explicitly.
from confluent_kafka.admin import AdminClient, NewTopic  # type: ignore[attr-defined]

from sahabino.common.config import get_settings
from sahabino.common.observability import configure_logging
from sahabino.messaging.exceptions import (
    TopicProvisioningError,
    TopicTopologyMismatchError,
)
from sahabino.messaging.topics import SAHABINO_TOPICS, TopicTopology

DEFAULT_ADMIN_TIMEOUT_SECONDS = 15.0


def _validated_topic_names(topics: Sequence[str]) -> tuple[str, ...]:
    names = tuple(topics)
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("Kafka topic names must not be blank")
    if len(names) != len(set(names)):
        raise ValueError("Kafka topic names must be unique")
    return names


def _read_metadata(client: AdminClient, timeout: float) -> Any:
    try:
        return client.list_topics(timeout=timeout)
    except KafkaException as error:
        raise TopicProvisioningError("could not read Kafka topic metadata") from error
    except TimeoutError as error:
        raise TopicProvisioningError("timed out reading Kafka topic metadata") from error


def _existing_topic_topology(topic_name: str, topic_metadata: object) -> TopicTopology:
    topic_error = getattr(topic_metadata, "error", None)
    if topic_error is not None:
        raise TopicProvisioningError(
            f"Kafka metadata for topic {topic_name!r} contains an error: {topic_error}"
        )

    partitions = getattr(topic_metadata, "partitions", None)
    if not isinstance(partitions, Mapping) or not partitions:
        raise TopicProvisioningError(
            f"Kafka metadata for topic {topic_name!r} has no valid partitions"
        )

    replica_counts: set[int] = set()
    for partition_id, partition in partitions.items():
        partition_error = getattr(partition, "error", None)
        if partition_error is not None:
            raise TopicProvisioningError(
                f"Kafka metadata for topic {topic_name!r} partition {partition_id!r} "
                f"contains an error: {partition_error}"
            )
        replicas = getattr(partition, "replicas", None)
        if not isinstance(replicas, Sequence) or isinstance(replicas, (str, bytes)):
            raise TopicProvisioningError(
                f"Kafka metadata for topic {topic_name!r} partition {partition_id!r} "
                "has no valid replica list"
            )
        replica_count = len(replicas)
        if replica_count < 1:
            raise TopicProvisioningError(
                f"Kafka metadata for topic {topic_name!r} partition {partition_id!r} "
                "has no replicas"
            )
        replica_counts.add(replica_count)

    if len(replica_counts) != 1:
        counts = ", ".join(str(count) for count in sorted(replica_counts))
        raise TopicProvisioningError(
            f"Kafka topic {topic_name!r} has inconsistent replication factors "
            f"across partitions: {counts}"
        )

    return TopicTopology(
        partitions=len(partitions),
        replication_factor=replica_counts.pop(),
    )


def _metadata_topics(metadata: object) -> Mapping[str, object]:
    topics = getattr(metadata, "topics", None)
    if not isinstance(topics, Mapping):
        raise TopicProvisioningError("Kafka cluster metadata has no valid topic map")
    return topics


def _verify_topic_topology(
    topic_name: str,
    topic_metadata: object,
    expected: TopicTopology,
) -> None:
    actual = _existing_topic_topology(topic_name, topic_metadata)
    if actual != expected:
        raise TopicTopologyMismatchError(
            topic_name,
            expected_partitions=expected.partitions,
            expected_replication_factor=expected.replication_factor,
            actual_partitions=actual.partitions,
            actual_replication_factor=actual.replication_factor,
        )


def _is_kafka_error(error: KafkaException, code: int) -> bool:
    kafka_error = error.args[0] if error.args else None
    return isinstance(kafka_error, KafkaError) and kafka_error.code() == code


def ensure_topics(
    bootstrap_servers: str,
    topics: Sequence[str],
    topology: TopicTopology,
    *,
    timeout: float = DEFAULT_ADMIN_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Create missing topics and verify existing topics without mutating topology."""

    names = _validated_topic_names(topics)

    try:
        client = AdminClient({"bootstrap.servers": bootstrap_servers})
    except KafkaException as error:
        raise TopicProvisioningError("could not construct Kafka admin client") from error

    # here we read topics metadata
    metadata = _read_metadata(client, timeout)
    metadata_topics = _metadata_topics(metadata)

    missing: list[str] = []
    for name in names:
        topic_metadata = metadata_topics.get(name)
        if topic_metadata is None:
            missing.append(name)
            continue

        # just to make sure existing ones are align with the topology we want
        _verify_topic_topology(name, topic_metadata, topology)

    if not missing:
        return ()

    new_topics = [
        NewTopic(
            topic=name,
            num_partitions=topology.partitions,
            replication_factor=topology.replication_factor,
        )
        for name in missing
    ]
    try:
        futures = client.create_topics(
            new_topics,
            operation_timeout=timeout,
            request_timeout=timeout,
        )
    except KafkaException as error:
        raise TopicProvisioningError("could not request Kafka topic creation") from error
    except TimeoutError as error:
        raise TopicProvisioningError("timed out requesting Kafka topic creation") from error

    created: list[str] = []
    raced_topics: list[str] = []
    for name in missing:
        future = futures.get(name)
        if future is None:
            raise TopicProvisioningError(
                f"Kafka did not return a creation result for topic {name!r}"
            )
        try:
            future.result(timeout=timeout)
        except KafkaException as error:
            if _is_kafka_error(error, KafkaError.TOPIC_ALREADY_EXISTS):
                raced_topics.append(name)
                continue
            raise TopicProvisioningError(f"could not create Kafka topic {name!r}") from error
        except TimeoutError as error:
            raise TopicProvisioningError(
                f"timed out while creating Kafka topic {name!r}"
            ) from error
        created.append(name)

    if raced_topics:
        refreshed_metadata = _read_metadata(client, timeout)
        refreshed_topics = _metadata_topics(refreshed_metadata)
        for name in raced_topics:
            topic_metadata = refreshed_topics.get(name)
            if topic_metadata is None:
                raise TopicProvisioningError(
                    f"Kafka reported topic {name!r} already exists, but metadata is missing"
                )
            _verify_topic_topology(name, topic_metadata, topology)

    return tuple(created)


def delete_topics(
    bootstrap_servers: str,
    topics: Sequence[str],
    *,
    timeout: float = DEFAULT_ADMIN_TIMEOUT_SECONDS,
) -> tuple[str, ...]:
    """Delete only the explicitly supplied topics, primarily for test cleanup."""
    names = _validated_topic_names(topics)
    if not names:
        return ()

    try:
        client = AdminClient({"bootstrap.servers": bootstrap_servers})
    except KafkaException as error:
        raise TopicProvisioningError("could not construct Kafka admin client") from error
    try:
        futures = client.delete_topics(
            list(names),
            operation_timeout=timeout,
            request_timeout=timeout,
        )
    except KafkaException as error:
        raise TopicProvisioningError("could not request Kafka topic deletion") from error
    except TimeoutError as error:
        raise TopicProvisioningError("timed out requesting Kafka topic deletion") from error

    deleted: list[str] = []
    for name in names:
        future = futures.get(name)
        if future is None:
            raise TopicProvisioningError(
                f"Kafka did not return a deletion result for topic {name!r}"
            )
        try:
            future.result(timeout=timeout)
        except KafkaException as error:
            if _is_kafka_error(error, KafkaError.UNKNOWN_TOPIC_OR_PART):
                continue
            raise TopicProvisioningError(f"could not delete Kafka topic {name!r}") from error
        except TimeoutError as error:
            raise TopicProvisioningError(
                f"timed out while deleting Kafka topic {name!r}"
            ) from error
        deleted.append(name)
    return tuple(deleted)


def main() -> None:
    settings = get_settings()
    configure_logging(
        service_name="sahabino-messaging-admin",
        level=settings.log_level,
        log_format=settings.log_format,
        environment=settings.environment,
    )
    topology = TopicTopology(
        partitions=settings.kafka_topic_partitions,
        replication_factor=settings.kafka_topic_replication_factor,
    )
    created = ensure_topics(
        settings.kafka_bootstrap_servers,
        SAHABINO_TOPICS,
        topology,
    )
    if created:
        print(f"Created Kafka topics: {', '.join(created)}")
    else:
        print("All required Kafka topics exist with the configured topology")


if __name__ == "__main__":
    main()
