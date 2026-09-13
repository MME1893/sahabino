from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from sahabino.network.service import object_key_for
from sahabino.network.storage import S3ObjectStorage
from scripts.generate_network_fixture import build_fixture
from tests.integration.crawler.conftest import psycopg_dsn


def _metadata(application_id: str, capture: bytes) -> dict[str, Any]:
    return {
        "application_id": application_id,
        "scenario": "upload",
        "filename": "integration.pcapng",
        "capture_size_bytes": len(capture),
        "transfer_file_size_bytes": 20,
        "sha256": hashlib.sha256(capture).hexdigest(),
    }


def test_capture_http_idempotency_presigned_upload_and_completion(
    network_api_client: TestClient,
    create_network_application: Any,
    network_database_url: str,
) -> None:
    application = create_network_application()
    capture = build_fixture()
    metadata = _metadata(application["id"], capture)
    first_key = str(uuid4())
    second_key = str(uuid4())

    first = network_api_client.post(
        "/network-captures", json=metadata, headers={"Idempotency-Key": first_key}
    )
    logical_duplicate = network_api_client.post(
        "/network-captures", json=metadata, headers={"Idempotency-Key": second_key}
    )
    repeated_second_key = network_api_client.post(
        "/network-captures", json=metadata, headers={"Idempotency-Key": second_key}
    )

    assert first.status_code == 201
    assert logical_duplicate.status_code == 201
    assert repeated_second_key.status_code == 201
    created = first.json()
    assert logical_duplicate.json()["capture_id"] == created["capture_id"]
    assert repeated_second_key.json()["capture_id"] == created["capture_id"]
    assert created["upload_required"] is True
    assert created["upload_url"]

    with psycopg.connect(psycopg_dsn(network_database_url)) as connection:
        capture_count = connection.execute("SELECT count(*) FROM network_captures").fetchone()
        mapping_count = connection.execute(
            "SELECT count(*) FROM network_capture_idempotency_keys"
        ).fetchone()
    assert capture_count == (1,)
    assert mapping_count == (2,)

    metadata_conflict = network_api_client.post(
        "/network-captures",
        json={**metadata, "capture_size_bytes": metadata["capture_size_bytes"] + 1},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert metadata_conflict.status_code == 409
    assert metadata_conflict.json()["detail"]["code"] == "capture_metadata_conflict"
    with psycopg.connect(psycopg_dsn(network_database_url)) as connection:
        mapping_count = connection.execute(
            "SELECT count(*) FROM network_capture_idempotency_keys"
        ).fetchone()
    assert mapping_count == (2,)

    conflict_metadata = {**metadata, "transfer_file_size_bytes": 21}
    for key in (second_key, first_key):
        conflict = network_api_client.post(
            "/network-captures",
            json=conflict_metadata,
            headers={"Idempotency-Key": key},
        )
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    upload = httpx.put(
        created["upload_url"],
        content=capture,
        headers={"Content-Type": "application/x-pcapng"},
        timeout=15,
    )
    assert upload.is_success, upload.text
    complete = network_api_client.post(f"/network-captures/{created['capture_id']}/complete")
    completed_again = network_api_client.post(f"/network-captures/{created['capture_id']}/complete")
    assert complete.status_code == completed_again.status_code == 200
    assert complete.json()["status"] == "uploaded"
    assert complete.json()["ready_event_id"] == completed_again.json()["ready_event_id"]

    listed = network_api_client.get(
        "/network-captures", params={"application_id": application["id"], "status": "uploaded"}
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [created["capture_id"]]


def test_capture_api_validates_request_and_missing_object(
    network_api_client: TestClient,
    create_network_application: Any,
) -> None:
    application = create_network_application()
    capture = build_fixture()
    metadata = _metadata(application["id"], capture)

    invalid = network_api_client.post(
        "/network-captures",
        json={**metadata, "filename": "capture.txt"},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert invalid.status_code == 422

    created = network_api_client.post(
        "/network-captures",
        json=metadata,
        headers={"Idempotency-Key": str(uuid4())},
    ).json()
    missing = network_api_client.post(f"/network-captures/{created['capture_id']}/complete")
    assert missing.status_code == 409
    assert missing.json()["detail"]["code"] == "capture_object_missing"


@pytest.mark.parametrize("physical_state", ["missing", "wrong_size"])
def test_verified_reuse_checks_real_physical_object(
    physical_state: str,
    network_api_client: TestClient,
    create_network_application: Any,
    network_storage: S3ObjectStorage,
    network_database_url: str,
) -> None:
    first_application = create_network_application()
    second_application = create_network_application()
    capture = build_fixture()
    metadata = _metadata(first_application["id"], capture)
    created = network_api_client.post(
        "/network-captures",
        json=metadata,
        headers={"Idempotency-Key": str(uuid4())},
    ).json()
    uploaded = httpx.put(
        created["upload_url"],
        content=capture,
        headers={"Content-Type": "application/x-pcapng"},
        timeout=15,
    )
    assert uploaded.is_success
    completed = network_api_client.post(f"/network-captures/{created['capture_id']}/complete")
    assert completed.status_code == 200
    with psycopg.connect(psycopg_dsn(network_database_url)) as connection:
        connection.execute(
            "UPDATE network_captures SET status='analyzed', verified_sha256=expected_sha256 "
            "WHERE id=%s",
            (created["capture_id"],),
        )

    object_key = object_key_for(metadata["sha256"], "pcapng")
    if physical_state == "missing":
        network_storage.delete_object(object_key)
    else:
        network_storage._client.put_object(
            Bucket=network_storage._bucket,
            Key=object_key,
            Body=b"wrong-size",
            ContentType="application/x-pcapng",
        )

    reuse_metadata = {**metadata, "application_id": second_application["id"]}
    response = network_api_client.post(
        "/network-captures",
        json=reuse_metadata,
        headers={"Idempotency-Key": str(uuid4())},
    )

    if physical_state == "missing":
        assert response.status_code == 201
        assert response.json()["upload_required"] is True
    else:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "capture_object_size_mismatch"
