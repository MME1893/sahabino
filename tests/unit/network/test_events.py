from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sahabino.messaging.events import EventEnvelope, deserialize_event, serialize_event
from sahabino.messaging.network_events import (
    NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE,
    NETWORK_CAPTURE_READY_EVENT_TYPE,
    NetworkAnalysisCollectedV1,
    NetworkCaptureReadyV1,
    analysis_collected_envelope,
    capture_ready_envelope,
)
from tests.unit.network.fakes import analysis_payload, capture_ready_payload


def test_network_capture_ready_contract_and_stable_event_id_round_trip() -> None:
    event_id = uuid4()
    payload = capture_ready_payload()
    event = capture_ready_envelope(payload, event_id=event_id)

    decoded = deserialize_event(serialize_event(event), EventEnvelope[NetworkCaptureReadyV1])

    assert decoded == event
    assert decoded.event_id == event_id
    assert decoded.event_type == NETWORK_CAPTURE_READY_EVENT_TYPE
    assert decoded.payload.uploaded_at.utcoffset() is not None


def test_network_analysis_contract_preserves_null_transport_semantics() -> None:
    event_id = uuid4()
    payload = analysis_payload()
    event = analysis_collected_envelope(payload, event_id=event_id)

    decoded = deserialize_event(serialize_event(event), EventEnvelope[NetworkAnalysisCollectedV1])

    assert decoded.event_id == event_id
    assert decoded.event_type == NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE
    assert decoded.payload.tcp is None
    assert decoded.payload.quic is None
    assert decoded.payload.traffic.tx_network_bytes is None


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"expected_sha256": "A" * 64}, "string_pattern_mismatch"),
        ({"capture_size_bytes": 0}, "greater_than"),
        ({"uploaded_at": datetime(2026, 9, 12)}, "timezone-aware"),
        ({"unexpected": "field"}, "extra_forbidden"),
    ],
)
def test_capture_ready_rejects_invalid_contract(changes: dict[str, object], match: str) -> None:
    values = capture_ready_payload().model_dump()
    values.update(changes)

    with pytest.raises(ValidationError, match=match):
        NetworkCaptureReadyV1.model_validate(values)


def test_nested_metrics_reject_invalid_ratios() -> None:
    values = analysis_payload().model_dump()
    values["traffic"]["ip_transport_header_overhead_ratio"] = 1.1

    with pytest.raises(ValidationError):
        NetworkAnalysisCollectedV1.model_validate(values)
