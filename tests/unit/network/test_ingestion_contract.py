from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from sahabino.ingestion.exceptions import InvalidIngestionMessage
from sahabino.ingestion.repository import IngestionRepository
from sahabino.ingestion.worker import decode_message
from sahabino.messaging.events import EventEnvelope, serialize_event
from sahabino.messaging.network_events import (
    NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE,
    NetworkAnalysisCollectedV1,
    QuicMetricsV1,
    analysis_collected_envelope,
)
from sahabino.messaging.topics import NETWORK_ANALYSIS_COLLECTED_TOPIC
from tests.unit.network.fakes import analysis_payload


class FakeMessage:
    def __init__(self, *, topic: str, value: bytes, key: bytes) -> None:
        self._topic = topic
        self._value = value
        self._key = key

    def topic(self) -> str:
        return self._topic

    def value(self) -> bytes:
        return self._value

    def key(self) -> bytes:
        return self._key


def _event() -> EventEnvelope[NetworkAnalysisCollectedV1]:
    return analysis_collected_envelope(analysis_payload(), event_id=uuid4())


def test_ingestion_accepts_typed_network_analysis_on_canonical_topic() -> None:
    event = _event()
    message = FakeMessage(
        topic=NETWORK_ANALYSIS_COLLECTED_TOPIC,
        value=serialize_event(event),
        key=str(event.payload.application_id).encode(),
    )

    decoded = decode_message(message)  # type: ignore[arg-type]

    assert decoded.event_type == NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE
    assert decoded.payload == event.payload


def test_ingestion_preserves_unavailable_quic_versions_as_null() -> None:
    session = MagicMock()
    payload = analysis_payload().model_copy(
        update={
            "quic": QuicMetricsV1(
                quic_identified_connection_count=1,
                quic_version_count=None,
                quic_versions_seen=None,
                quic_retry_count=None,
                quic_version_negotiation_count=None,
                quic_0rtt_observed_count=None,
                quic_initial_rtt_avg_ms=None,
                quic_initial_rtt_p50_ms=None,
                quic_initial_rtt_p95_ms=None,
                quic_spin_rtt_sample_count=None,
                quic_spin_rtt_min_ms=None,
                quic_spin_rtt_p50_ms=None,
                quic_spin_rtt_p95_ms=None,
            )
        }
    )

    IngestionRepository(session).insert_network_analysis(payload)

    statement = session.execute.call_args.args[0]
    assert statement.compile().params["quic_versions_seen"] is None


@pytest.mark.parametrize(
    ("topic", "key", "reason"),
    [
        ("network.capture-ready.v1", None, "topic_event_mismatch"),
        (NETWORK_ANALYSIS_COLLECTED_TOPIC, b"wrong", "application_key_mismatch"),
    ],
)
def test_ingestion_rejects_network_topic_or_key_mismatch(
    topic: str, key: bytes | None, reason: str
) -> None:
    event = _event()
    message = FakeMessage(
        topic=topic,
        value=serialize_event(event),
        key=key if key is not None else str(event.payload.application_id).encode(),
    )

    with pytest.raises(InvalidIngestionMessage) as error:
        decode_message(message)  # type: ignore[arg-type]

    assert error.value.reason == reason


def test_ingestion_rejects_unsupported_network_schema() -> None:
    event = _event().model_copy(update={"schema_version": 2})
    message = FakeMessage(
        topic=NETWORK_ANALYSIS_COLLECTED_TOPIC,
        value=serialize_event(event),
        key=str(event.payload.application_id).encode(),
    )

    with pytest.raises(InvalidIngestionMessage) as error:
        decode_message(message)  # type: ignore[arg-type]

    assert error.value.reason == "unsupported_schema_version"
