from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_core import PydanticSerializationError

from sahabino.messaging.exceptions import EventDeserializationError, EventSerializationError


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventEnvelope[PayloadT](BaseModel):
    """Versioned transport envelope shared by Sahabino Kafka events."""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    occurred_at: datetime = Field(default_factory=_utc_now)
    payload: PayloadT

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)


def serialize_event(event: EventEnvelope[Any]) -> bytes:
    """Serialize an envelope as UTF-8 JSON bytes without involving Kafka."""
    try:
        return event.model_dump_json().encode("utf-8")
    except (PydanticSerializationError, UnicodeEncodeError) as error:
        raise EventSerializationError("event envelope could not be serialized") from error


def deserialize_event[PayloadT](
    data: bytes,
    envelope_type: type[EventEnvelope[PayloadT]],
) -> EventEnvelope[PayloadT]:
    """Validate UTF-8 JSON bytes against the requested typed envelope."""
    try:
        return envelope_type.model_validate_json(data)
    except (ValidationError, ValueError) as error:
        raise EventDeserializationError("invalid event envelope JSON") from error
