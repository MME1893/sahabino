from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sahabino.app_registry.models import Application
from sahabino.common.config import Settings
from sahabino.network.exceptions import CaptureMetadataConflictError, IdempotencyConflictError
from sahabino.network.models import NetworkCapture, NetworkCaptureIdempotencyKey
from sahabino.network.repository import CaptureRepository
from sahabino.network.schemas import CaptureCreate, CaptureCreateResponse
from sahabino.network.service import CaptureService
from sahabino.network.storage import ObjectMetadata


class _Storage:
    def generate_presigned_put(self, object_key: str, content_type: str) -> str:
        _ = content_type
        return f"http://public.invalid/{object_key}"

    def generate_presigned_get(self, object_key: str) -> str:
        raise NotImplementedError

    def head_object(self, object_key: str) -> ObjectMetadata | None:
        return None

    def download_file(self, object_key: str, destination: Path) -> None:
        raise NotImplementedError

    def delete_object(self, object_key: str) -> None:
        raise NotImplementedError

    def ensure_bucket(self) -> bool:
        return True


class _Publisher:
    def publish_ready(self, capture: NetworkCapture, package_name: str) -> None:
        raise AssertionError("pending uploads must not publish")

    def publish_analysis(self, capture: NetworkCapture, payload: object) -> None:
        raise NotImplementedError


def _request(
    application_id: UUID,
    sha256: str,
    *,
    filename: str = "race.pcapng",
) -> CaptureCreate:
    return CaptureCreate.model_validate(
        {
            "application_id": application_id,
            "scenario": "upload",
            "filename": filename,
            "capture_size_bytes": 100,
            "transfer_file_size_bytes": 50,
            "sha256": sha256,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("same_request", [True, False], ids=["same-request", "conflict"])
async def test_concurrent_same_key_has_one_durable_binding(
    network_database_url: str,
    same_request: bool,
) -> None:
    engine = create_async_engine(network_database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    application_id = uuid4()
    key = uuid4()
    settings = Settings(database_url=network_database_url)

    try:
        async with sessions() as session:
            session.add(
                Application(
                    id=application_id,
                    name="Idempotency Race",
                    package_name=f"com.example.idempotency.{uuid4().hex}",
                )
            )
            await session.commit()

        async def create(request: CaptureCreate) -> CaptureCreateResponse:
            async with sessions() as session:
                service = CaptureService(
                    session,
                    _Storage(),  # type: ignore[arg-type]
                    _Publisher(),  # type: ignore[arg-type]
                    settings,
                )
                return await service.create(request, key)

        results = await asyncio.gather(
            create(_request(application_id, "a" * 64)),
            create(_request(application_id, ("a" if same_request else "b") * 64)),
            return_exceptions=True,
        )

        successful = [result for result in results if not isinstance(result, BaseException)]
        if same_request:
            assert len(successful) == 2
            assert len({result.capture_id for result in successful}) == 1
        else:
            assert len(successful) == 1
            assert sum(isinstance(result, IdempotencyConflictError) for result in results) == 1

        async with sessions() as session:
            capture_count = await session.scalar(select(func.count()).select_from(NetworkCapture))
            mappings = list(await session.scalars(select(NetworkCaptureIdempotencyKey)))
        assert capture_count == 1
        assert len(mappings) == 1
        assert mappings[0].idempotency_key == key
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_logical_race_winner_metadata_is_canonical(
    network_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(network_database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    application_id = uuid4()
    settings = Settings(database_url=network_database_url)
    original_get_logical = CaptureRepository.get_logical
    initial_queries = 0
    query_lock = asyncio.Lock()
    both_queried = asyncio.Event()

    async def synchronized_get_logical(
        repository: CaptureRepository,
        *,
        application_id: UUID,
        scenario: str,
        expected_sha256: str,
    ) -> NetworkCapture | None:
        nonlocal initial_queries
        result = await original_get_logical(
            repository,
            application_id=application_id,
            scenario=scenario,
            expected_sha256=expected_sha256,
        )
        if result is not None:
            return result
        async with query_lock:
            initial_queries += 1
            if initial_queries == 2:
                both_queried.set()
        await asyncio.wait_for(both_queried.wait(), timeout=5)
        return None

    monkeypatch.setattr(CaptureRepository, "get_logical", synchronized_get_logical)

    try:
        async with sessions() as session:
            session.add(
                Application(
                    id=application_id,
                    name="Logical Metadata Race",
                    package_name=f"com.example.logical-race.{uuid4().hex}",
                )
            )
            await session.commit()

        async def create(request: CaptureCreate, key: UUID) -> CaptureCreateResponse:
            async with sessions() as session:
                service = CaptureService(
                    session,
                    _Storage(),  # type: ignore[arg-type]
                    _Publisher(),  # type: ignore[arg-type]
                    settings,
                )
                return await service.create(request, key)

        sha256 = "c" * 64
        results = await asyncio.gather(
            create(_request(application_id, sha256, filename="race.pcapng"), uuid4()),
            create(_request(application_id, sha256, filename="race.pcap"), uuid4()),
            return_exceptions=True,
        )

        assert sum(not isinstance(result, BaseException) for result in results) == 1
        assert sum(isinstance(result, CaptureMetadataConflictError) for result in results) == 1

        async with sessions() as session:
            capture_count = await session.scalar(select(func.count()).select_from(NetworkCapture))
            mapping_count = await session.scalar(
                select(func.count()).select_from(NetworkCaptureIdempotencyKey)
            )
        assert capture_count == 1
        assert mapping_count == 1
    finally:
        await engine.dispose()
