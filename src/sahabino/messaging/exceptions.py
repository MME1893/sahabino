from collections.abc import Sequence


class MessagingError(Exception):
    """Base exception for Kafka transport failures."""


class EventSerializationError(MessagingError):
    """Raised when an event envelope cannot be serialized."""


class EventDeserializationError(MessagingError):
    """Raised when bytes do not contain a valid event envelope."""


class ProducerError(MessagingError):
    """Base exception for producer failures."""


class ProducerClosedError(ProducerError):
    """Raised when publishing through a closed producer."""


class ProducerPublishError(ProducerError):
    """Raised when a message cannot be queued for delivery."""


class ProducerFlushError(ProducerError):
    """Raised when messages remain queued after a producer flush."""


class ProducerDeliveryError(ProducerError):
    """Raised when Kafka reports one or more failed deliveries."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class ConsumerError(MessagingError):
    """Base exception for consumer failures."""


class ConsumerClosedError(ConsumerError):
    """Raised when operating on a closed consumer."""


class ConsumerMessageError(ConsumerError):
    """Raised when Kafka returns an error-bearing message."""


class ConsumerCommitError(ConsumerError):
    """Raised when an explicit offset commit fails."""


class TopicProvisioningError(MessagingError):
    """Raised when required Kafka topics cannot be ensured."""


class TopicTopologyMismatchError(TopicProvisioningError):
    """Raised when an existing topic does not match the configured topology."""

    def __init__(
        self,
        topic: str,
        *,
        expected_partitions: int,
        expected_replication_factor: int,
        actual_partitions: int,
        actual_replication_factor: int,
    ) -> None:
        self.topic = topic
        self.expected_partitions = expected_partitions
        self.expected_replication_factor = expected_replication_factor
        self.actual_partitions = actual_partitions
        self.actual_replication_factor = actual_replication_factor
        super().__init__(
            f"Kafka topic topology mismatch for {topic!r}: "
            f"expected partitions={expected_partitions}, "
            f"replication_factor={expected_replication_factor}; "
            f"actual partitions={actual_partitions}, "
            f"replication_factor={actual_replication_factor}"
        )
