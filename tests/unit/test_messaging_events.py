from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from sahabino.messaging.events import EventEnvelope, deserialize_event, serialize_event
from sahabino.messaging.exceptions import EventDeserializationError


class SamplePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    count: int


def test_new_envelopes_generate_unique_uuid_and_utc_timestamp() -> None:
    first = EventEnvelope[SamplePayload](
        event_type="test.observed",
        schema_version=1,
        payload=SamplePayload(label="first", count=1),
    )
    second = EventEnvelope[SamplePayload](
        event_type="test.observed",
        schema_version=1,
        payload=SamplePayload(label="second", count=2),
    )

    assert isinstance(first.event_id, UUID)
    assert first.event_id != second.event_id
    assert first.occurred_at.tzinfo is UTC
    assert first.occurred_at.utcoffset() is not None


def test_explicit_utc_timestamp_remains_utc() -> None:
    occurred_at = datetime(2026, 9, 5, 14, 30, tzinfo=UTC)

    envelope = EventEnvelope[SamplePayload](
        event_type="test.observed",
        schema_version=1,
        occurred_at=occurred_at,
        payload=SamplePayload(label="utc", count=1),
    )

    assert envelope.occurred_at == occurred_at
    assert envelope.occurred_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            datetime(2026, 9, 5, 18, 0, tzinfo=timezone(timedelta(hours=3, minutes=30))),
            datetime(2026, 9, 5, 14, 30, tzinfo=UTC),
        ),
        (
            datetime(2026, 9, 5, 9, 0, tzinfo=timezone(-timedelta(hours=5, minutes=30))),
            datetime(2026, 9, 5, 14, 30, tzinfo=UTC),
        ),
    ],
)
def test_explicit_offset_timestamp_is_normalized_to_utc(
    source: datetime, expected: datetime
) -> None:
    envelope = EventEnvelope[SamplePayload](
        event_type="test.observed",
        schema_version=1,
        occurred_at=source,
        payload=SamplePayload(label="offset", count=1),
    )

    assert envelope.occurred_at == expected
    assert envelope.occurred_at.tzinfo is UTC


@pytest.mark.parametrize("schema_version", [0, -1])
def test_envelope_rejects_invalid_schema_versions(schema_version: int) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope[SamplePayload](
            event_type="test.observed",
            schema_version=schema_version,
            payload=SamplePayload(label="invalid", count=0),
        )


def test_envelope_rejects_naive_timestamp_and_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        EventEnvelope[SamplePayload](
            event_type="test.observed",
            schema_version=1,
            occurred_at=datetime(2026, 9, 4, 12, 0),
            payload=SamplePayload(label="invalid", count=0),
        )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EventEnvelope[SamplePayload].model_validate(
            {
                "event_type": "test.observed",
                "schema_version": 1,
                "payload": {"label": "invalid", "count": 0},
                "unexpected": True,
            }
        )


def test_typed_envelope_json_round_trip_is_utf8_and_stable() -> None:
    event_id = uuid4()
    occurred_at = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    envelope = EventEnvelope[SamplePayload](
        event_id=event_id,
        event_type="test.observed",
        schema_version=1,
        occurred_at=occurred_at,
        payload=SamplePayload(label="سحابینو", count=3),
    )

    serialized = serialize_event(envelope)
    restored = deserialize_event(serialized, EventEnvelope[SamplePayload])

    assert serialized == serialize_event(envelope)
    assert "سحابینو" in serialized.decode("utf-8")
    assert restored == envelope
    assert restored.payload == SamplePayload(label="سحابینو", count=3)


def test_offset_timestamp_round_trip_preserves_instant_in_utc() -> None:
    source = datetime(2026, 9, 5, 18, 0, tzinfo=timezone(timedelta(hours=3, minutes=30)))
    envelope = EventEnvelope[SamplePayload](
        event_type="test.observed",
        schema_version=1,
        occurred_at=source,
        payload=SamplePayload(label="offset", count=1),
    )

    serialized = serialize_event(envelope)
    restored = deserialize_event(serialized, EventEnvelope[SamplePayload])

    assert b'"occurred_at":"2026-09-05T14:30:00Z"' in serialized
    assert restored.occurred_at == source.astimezone(UTC)
    assert restored.occurred_at.tzinfo is UTC


@pytest.mark.parametrize(
    "serialized",
    [
        b"not-json",
        b'{"event_type":"test.observed","schema_version":0,"payload":{}}',
        b'{"event_type":"test.observed","schema_version":1,"payload":{},"extra":true}',
    ],
)
def test_deserialization_wraps_invalid_json_or_envelopes(serialized: bytes) -> None:
    with pytest.raises(EventDeserializationError) as exc_info:
        deserialize_event(serialized, EventEnvelope[SamplePayload])

    assert isinstance(exc_info.value.__cause__, ValidationError)
