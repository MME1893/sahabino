from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import UUID, uuid4

import pytest

from sahabino.common.config import Settings
from sahabino.network.exceptions import CleanupConflictError, InvalidCaptureStateError
from sahabino.network.models import NetworkCapture
from sahabino.network.operations import cleanup_capture_object, reconcile_captures, retry_capture
from sahabino.network.storage import ObjectMetadata


def _capture(
    status: str, *, created_at: datetime, object_key: str = "captures/object"
) -> NetworkCapture:
    sha256 = "a" * 64
    return NetworkCapture(
        id=uuid4(),
        application_id=uuid4(),
        scenario="upload",
        original_filename="fixture.pcapng",
        capture_format="pcapng",
        content_type="application/vnd.tcpdump.pcap",
        object_key=object_key,
        expected_sha256=sha256,
        capture_size_bytes=100,
        transfer_file_size_bytes=50,
        status=status,
        ready_event_id=uuid4(),
        analysis_id=uuid4(),
        analysis_event_id=uuid4(),
        analysis_attempt_count=1,
        created_at=created_at,
    )


class _Result:
    def __init__(self, rows: list[tuple[NetworkCapture, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[NetworkCapture, str]]:
        return self._rows

    def one_or_none(self) -> tuple[NetworkCapture, str] | None:
        return self._rows[0] if self._rows else None


class _Context(AbstractContextManager["_Session"]):
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __enter__(self) -> _Session:
        return self.session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class _Session:
    def __init__(self, captures: list[NetworkCapture]) -> None:
        self.captures = captures
        self.events: list[str] = []

    def execute(self, statement: Any, parameters: Any = None) -> _Result:
        _ = statement
        self.events.append("lock" if parameters is not None else "query")
        return _Result([(capture, "com.example.network") for capture in self.captures])

    def get(self, model: type[NetworkCapture], capture_id: UUID) -> NetworkCapture | None:
        _ = model
        return next((capture for capture in self.captures if capture.id == capture_id), None)

    def scalars(self, statement: Any) -> list[NetworkCapture]:
        _ = statement
        self.events.append("references")
        return self.captures

    def refresh(self, capture: NetworkCapture) -> None:
        _ = capture
        self.events.append("refresh")

    def expunge(self, capture: NetworkCapture) -> None:
        _ = capture


class _SessionFactory:
    def __init__(self, captures: list[NetworkCapture]) -> None:
        self.session = _Session(captures)

    def begin(self) -> _Context:
        return _Context(self.session)

    def __call__(self) -> _Context:
        return _Context(self.session)


class _Storage:
    def __init__(self, objects: dict[str, int]) -> None:
        self.objects = objects
        self.deleted: list[str] = []

    def head_object(self, object_key: str) -> ObjectMetadata | None:
        size = self.objects.get(object_key)
        return ObjectMetadata(size_bytes=size) if size is not None else None

    def delete_object(self, object_key: str) -> None:
        self.deleted.append(object_key)
        self.objects.pop(object_key, None)

    def generate_presigned_put(self, object_key: str, content_type: str) -> str:
        raise NotImplementedError

    def generate_presigned_get(self, object_key: str) -> str:
        raise NotImplementedError

    def download_file(self, object_key: str, destination: Path) -> None:
        raise NotImplementedError

    def ensure_bucket(self) -> bool:
        return False


class _Publisher:
    def __init__(self) -> None:
        self.published: list[UUID] = []

    def publish_ready(self, capture: NetworkCapture, package_name: str) -> None:
        assert package_name == "com.example.network"
        self.published.append(capture.ready_event_id)

    def publish_analysis(self, capture: NetworkCapture, payload: object) -> None:
        raise NotImplementedError


def test_reconcile_recovers_upload_expires_missing_and_replays_stale_work() -> None:
    now = datetime.now(UTC)
    recovered = _capture("pending_upload", created_at=now - timedelta(hours=2), object_key="found")
    recovered.object_deleted_at = now - timedelta(days=1)
    expired = _capture("pending_upload", created_at=now - timedelta(hours=2), object_key="missing")
    uploaded = _capture("uploaded", created_at=now - timedelta(minutes=5), object_key="ready")
    stale = _capture("analyzing", created_at=now - timedelta(minutes=5), object_key="stale")
    stale.analysis_started_at = now - timedelta(hours=1)
    captures = [recovered, expired, uploaded, stale]
    factory = _SessionFactory(captures)
    storage = _Storage({"found": 100, "ready": 100, "stale": 100})
    publisher = _Publisher()
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        network_pending_upload_expiry_seconds=60,
        network_stale_analysis_seconds=60,
    )

    result = reconcile_captures(
        session_factory=factory,  # type: ignore[arg-type]
        storage=storage,
        publisher=publisher,
        settings=settings,
        now=now,
    )

    assert result == {"published": 3, "uploaded": 1, "expired": 1}
    assert recovered.status == "uploaded"
    assert recovered.object_deleted_at is None
    assert expired.status == "expired"
    assert expired.error_code == "upload_expired"
    assert publisher.published == [
        recovered.ready_event_id,
        uploaded.ready_event_id,
        stale.ready_event_id,
    ]


def test_retry_preserves_identities_and_only_accepts_failed_capture() -> None:
    failed = _capture("failed", created_at=datetime.now(UTC))
    ready_event_id = failed.ready_event_id
    analysis_id = failed.analysis_id
    publisher = _Publisher()

    retried = retry_capture(
        failed.id,
        session_factory=_SessionFactory([failed]),  # type: ignore[arg-type]
        publisher=publisher,
    )

    assert retried.status == "uploaded"
    assert retried.ready_event_id == ready_event_id
    assert retried.analysis_id == analysis_id
    assert publisher.published == [ready_event_id]
    with pytest.raises(InvalidCaptureStateError):
        retry_capture(
            failed.id,
            session_factory=_SessionFactory([failed]),  # type: ignore[arg-type]
            publisher=publisher,
        )

    deleted = _capture("failed", created_at=datetime.now(UTC))
    deleted.object_deleted_at = datetime.now(UTC)
    with pytest.raises(InvalidCaptureStateError, match="raw object was deleted"):
        retry_capture(
            deleted.id,
            session_factory=_SessionFactory([deleted]),  # type: ignore[arg-type]
            publisher=publisher,
        )


def test_cleanup_deletes_shared_terminal_object_once_and_rejects_live_reference() -> None:
    now = datetime.now(UTC)
    first = _capture("analyzed", created_at=now, object_key="shared")
    second = _capture("failed", created_at=now, object_key="shared")
    storage = _Storage({"shared": 100})

    factory = _SessionFactory([first, second])
    affected = cleanup_capture_object(
        first.id,
        session_factory=factory,  # type: ignore[arg-type]
        storage=storage,
    )

    assert affected == 2
    assert storage.deleted == ["shared"]
    assert first.object_deleted_at is not None
    assert second.object_deleted_at is not None
    assert factory.session.events[:3] == ["lock", "refresh", "references"]

    live = _capture("uploaded", created_at=now, object_key="live")
    with pytest.raises(CleanupConflictError):
        cleanup_capture_object(
            live.id,
            session_factory=_SessionFactory([live]),  # type: ignore[arg-type]
            storage=_Storage({"live": 100}),
        )
