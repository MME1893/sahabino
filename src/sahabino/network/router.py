from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status

from sahabino.network.dependencies import get_capture_service
from sahabino.network.schemas import (
    CaptureCreate,
    CaptureCreateResponse,
    CaptureRead,
    CaptureStatus,
    DownloadUrlResponse,
)
from sahabino.network.service import CaptureService

router = APIRouter(prefix="/network-captures", tags=["network captures"])
ServiceDependency = Annotated[CaptureService, Depends(get_capture_service)]


@router.post("", response_model=CaptureCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_capture(
    payload: CaptureCreate,
    service: ServiceDependency,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> CaptureCreateResponse:
    return await service.create(payload, idempotency_key)


@router.get("", response_model=list[CaptureRead])
async def list_captures(
    service: ServiceDependency,
    application_id: Annotated[UUID | None, Query()] = None,
    scenario: Annotated[Literal["upload", "download"] | None, Query()] = None,
    capture_status: Annotated[CaptureStatus | None, Query(alias="status")] = None,
) -> list[CaptureRead]:
    captures = await service.list(
        application_id=application_id, scenario=scenario, status=capture_status
    )
    return [CaptureRead.from_capture(capture) for capture in captures]


@router.get("/{capture_id}", response_model=CaptureRead)
async def get_capture(capture_id: UUID, service: ServiceDependency) -> CaptureRead:
    return CaptureRead.from_capture(await service.get(capture_id))


@router.post("/{capture_id}/complete", response_model=CaptureRead)
async def complete_capture(capture_id: UUID, service: ServiceDependency) -> CaptureRead:
    return CaptureRead.from_capture(await service.complete(capture_id))


@router.post("/{capture_id}/download-url", response_model=DownloadUrlResponse)
async def create_download_url(capture_id: UUID, service: ServiceDependency) -> DownloadUrlResponse:
    return await service.download_url(capture_id)
