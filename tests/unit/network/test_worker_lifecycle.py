from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from uuid import uuid4

import pytest

from sahabino.messaging.events import serialize_event
from sahabino.messaging.network_events import capture_ready_envelope
from sahabino.messaging.topics import NETWORK_CAPTURE_READY_TOPIC
from sahabino.network import worker as worker_module
from sahabino.network.analyzer import AnalysisMetrics
from sahabino.network.exceptions import (
    CaptureDispatchError,
    CaptureObjectMissingError,
    StorageUnavailableError,
    TerminalAnalysisError,
)
from sahabino.network.models import NetworkCapture
from sahabino.network.worker import AnalysisClaimUnavailable, NetworkAnalyzerWorker
from tests.unit.network.fakes import analysis_payload, capture_ready_payload


class FakeMessage:
    def __init__(self, value: bytes, key: bytes) -> None:
        self._value = value
        self._key = key

    def topic(self) -> str:
        return NETWORK_CAPTURE_READY_TOPIC

    def value(self) -> bytes:
        return self._value

    def key(self) -> bytes:
        return self._key

    def partition(self) -> int:
        return 0

    def offset(self) -> int:
        return 7


class FakeConsumer:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message
        self.commits = 0
        self.commit_error: Exception | None = None

    def poll(self, timeout: float) -> FakeMessage:
        _ = timeout
        return self.message

    def commit(self, message: FakeMessage) -> None:
        _ = message
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    def close(self) -> None:
        return None


class FakeStorage:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def download_file(self, object_key: str, destination: Path) -> None:
        _ = object_key
        if self.error is not None:
            raise self.error
        destination.write_bytes(bytes.fromhex("d4c3b2a1") + b"\x00" * 20)


class FakePublisher:
    def __init__(self) -> None:
        self.published = 0
        self.error: Exception | None = None

    def publish_analysis(self, capture: NetworkCapture, payload: object) -> None:
        _ = capture
        _ = payload
        if self.error is not None:
            raise self.error
        self.published += 1

    def publish_ready(self, capture: NetworkCapture, package_name: str) -> None:
        raise NotImplementedError


class FakeSession(AbstractContextManager["FakeSession"]):
    def __init__(self, capture: NetworkCapture) -> None:
        self.capture = capture

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        _ = (exc_type, exc_value, traceback)
        return False

    def get(self, model: object, capture_id: object) -> NetworkCapture | None:
        _ = model
        return self.capture if self.capture.id == capture_id else None


class FakeSessionFactory:
    def __init__(self, capture: NetworkCapture) -> None:
        self.capture = capture

    def begin(self) -> FakeSession:
        return FakeSession(self.capture)

    def __call__(self) -> FakeSession:
        return FakeSession(self.capture)


class FakeRepository:
    capture: NetworkCapture

    def __init__(self, session: FakeSession) -> None:
        self.capture = session.capture

    def load_and_validate(self, event: object) -> tuple[NetworkCapture, str]:
        _ = event
        return self.capture, "com.example.network"

    def claim_attempt(
        self,
        capture_id: object,
        *,
        attempt_started_at: datetime,
        stale_before: datetime,
    ) -> bool:
        _ = capture_id
        if self.capture.status == "uploaded" or (
            self.capture.status == "analyzing"
            and (
                self.capture.analysis_started_at is None
                or self.capture.analysis_started_at <= stale_before
            )
        ):
            self.capture.status = "analyzing"
            self.capture.analysis_attempt_count += 1
            self.capture.analysis_started_at = attempt_started_at
            return True
        return False

    def release_attempt(self, capture_id: object, attempt_started_at: datetime) -> None:
        _ = capture_id
        if (
            self.capture.status == "analyzing"
            and self.capture.analysis_started_at == attempt_started_at
        ):
            self.capture.status = "uploaded"

    def mark_verified(self, capture_id: object, sha256: str, attempt_started_at: datetime) -> None:
        _ = capture_id
        assert self.capture.analysis_started_at == attempt_started_at
        self.capture.verified_sha256 = sha256

    def mark_analyzed(self, capture_id: object, sha256: str, attempt_started_at: datetime) -> None:
        _ = capture_id
        assert self.capture.analysis_started_at == attempt_started_at
        self.capture.verified_sha256 = sha256
        self.capture.status = "analyzed"

    def mark_failed(
        self,
        capture_id: object,
        error: TerminalAnalysisError,
        attempt_started_at: datetime,
    ) -> None:
        _ = capture_id
        assert self.capture.analysis_started_at == attempt_started_at
        self.capture.status = "failed"
        self.capture.error_code = error.code


def _metrics() -> AnalysisMetrics:
    payload = analysis_payload()
    return AnalysisMetrics.model_validate(
        payload.model_dump(
            exclude={"analysis_id", "capture_id", "application_id", "package_name", "scenario"}
        )
    )


def _worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage_error: Exception | None = None,
) -> tuple[NetworkAnalyzerWorker, NetworkCapture, FakeConsumer, FakePublisher]:
    payload = capture_ready_payload()
    event_id = uuid4()
    event = capture_ready_envelope(payload, event_id=event_id)
    capture = NetworkCapture(
        id=payload.capture_id,
        application_id=payload.application_id,
        scenario=payload.scenario,
        original_filename="synthetic.pcap",
        capture_format=payload.capture_format,
        content_type="application/vnd.tcpdump.pcap",
        object_key=payload.object_key,
        expected_sha256=payload.expected_sha256,
        capture_size_bytes=payload.capture_size_bytes,
        transfer_file_size_bytes=payload.transfer_file_size_bytes,
        status="uploaded",
        ready_event_id=event_id,
        analysis_id=payload.analysis_id,
        analysis_event_id=uuid4(),
        analysis_attempt_count=0,
        uploaded_at=payload.uploaded_at,
    )
    message = FakeMessage(serialize_event(event), str(payload.application_id).encode())
    consumer = FakeConsumer(message)
    publisher = FakePublisher()
    monkeypatch.setattr(worker_module, "AnalyzerRepository", FakeRepository)
    monkeypatch.setattr(worker_module, "analyze_capture", lambda *args, **kwargs: _metrics())
    worker = NetworkAnalyzerWorker(
        consumer=consumer,  # type: ignore[arg-type]
        session_factory=FakeSessionFactory(capture),  # type: ignore[arg-type]
        storage=FakeStorage(storage_error),  # type: ignore[arg-type]
        publisher=publisher,  # type: ignore[arg-type]
        engine=object(),  # type: ignore[arg-type]
        consumer_group="test-network-analyzer",
    )
    return worker, capture, consumer, publisher


def test_success_marks_analyzed_before_offset_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, capture, consumer, publisher = _worker(monkeypatch)

    assert worker.process_next() is True

    assert capture.status == "analyzed"
    assert capture.analysis_attempt_count == 1
    assert publisher.published == 1
    assert consumer.commits == 1


def test_commit_failure_redelivery_acknowledges_without_reanalysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, capture, consumer, publisher = _worker(monkeypatch)
    consumer.commit_error = RuntimeError("Kafka commit unavailable")

    with pytest.raises(RuntimeError, match="Kafka commit unavailable"):
        worker.process_next()

    assert capture.status == "analyzed"
    assert capture.analysis_attempt_count == 1
    assert publisher.published == 1
    assert consumer.commits == 0

    consumer.commit_error = None
    assert worker.process_next() is True

    assert capture.status == "analyzed"
    assert capture.analysis_attempt_count == 1
    assert publisher.published == 1
    assert consumer.commits == 1


def test_publish_failure_releases_attempt_and_does_not_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, capture, consumer, publisher = _worker(monkeypatch)
    publisher.error = CaptureDispatchError("Kafka unavailable")

    with pytest.raises(CaptureDispatchError):
        worker.process_next()

    assert capture.status == "uploaded"
    assert consumer.commits == 0

    publisher.error = None
    assert worker.process_next() is True

    assert capture.status == "analyzed"
    assert capture.analysis_attempt_count == 2
    assert publisher.published == 1
    assert consumer.commits == 1


def test_terminal_content_failure_is_persisted_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, capture, consumer, publisher = _worker(monkeypatch)
    monkeypatch.setattr(
        worker_module,
        "analyze_capture",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TerminalAnalysisError("checksum_mismatch", "bad checksum")
        ),
    )

    assert worker.process_next() is True

    assert capture.status == "failed"
    assert capture.error_code == "checksum_mismatch"
    assert publisher.published == 0
    assert consumer.commits == 1


def test_analyzed_redelivery_skips_work_and_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, capture, consumer, publisher = _worker(monkeypatch)
    capture.status = "analyzed"

    assert worker.process_next() is True

    assert capture.analysis_attempt_count == 0
    assert publisher.published == 0
    assert consumer.commits == 1


def test_missing_completed_object_is_terminal_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, capture, consumer, publisher = _worker(
        monkeypatch,
        storage_error=CaptureObjectMissingError("gone"),
    )

    assert worker.process_next() is True

    assert capture.status == "failed"
    assert capture.error_code == "capture_object_missing"
    assert capture.analysis_attempt_count == 1
    assert publisher.published == 0
    assert consumer.commits == 1


def test_transient_storage_failure_is_retryable_and_not_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, capture, consumer, _ = _worker(
        monkeypatch,
        storage_error=StorageUnavailableError("temporary outage"),
    )

    with pytest.raises(StorageUnavailableError, match="temporary outage"):
        worker.process_next()

    assert capture.status == "uploaded"
    assert capture.analysis_attempt_count == 1
    assert consumer.commits == 0


def test_fresh_analyzing_attempt_cannot_be_stolen(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, capture, consumer, publisher = _worker(monkeypatch)
    started_at = datetime.now(UTC)
    capture.status = "analyzing"
    capture.analysis_attempt_count = 1
    capture.analysis_started_at = started_at

    with pytest.raises(AnalysisClaimUnavailable):
        worker.process_next()

    assert capture.analysis_attempt_count == 1
    assert capture.analysis_started_at == started_at
    assert publisher.published == 0
    assert consumer.commits == 0


def test_stale_analyzing_attempt_is_resumed_with_fresh_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker, capture, consumer, _ = _worker(monkeypatch)
    stale_started_at = datetime.now(UTC) - timedelta(hours=1)
    capture.status = "analyzing"
    capture.analysis_attempt_count = 1
    capture.analysis_started_at = stale_started_at

    assert worker.process_next() is True

    assert capture.status == "analyzed"
    assert capture.analysis_attempt_count == 2
    assert capture.analysis_started_at is not None
    assert capture.analysis_started_at > stale_started_at
    assert consumer.commits == 1


def test_uploaded_retry_refreshes_old_attempt_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, capture, _, _ = _worker(monkeypatch)
    old_started_at = datetime.now(UTC) - timedelta(hours=2)
    capture.analysis_attempt_count = 1
    capture.analysis_started_at = old_started_at

    assert worker.process_next() is True

    assert capture.analysis_attempt_count == 2
    assert capture.analysis_started_at is not None
    assert capture.analysis_started_at > old_started_at
