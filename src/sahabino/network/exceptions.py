from __future__ import annotations

from uuid import UUID


class NetworkCaptureError(Exception):
    code = "network_capture_error"


class CaptureNotFoundError(NetworkCaptureError):
    code = "capture_not_found"

    def __init__(self, capture_id: UUID) -> None:
        super().__init__(f"Network capture {capture_id} was not found")


class IdempotencyConflictError(NetworkCaptureError):
    code = "idempotency_conflict"

    def __init__(self) -> None:
        super().__init__("Idempotency-Key was already used for a different capture request")


class CaptureMetadataConflictError(NetworkCaptureError):
    code = "capture_metadata_conflict"

    def __init__(self) -> None:
        super().__init__(
            "capture_format, capture_size_bytes, and transfer_file_size_bytes must match "
            "the existing logical capture"
        )


class InvalidCaptureStateError(NetworkCaptureError):
    code = "invalid_capture_state"


class CaptureObjectMissingError(NetworkCaptureError):
    code = "capture_object_missing"


class CaptureObjectSizeMismatchError(NetworkCaptureError):
    code = "capture_object_size_mismatch"


class StorageUnavailableError(NetworkCaptureError):
    code = "object_storage_unavailable"


class CaptureTooLargeError(NetworkCaptureError):
    code = "capture_too_large"


class CaptureDispatchError(NetworkCaptureError):
    code = "capture_dispatch_unavailable"


class CleanupConflictError(NetworkCaptureError):
    code = "cleanup_conflict"


class TerminalAnalysisError(NetworkCaptureError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)
