class IngestionConsistencyError(RuntimeError):
    """A valid event conflicts with persisted crawler lifecycle state."""


class InvalidIngestionMessage(ValueError):
    """A Kafka record is permanently invalid for this worker version."""

    def __init__(self, reason: str, **context: object) -> None:
        self.reason = reason
        self.context = context
        super().__init__(reason)
