from __future__ import annotations

from datetime import datetime
from pathlib import PurePath
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from sahabino.messaging.network_events import Sha256
from sahabino.network.models import NetworkCapture

NonBlankString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CaptureStatus = Literal["pending_upload", "uploaded", "analyzing", "analyzed", "failed", "expired"]


class CaptureCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: UUID
    scenario: Literal["upload", "download"]
    filename: NonBlankString
    capture_size_bytes: int = Field(gt=0)
    transfer_file_size_bytes: int = Field(gt=0)
    sha256: Sha256

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        suffix = PurePath(value).suffix.lower()
        if suffix not in {".pcap", ".pcapng"}:
            raise ValueError("filename must end with .pcap or .pcapng")
        return value

    @property
    def capture_format(self) -> Literal["pcap", "pcapng"]:
        return "pcapng" if PurePath(self.filename).suffix.lower() == ".pcapng" else "pcap"

    @property
    def content_type(self) -> str:
        return (
            "application/x-pcapng"
            if self.capture_format == "pcapng"
            else "application/vnd.tcpdump.pcap"
        )


class CaptureCreateResponse(BaseModel):
    capture_id: UUID
    analysis_id: UUID
    ready_event_id: UUID
    status: CaptureStatus
    upload_required: bool
    upload_url: str | None
    expires_in: int | None


class CaptureRead(BaseModel):
    id: UUID
    application_id: UUID
    scenario: Literal["upload", "download"]
    original_filename: str
    capture_format: Literal["pcap", "pcapng"]
    content_type: str
    object_key: str
    expected_sha256: str
    verified_sha256: str | None
    capture_size_bytes: int
    transfer_file_size_bytes: int
    status: CaptureStatus
    ready_event_id: UUID
    analysis_id: UUID
    analysis_event_id: UUID
    analysis_attempt_count: int
    created_at: datetime
    uploaded_at: datetime | None
    analysis_started_at: datetime | None
    analysis_finished_at: datetime | None
    object_deleted_at: datetime | None
    error_code: str | None
    error_message: str | None

    @classmethod
    def from_capture(cls, capture: NetworkCapture) -> Self:
        return cls.model_validate(capture, from_attributes=True)


class DownloadUrlResponse(BaseModel):
    capture_id: UUID
    download_url: str
    expires_in: int
