from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TopicTopology:
    """Deployment-specific topology shared by Sahabino's domain topics."""

    partitions: int
    replication_factor: int

    def __post_init__(self) -> None:
        if self.partitions < 1:
            raise ValueError("topic partitions must be at least 1")
        if self.replication_factor < 1:
            raise ValueError("topic replication factor must be at least 1")


PLAYSTORE_APP_STATS_TOPIC = "playstore.app-stats.v1"
PLAYSTORE_REVIEW_OBSERVED_TOPIC = "playstore.review-observed.v1"
NETWORK_ANALYSIS_COLLECTED_TOPIC = "network.analysis-collected.v1"

SAHABINO_TOPICS = (
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
    NETWORK_ANALYSIS_COLLECTED_TOPIC,
)
