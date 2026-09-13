from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sahabino.common.config import Settings
from sahabino.network.exceptions import (
    CaptureMetadataConflictError,
    CaptureNotFoundError,
    CaptureObjectMissingError,
    CaptureObjectSizeMismatchError,
    CaptureTooLargeError,
    IdempotencyConflictError,
    InvalidCaptureStateError,
)
from sahabino.network.models import NetworkCapture, NetworkCaptureIdempotencyKey
from sahabino.network.publisher import NetworkPublisher
from sahabino.network.repository import CaptureRepository
from sahabino.network.schemas import CaptureCreate, CaptureCreateResponse, DownloadUrlResponse
from sahabino.network.storage import ObjectStorage

logger = logging.getLogger(__name__)


def object_key_for(sha256: str, capture_format: str) -> str:
    return f"captures/sha256/{sha256[:2]}/{sha256}.{capture_format}"


def _request_fingerprint(data: CaptureCreate) -> str:
    canonical_request = {
        "application_id": str(data.application_id),
        "capture_format": data.capture_format,
        "capture_size_bytes": data.capture_size_bytes,
        "expected_sha256": data.sha256,
        "original_filename": data.filename,
        "scenario": data.scenario,
        "transfer_file_size_bytes": data.transfer_file_size_bytes,
    }
    serialized = json.dumps(
        canonical_request,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_logical_metadata(capture: NetworkCapture, data: CaptureCreate) -> None:
    if (
        capture.capture_format != data.capture_format
        or capture.capture_size_bytes != data.capture_size_bytes
        or capture.transfer_file_size_bytes != data.transfer_file_size_bytes
    ):
        raise CaptureMetadataConflictError


class CaptureService:
    def __init__(
        self,
        session: AsyncSession,
        storage: ObjectStorage,
        publisher: NetworkPublisher,
        settings: Settings,
    ) -> None:
        self._session = session
        self._storage = storage
        self._publisher = publisher
        self._settings = settings
        self._repository = CaptureRepository(session)

    async def create(self, data: CaptureCreate, idempotency_key: UUID) -> CaptureCreateResponse:
        request_fingerprint = _request_fingerprint(data)
        await self._repository.lock_idempotency_key(idempotency_key)

        mapping = await self._repository.get_idempotency_key(idempotency_key)
        if mapping is not None:
            if mapping.request_fingerprint != request_fingerprint:
                raise IdempotencyConflictError
            mapped_capture = await self._repository.get(mapping.capture_id)
            if mapped_capture is None:
                raise RuntimeError("Idempotency mapping references a missing network capture")
            await self._repository.lock_object_key(mapped_capture.object_key)
            await self._repository.refresh(mapped_capture)
            if self._reopen_expired(mapped_capture):
                await self._session.commit()
            await self._publish_ready_if_dispatchable(mapped_capture)
            return await self._create_response(mapped_capture)

        if data.capture_size_bytes > self._settings.object_storage_max_capture_size_bytes:
            raise CaptureTooLargeError("capture_size_bytes exceeds the configured maximum")

        object_key = object_key_for(data.sha256, data.capture_format)
        await self._repository.lock_object_key(object_key)

        logical = await self._repository.get_logical(
            application_id=data.application_id,
            scenario=data.scenario,
            expected_sha256=data.sha256,
        )
        if logical is not None:
            if logical.object_key != object_key:
                await self._repository.lock_object_key(logical.object_key)
                await self._repository.refresh(logical)
            _validate_logical_metadata(logical, data)
            self._reopen_expired(logical)
            self._repository.add_idempotency_key(
                NetworkCaptureIdempotencyKey(
                    idempotency_key=idempotency_key,
                    capture_id=logical.id,
                    request_fingerprint=request_fingerprint,
                )
            )
            await self._repository.flush()
            await self._session.commit()
            await self._publish_ready_if_dispatchable(logical)
            return await self._create_response(logical)

        package_name = await self._repository.application_package_name(data.application_id)
        if package_name is None:
            from sahabino.app_registry.exceptions import ApplicationNotFoundError

            raise ApplicationNotFoundError(data.application_id)

        verified = await self._repository.get_verified_object(object_key)
        if verified is not None and verified.capture_size_bytes != data.capture_size_bytes:
            raise CaptureObjectSizeMismatchError(
                "capture_size_bytes conflicts with the verified content-addressed object"
            )
        if verified is not None:
            metadata = await asyncio.to_thread(self._storage.head_object, object_key)
            if metadata is None:
                logger.warning(
                    "verified capture object is missing; requiring a fresh upload",
                    extra={
                        "event": "network.capture.verified_object_missing",
                        "object_key": object_key,
                        "verified_capture_id": str(verified.id),
                    },
                )
                verified = None
            elif metadata.size_bytes != data.capture_size_bytes:
                raise CaptureObjectSizeMismatchError(
                    "The verified content-addressed object size is inconsistent with storage"
                )
        now = datetime.now(UTC)
        capture = NetworkCapture(
            id=uuid4(),
            application_id=data.application_id,
            scenario=data.scenario,
            original_filename=data.filename,
            capture_format=data.capture_format,
            content_type=data.content_type,
            object_key=object_key,
            expected_sha256=data.sha256,
            verified_sha256=verified.verified_sha256 if verified is not None else None,
            capture_size_bytes=data.capture_size_bytes,
            transfer_file_size_bytes=data.transfer_file_size_bytes,
            status="uploaded" if verified is not None else "pending_upload",
            uploaded_at=now if verified is not None else None,
            ready_event_id=uuid4(),
            analysis_id=uuid4(),
            analysis_event_id=uuid4(),
        )
        try:
            self._repository.add(capture)
            self._repository.add_idempotency_key(
                NetworkCaptureIdempotencyKey(
                    idempotency_key=idempotency_key,
                    capture_id=capture.id,
                    request_fingerprint=request_fingerprint,
                )
            )
            await self._repository.flush()
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            await self._repository.lock_idempotency_key(idempotency_key)
            mapping = await self._repository.get_idempotency_key(idempotency_key)
            if mapping is not None:
                if mapping.request_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError from None
                winner = await self._repository.get(mapping.capture_id)
                if winner is None:
                    raise RuntimeError(
                        "Idempotency mapping references a missing network capture"
                    ) from None
                await self._repository.lock_object_key(winner.object_key)
                await self._repository.refresh(winner)
                if self._reopen_expired(winner):
                    await self._session.commit()
                await self._publish_ready_if_dispatchable(winner)
                return await self._create_response(winner)

            await self._repository.lock_object_key(object_key)
            winner = await self._repository.get_logical(
                application_id=data.application_id,
                scenario=data.scenario,
                expected_sha256=data.sha256,
            )
            if winner is not None:
                if winner.object_key != object_key:
                    await self._repository.lock_object_key(winner.object_key)
                    await self._repository.refresh(winner)
                _validate_logical_metadata(winner, data)
                self._reopen_expired(winner)
                self._repository.add_idempotency_key(
                    NetworkCaptureIdempotencyKey(
                        idempotency_key=idempotency_key,
                        capture_id=winner.id,
                        request_fingerprint=request_fingerprint,
                    )
                )
                await self._repository.flush()
                await self._session.commit()
                await self._publish_ready_if_dispatchable(winner)
                return await self._create_response(winner)
            raise

        logger.info(
            "network capture created",
            extra={
                "event": "network.capture.created",
                "capture_id": str(capture.id),
                "analysis_id": str(capture.analysis_id),
                "application_id": str(capture.application_id),
                "scenario": capture.scenario,
                "status": capture.status,
            },
        )
        await self._publish_ready_if_dispatchable(capture, package_name=package_name)
        return await self._create_response(capture)

    async def get(self, capture_id: UUID) -> NetworkCapture:
        capture = await self._repository.get(capture_id)
        if capture is None:
            raise CaptureNotFoundError(capture_id)
        return capture

    async def list(
        self,
        *,
        application_id: UUID | None,
        scenario: str | None,
        status: str | None,
    ) -> list[NetworkCapture]:
        return await self._repository.list(
            application_id=application_id, scenario=scenario, status=status
        )

    async def complete(self, capture_id: UUID) -> NetworkCapture:
        capture = await self.get(capture_id)
        if capture.status in {"failed", "expired"}:
            raise InvalidCaptureStateError(
                f"Capture in status {capture.status!r} cannot be completed"
            )
        metadata = await asyncio.to_thread(self._storage.head_object, capture.object_key)
        if metadata is None:
            raise CaptureObjectMissingError("The uploaded capture object was not found")
        if metadata.size_bytes != capture.capture_size_bytes:
            raise CaptureObjectSizeMismatchError(
                "The uploaded object size does not match capture_size_bytes"
            )
        if capture.status == "pending_upload":
            capture.status = "uploaded"
            capture.uploaded_at = datetime.now(UTC)
            capture.object_deleted_at = None
            capture.error_code = None
            capture.error_message = None
            await self._session.commit()
        if capture.status in {"uploaded", "analyzing"}:
            package_name = await self._repository.package_name_for_capture(capture)
            await asyncio.to_thread(self._publisher.publish_ready, capture, package_name)
            logger.info(
                "capture-ready event published",
                extra={
                    "event": "network.capture.ready_published",
                    "capture_id": str(capture.id),
                    "analysis_id": str(capture.analysis_id),
                    "event_id": str(capture.ready_event_id),
                    "application_id": str(capture.application_id),
                    "scenario": capture.scenario,
                    "status": capture.status,
                },
            )
        return capture

    async def download_url(self, capture_id: UUID) -> DownloadUrlResponse:
        capture = await self.get(capture_id)
        if capture.status == "pending_upload" or capture.object_deleted_at is not None:
            raise InvalidCaptureStateError("Capture raw data is not available for download")
        metadata = await asyncio.to_thread(self._storage.head_object, capture.object_key)
        if metadata is None:
            raise CaptureObjectMissingError("The capture object was not found")
        url = await asyncio.to_thread(self._storage.generate_presigned_get, capture.object_key)
        return DownloadUrlResponse(
            capture_id=capture.id,
            download_url=url,
            expires_in=self._settings.object_storage_presign_expiry_seconds,
        )

    async def _create_response(self, capture: NetworkCapture) -> CaptureCreateResponse:
        upload_required = capture.status == "pending_upload"
        upload_url = None
        expires_in = None
        if upload_required:
            upload_url = await asyncio.to_thread(
                self._storage.generate_presigned_put,
                capture.object_key,
                capture.content_type,
            )
            expires_in = self._settings.object_storage_presign_expiry_seconds
        else:
            logger.info(
                "network capture reused",
                extra={
                    "event": "network.capture.reused",
                    "capture_id": str(capture.id),
                    "analysis_id": str(capture.analysis_id),
                    "application_id": str(capture.application_id),
                    "scenario": capture.scenario,
                    "status": capture.status,
                },
            )
        return CaptureCreateResponse(
            capture_id=capture.id,
            analysis_id=capture.analysis_id,
            ready_event_id=capture.ready_event_id,
            status=capture.status,  # type: ignore[arg-type]
            upload_required=upload_required,
            upload_url=upload_url,
            expires_in=expires_in,
        )

    def _reopen_expired(self, capture: NetworkCapture) -> bool:
        if capture.status != "expired":
            return False
        capture.status = "pending_upload"
        capture.verified_sha256 = None
        capture.uploaded_at = None
        capture.analysis_started_at = None
        capture.analysis_finished_at = None
        capture.error_code = None
        capture.error_message = None
        return True

    async def _publish_ready_if_dispatchable(
        self,
        capture: NetworkCapture,
        *,
        package_name: str | None = None,
    ) -> None:
        """Re-publish the stable ready event when an upload is ready for processing.

        Replays are intentional: Kafka delivery and the analyzer are idempotent by the
        persisted ready_event_id/analysis_id, and this closes the DB-commit/Kafka-publish
        failure window for repeated create requests.
        """
        if capture.status not in {"uploaded", "analyzing"}:
            return
        if package_name is None:
            package_name = await self._repository.package_name_for_capture(capture)
        await asyncio.to_thread(self._publisher.publish_ready, capture, package_name)
