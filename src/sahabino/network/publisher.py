from __future__ import annotations

from typing import Protocol

from sahabino.messaging.network_events import (
    NetworkAnalysisCollectedV1,
    NetworkCaptureReadyV1,
    analysis_collected_envelope,
    capture_ready_envelope,
)
from sahabino.messaging.producer import KafkaProducer
from sahabino.messaging.topics import (
    NETWORK_ANALYSIS_COLLECTED_TOPIC,
    NETWORK_CAPTURE_READY_TOPIC,
)
from sahabino.network.exceptions import CaptureDispatchError
from sahabino.network.models import NetworkCapture


class NetworkPublisher(Protocol):
    def publish_ready(self, capture: NetworkCapture, package_name: str) -> None: ...

    def publish_analysis(
        self, capture: NetworkCapture, payload: NetworkAnalysisCollectedV1
    ) -> None: ...


class KafkaNetworkPublisher:
    def __init__(self, producer: KafkaProducer) -> None:
        self._producer = producer

    def publish_ready(self, capture: NetworkCapture, package_name: str) -> None:
        if capture.uploaded_at is None:
            raise ValueError("an uploaded timestamp is required before publication")
        payload = NetworkCaptureReadyV1(
            capture_id=capture.id,
            analysis_id=capture.analysis_id,
            application_id=capture.application_id,
            package_name=package_name,
            scenario=capture.scenario,  # type: ignore[arg-type]
            object_key=capture.object_key,
            capture_format=capture.capture_format,  # type: ignore[arg-type]
            capture_size_bytes=capture.capture_size_bytes,
            transfer_file_size_bytes=capture.transfer_file_size_bytes,
            expected_sha256=capture.expected_sha256,
            uploaded_at=capture.uploaded_at,
        )
        try:
            self._producer.publish(
                topic=NETWORK_CAPTURE_READY_TOPIC,
                key=str(capture.application_id),
                event=capture_ready_envelope(payload, event_id=capture.ready_event_id),
            )
            self._producer.flush()
        except Exception as error:
            raise CaptureDispatchError("Could not publish the capture-ready event") from error

    def publish_analysis(
        self, capture: NetworkCapture, payload: NetworkAnalysisCollectedV1
    ) -> None:
        try:
            self._producer.publish(
                topic=NETWORK_ANALYSIS_COLLECTED_TOPIC,
                key=str(capture.application_id),
                event=analysis_collected_envelope(payload, event_id=capture.analysis_event_id),
            )
            self._producer.flush()
        except Exception as error:
            raise CaptureDispatchError("Could not publish the analysis event") from error

    def close(self) -> None:
        self._producer.close()
