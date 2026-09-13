from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from sahabino.app_registry.models import Application
from sahabino.common.config import Settings
from sahabino.network.exceptions import (
    CaptureNotFoundError,
    CaptureObjectSizeMismatchError,
    CleanupConflictError,
    InvalidCaptureStateError,
)
from sahabino.network.models import NetworkCapture
from sahabino.network.publisher import NetworkPublisher
from sahabino.network.storage import ObjectStorage


def reconcile_captures(
    *,
    session_factory: sessionmaker[Session],
    storage: ObjectStorage,
    publisher: NetworkPublisher,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, int]:
    current = now or datetime.now(UTC)
    pending_cutoff = current - timedelta(seconds=settings.network_pending_upload_expiry_seconds)
    analyzing_cutoff = current - timedelta(seconds=settings.network_stale_analysis_seconds)
    to_publish: list[tuple[UUID, str]] = []
    expired = 0
    uploaded = 0
    with session_factory.begin() as session:
        rows = session.execute(
            select(NetworkCapture, Application.package_name)
            .join(Application, Application.id == NetworkCapture.application_id)
            .where(NetworkCapture.status.in_(("pending_upload", "uploaded", "analyzing")))
            .order_by(NetworkCapture.created_at, NetworkCapture.id)
        ).all()
        for capture, package_name in rows:
            if capture.status == "pending_upload":
                if capture.created_at > pending_cutoff:
                    continue
                metadata = storage.head_object(capture.object_key)
                if metadata is None:
                    capture.status = "expired"
                    capture.error_code = "upload_expired"
                    capture.error_message = "No object was found before the upload deadline"
                    expired += 1
                    continue
                if metadata.size_bytes != capture.capture_size_bytes:
                    capture.status = "expired"
                    capture.error_code = "capture_object_size_mismatch"
                    capture.error_message = "Uploaded object size did not match registered size"
                    expired += 1
                    continue
                capture.status = "uploaded"
                capture.uploaded_at = current
                capture.object_deleted_at = None
                capture.error_code = None
                capture.error_message = None
                uploaded += 1
                to_publish.append((capture.id, package_name))
            elif capture.status == "uploaded" or (
                capture.status == "analyzing"
                and capture.analysis_started_at is not None
                and capture.analysis_started_at <= analyzing_cutoff
            ):
                to_publish.append((capture.id, package_name))

    for capture_id, package_name in to_publish:
        with session_factory() as session:
            capture = session.get(NetworkCapture, capture_id)
            if capture is not None:
                publisher.publish_ready(capture, package_name)
    return {"published": len(to_publish), "uploaded": uploaded, "expired": expired}


def retry_capture(
    capture_id: UUID,
    *,
    session_factory: sessionmaker[Session],
    publisher: NetworkPublisher,
) -> NetworkCapture:
    with session_factory.begin() as session:
        row = session.execute(
            select(NetworkCapture, Application.package_name)
            .join(Application, Application.id == NetworkCapture.application_id)
            .where(NetworkCapture.id == capture_id)
        ).one_or_none()
        if row is None:
            raise CaptureNotFoundError(capture_id)
        capture, package_name = row
        if capture.status != "failed":
            raise InvalidCaptureStateError("Only a failed capture is eligible for explicit retry")
        if capture.object_deleted_at is not None:
            raise InvalidCaptureStateError(
                "A capture whose raw object was deleted cannot be retried"
            )
        capture.status = "uploaded"
        capture.error_code = None
        capture.error_message = None
        capture.analysis_finished_at = None
    with session_factory() as session:
        capture = session.get(NetworkCapture, capture_id)
        assert capture is not None
        publisher.publish_ready(capture, package_name)
        session.expunge(capture)
        return cast(NetworkCapture, capture)


def cleanup_capture_object(
    capture_id: UUID,
    *,
    session_factory: sessionmaker[Session],
    storage: ObjectStorage,
) -> int:
    with session_factory.begin() as session:
        capture = session.get(NetworkCapture, capture_id)
        if capture is None:
            raise CaptureNotFoundError(capture_id)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:object_key, 0))"),
            {"object_key": capture.object_key},
        )
        session.refresh(capture)
        references = list(
            session.scalars(
                select(NetworkCapture).where(
                    NetworkCapture.object_key == capture.object_key,
                    NetworkCapture.object_deleted_at.is_(None),
                )
            )
        )
        nonterminal = [
            item for item in references if item.status not in {"analyzed", "failed", "expired"}
        ]
        if nonterminal:
            raise CleanupConflictError(
                "Raw object is still required by a non-terminal logical capture"
            )
        if not references:
            return 0
        metadata = storage.head_object(capture.object_key)
        if metadata is not None:
            storage.delete_object(capture.object_key)
        deleted_at = datetime.now(UTC)
        for item in references:
            item.object_deleted_at = deleted_at
        return len(references)


def verify_capture_object_size(capture: NetworkCapture, storage: ObjectStorage) -> None:
    metadata = storage.head_object(capture.object_key)
    if metadata is not None and metadata.size_bytes != capture.capture_size_bytes:
        raise CaptureObjectSizeMismatchError("Capture object size does not match metadata")
