from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from sahabino.common.config import Settings
from sahabino.network.exceptions import (
    CaptureDispatchError,
    CaptureMetadataConflictError,
    CaptureObjectSizeMismatchError,
    IdempotencyConflictError,
)
from sahabino.network.models import NetworkCapture, NetworkCaptureIdempotencyKey
from sahabino.network.schemas import CaptureCreate
from sahabino.network.service import CaptureService
from sahabino.network.storage import ObjectMetadata


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeRepository:
    def __init__(self, applications: dict[UUID, str]) -> None:
        self.applications = applications
        self.captures: list[NetworkCapture] = []
        self.locked_keys: list[str] = []
        self.locked_idempotency_keys: list[UUID] = []
        self.idempotency_mappings: dict[UUID, NetworkCaptureIdempotencyKey] = {}

    async def application_package_name(self, application_id: UUID) -> str | None:
        return self.applications.get(application_id)

    async def lock_object_key(self, object_key: str) -> None:
        self.locked_keys.append(object_key)

    async def lock_idempotency_key(self, key: UUID) -> None:
        self.locked_idempotency_keys.append(key)

    async def get_idempotency_key(self, key: UUID) -> NetworkCaptureIdempotencyKey | None:
        return self.idempotency_mappings.get(key)

    async def get_logical(
        self, *, application_id: UUID, scenario: str, expected_sha256: str
    ) -> NetworkCapture | None:
        return next(
            (
                item
                for item in self.captures
                if item.application_id == application_id
                and item.scenario == scenario
                and item.expected_sha256 == expected_sha256
            ),
            None,
        )

    async def get_verified_object(self, object_key: str) -> NetworkCapture | None:
        return next(
            (
                item
                for item in self.captures
                if item.object_key == object_key
                and item.verified_sha256 is not None
                and item.object_deleted_at is None
            ),
            None,
        )

    async def get(self, capture_id: UUID) -> NetworkCapture | None:
        return next((item for item in self.captures if item.id == capture_id), None)

    async def package_name_for_capture(self, capture: NetworkCapture) -> str:
        return self.applications[capture.application_id]

    def add(self, capture: NetworkCapture) -> None:
        self.captures.append(capture)

    def add_idempotency_key(self, mapping: NetworkCaptureIdempotencyKey) -> None:
        self.idempotency_mappings[mapping.idempotency_key] = mapping

    async def refresh(self, capture: NetworkCapture) -> None:
        _ = capture

    async def flush(self) -> None:
        return None


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, ObjectMetadata] = {}
        self.put_requests: list[tuple[str, str]] = []

    def generate_presigned_put(self, object_key: str, content_type: str) -> str:
        self.put_requests.append((object_key, content_type))
        return f"http://public.invalid/{object_key}?signature=secret"

    def generate_presigned_get(self, object_key: str) -> str:
        return f"http://public.invalid/{object_key}?signature=secret"

    def head_object(self, object_key: str) -> ObjectMetadata | None:
        return self.objects.get(object_key)

    def download_file(self, object_key: str, destination: Path) -> None:
        raise NotImplementedError

    def delete_object(self, object_key: str) -> None:
        self.objects.pop(object_key, None)

    def ensure_bucket(self) -> bool:
        return True


class FakePublisher:
    def __init__(self) -> None:
        self.ready_event_ids: list[UUID] = []
        self.failure: Exception | None = None

    def publish_ready(self, capture: NetworkCapture, package_name: str) -> None:
        _ = package_name
        if self.failure is not None:
            raise self.failure
        self.ready_event_ids.append(capture.ready_event_id)

    def publish_analysis(self, capture: NetworkCapture, payload: object) -> None:
        raise NotImplementedError


def _payload(application_id: UUID, **changes: object) -> CaptureCreate:
    values: dict[str, object] = {
        "application_id": application_id,
        "scenario": "upload",
        "filename": "synthetic.pcapng",
        "capture_size_bytes": 100,
        "transfer_file_size_bytes": 50,
        "sha256": "a" * 64,
    }
    values.update(changes)
    return CaptureCreate.model_validate(values)


def _service(
    applications: dict[UUID, str],
) -> tuple[CaptureService, FakeRepository, FakeStorage, FakePublisher, FakeSession]:
    session = FakeSession()
    storage = FakeStorage()
    publisher = FakePublisher()
    settings = Settings(database_url="postgresql+psycopg://localhost/sahabino")
    service = CaptureService(session, storage, publisher, settings)  # type: ignore[arg-type]
    repository = FakeRepository(applications)
    service._repository = repository  # type: ignore[assignment]
    return service, repository, storage, publisher, session


@pytest.mark.asyncio
async def test_every_accepted_idempotency_key_is_persisted_and_conflicts_are_rejected() -> None:
    application_id = uuid4()
    service, repository, storage, _, _ = _service({application_id: "com.example.network"})
    first_key = uuid4()
    second_key = uuid4()
    request = _payload(application_id)

    first = await service.create(request, first_key)
    second = await service.create(request, second_key)
    repeated_second = await service.create(request, second_key)

    assert first.capture_id == second.capture_id == repeated_second.capture_id
    assert len(repository.captures) == 1
    assert len(repository.idempotency_mappings) == 2
    assert {mapping.capture_id for mapping in repository.idempotency_mappings.values()} == {
        first.capture_id
    }
    assert first.upload_required is True
    assert first.upload_url is not None
    assert storage.put_requests[0][0] == f"captures/sha256/aa/{'a' * 64}.pcapng"
    assert repository.locked_keys == [storage.put_requests[0][0]] * 3
    assert repository.locked_idempotency_keys == [first_key, second_key, second_key]

    with pytest.raises(IdempotencyConflictError):
        await service.create(_payload(application_id, transfer_file_size_bytes=51), second_key)
    with pytest.raises(IdempotencyConflictError):
        await service.create(_payload(application_id, transfer_file_size_bytes=51), first_key)

    assert len(repository.captures) == 1
    assert len(repository.idempotency_mappings) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "synthetic.pcap"},
        {"capture_size_bytes": 101},
        {"transfer_file_size_bytes": 51},
    ],
    ids=["capture-format", "capture-size", "transfer-file-size"],
)
async def test_new_key_rejects_logical_capture_metadata_conflict(
    changes: dict[str, object],
) -> None:
    application_id = uuid4()
    service, repository, _, _, _ = _service({application_id: "com.example.network"})
    await service.create(_payload(application_id), uuid4())
    canonical = repository.captures[0]
    canonical.status = "expired"

    with pytest.raises(CaptureMetadataConflictError) as raised:
        await service.create(_payload(application_id, **changes), uuid4())

    assert raised.value.code == "capture_metadata_conflict"
    assert len(repository.captures) == 1
    assert len(repository.idempotency_mappings) == 1
    assert canonical.status == "expired"


@pytest.mark.asyncio
async def test_new_key_allows_filename_alias_for_same_logical_capture() -> None:
    application_id = uuid4()
    service, repository, _, _, _ = _service({application_id: "com.example.network"})
    first = await service.create(_payload(application_id), uuid4())
    alias_key = uuid4()
    alias_request = _payload(application_id, filename="renamed-capture.pcapng")

    alias = await service.create(alias_request, alias_key)
    repeated_alias = await service.create(alias_request, alias_key)

    assert alias.capture_id == repeated_alias.capture_id == first.capture_id
    assert len(repository.captures) == 1
    assert len(repository.idempotency_mappings) == 2


@pytest.mark.asyncio
async def test_verified_content_is_reused_across_logical_captures_without_upload() -> None:
    first_app = uuid4()
    second_app = uuid4()
    service, repository, storage, publisher, _ = _service(
        {first_app: "com.example.one", second_app: "com.example.two"}
    )
    first = await service.create(_payload(first_app), uuid4())
    stored = repository.captures[0]
    stored.status = "analyzed"
    stored.verified_sha256 = stored.expected_sha256
    storage.objects[stored.object_key] = ObjectMetadata(size_bytes=100)

    second = await service.create(_payload(second_app), uuid4())

    assert first.capture_id != second.capture_id
    assert len(repository.captures) == 2
    assert repository.captures[0].object_key == repository.captures[1].object_key
    assert second.status == "uploaded"
    assert second.upload_required is False
    assert publisher.ready_event_ids == [repository.captures[1].ready_event_id]


@pytest.mark.asyncio
async def test_completion_persists_uploaded_before_publish_failure_and_reuses_event_id() -> None:
    application_id = uuid4()
    service, repository, storage, publisher, session = _service(
        {application_id: "com.example.network"}
    )
    created = await service.create(_payload(application_id), uuid4())
    capture = repository.captures[0]
    storage.objects[capture.object_key] = ObjectMetadata(size_bytes=100)
    publisher.failure = CaptureDispatchError("Kafka unavailable")

    with pytest.raises(CaptureDispatchError):
        await service.complete(created.capture_id)

    assert capture.status == "uploaded"
    stable_event_id = capture.ready_event_id
    assert session.commits == 2
    publisher.failure = None

    completed = await service.complete(created.capture_id)

    assert completed.ready_event_id == stable_event_id
    assert publisher.ready_event_ids == [stable_event_id]


@pytest.mark.asyncio
async def test_repeated_create_recovers_uploaded_capture_publish_failure() -> None:
    application_id = uuid4()
    service, repository, storage, publisher, _ = _service({application_id: "com.example.network"})
    key = uuid4()
    created = await service.create(_payload(application_id), key)
    capture = repository.captures[0]
    storage.objects[capture.object_key] = ObjectMetadata(size_bytes=100)
    publisher.failure = CaptureDispatchError("Kafka unavailable")

    with pytest.raises(CaptureDispatchError):
        await service.complete(created.capture_id)

    publisher.failure = None
    repeated = await service.create(_payload(application_id), key)

    assert repeated.capture_id == created.capture_id
    assert repeated.ready_event_id == capture.ready_event_id
    assert publisher.ready_event_ids == [capture.ready_event_id]


@pytest.mark.asyncio
async def test_verified_object_reuse_rejects_conflicting_declared_size() -> None:
    first_app = uuid4()
    second_app = uuid4()
    service, repository, _, _, _ = _service(
        {first_app: "com.example.one", second_app: "com.example.two"}
    )
    await service.create(_payload(first_app), uuid4())
    stored = repository.captures[0]
    stored.status = "analyzed"
    stored.verified_sha256 = stored.expected_sha256

    with pytest.raises(CaptureObjectSizeMismatchError):
        await service.create(_payload(second_app, capture_size_bytes=101), uuid4())


@pytest.mark.asyncio
async def test_verified_metadata_with_missing_object_requires_fresh_upload() -> None:
    first_app = uuid4()
    second_app = uuid4()
    service, repository, storage, publisher, _ = _service(
        {first_app: "com.example.one", second_app: "com.example.two"}
    )
    await service.create(_payload(first_app), uuid4())
    verified = repository.captures[0]
    verified.status = "analyzed"
    verified.verified_sha256 = verified.expected_sha256

    created = await service.create(_payload(second_app), uuid4())

    assert created.upload_required is True
    assert created.status == "pending_upload"
    assert storage.put_requests[-1][0] == verified.object_key
    assert publisher.ready_event_ids == []


@pytest.mark.asyncio
async def test_verified_metadata_with_wrong_physical_size_is_rejected() -> None:
    first_app = uuid4()
    second_app = uuid4()
    service, repository, storage, _, _ = _service(
        {first_app: "com.example.one", second_app: "com.example.two"}
    )
    await service.create(_payload(first_app), uuid4())
    verified = repository.captures[0]
    verified.status = "analyzed"
    verified.verified_sha256 = verified.expected_sha256
    storage.objects[verified.object_key] = ObjectMetadata(size_bytes=99)

    with pytest.raises(CaptureObjectSizeMismatchError, match="inconsistent with storage"):
        await service.create(_payload(second_app), uuid4())

    assert len(repository.captures) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("use_original_key", [True, False])
async def test_expired_logical_capture_reopens_without_changing_identities(
    use_original_key: bool,
) -> None:
    application_id = uuid4()
    service, repository, _, _, _ = _service({application_id: "com.example.network"})
    original_key = uuid4()
    created = await service.create(_payload(application_id), original_key)
    capture = repository.captures[0]
    identities = (
        capture.id,
        capture.analysis_id,
        capture.ready_event_id,
        capture.analysis_event_id,
    )
    capture.status = "expired"
    capture.error_code = "upload_expired"
    capture.error_message = "expired"

    reopened = await service.create(
        _payload(application_id), original_key if use_original_key else uuid4()
    )

    assert len(repository.captures) == 1
    assert reopened.capture_id == created.capture_id
    assert reopened.upload_required is True
    assert reopened.status == "pending_upload"
    assert (
        capture.id,
        capture.analysis_id,
        capture.ready_event_id,
        capture.analysis_event_id,
    ) == identities
    assert capture.error_code is None
    assert capture.error_message is None
    assert len(repository.idempotency_mappings) == (1 if use_original_key else 2)


@pytest.mark.asyncio
async def test_reopened_upload_clears_stale_object_deletion_marker_on_complete() -> None:
    application_id = uuid4()
    service, repository, storage, _, _ = _service({application_id: "com.example.network"})
    key = uuid4()
    await service.create(_payload(application_id), key)
    capture = repository.captures[0]
    capture.status = "expired"
    capture.object_deleted_at = datetime.now(UTC)

    reopened = await service.create(_payload(application_id), key)
    assert reopened.upload_required is True
    assert capture.object_deleted_at is not None
    storage.objects[capture.object_key] = ObjectMetadata(size_bytes=100)

    completed = await service.complete(capture.id)
    download = await service.download_url(capture.id)

    assert completed.status == "uploaded"
    assert completed.object_deleted_at is None
    assert download.download_url.startswith("http://public.invalid/")
