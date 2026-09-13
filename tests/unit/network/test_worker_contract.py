from __future__ import annotations

from uuid import uuid4

import pytest

from sahabino.messaging.events import serialize_event
from sahabino.messaging.network_events import capture_ready_envelope
from sahabino.messaging.topics import NETWORK_CAPTURE_READY_TOPIC
from sahabino.network.worker import InvalidAnalyzerMessage, decode_capture_ready
from tests.unit.network.fakes import capture_ready_payload


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


def _message(*, topic: str = NETWORK_CAPTURE_READY_TOPIC, key: bytes | None = None) -> FakeMessage:
    payload = capture_ready_payload()
    event = capture_ready_envelope(payload, event_id=uuid4())
    return FakeMessage(
        topic=topic,
        value=serialize_event(event),
        key=key if key is not None else str(payload.application_id).encode(),
    )


def test_capture_ready_decoder_validates_topic_key_and_typed_contract() -> None:
    event = decode_capture_ready(_message())  # type: ignore[arg-type]

    assert event.payload.package_name == "com.example.network"


@pytest.mark.parametrize(
    "message",
    [
        _message(topic="network.analysis-collected.v1"),
        _message(key=b"not-the-application-id"),
    ],
)
def test_capture_ready_decoder_rejects_wrong_topic_or_key(message: FakeMessage) -> None:
    with pytest.raises(InvalidAnalyzerMessage):
        decode_capture_ready(message)  # type: ignore[arg-type]
