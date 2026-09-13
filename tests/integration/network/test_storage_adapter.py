from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import httpx

from sahabino.common.config import Settings
from sahabino.network.storage import S3ObjectStorage


def test_real_s3_adapter_complete_object_lifecycle(
    network_s3_endpoint: str,
    tmp_path: Path,
) -> None:
    bucket = f"sahabino-storage-{uuid4().hex}"
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        object_storage_endpoint_url=network_s3_endpoint,
        object_storage_public_endpoint_url=network_s3_endpoint,
        object_storage_bucket=bucket,
        object_storage_access_key="integration",
        object_storage_secret_key="integration-secret",
    )
    storage = S3ObjectStorage(settings)
    object_key = "captures/sha256/ab/" + "ab" * 32 + ".pcap"
    content = b"pcap-bytes-for-storage-integration"

    bucket_created = False
    try:
        assert storage.ensure_bucket() is True
        bucket_created = True
        assert storage.ensure_bucket() is False

        put_url = storage.generate_presigned_put(object_key, "application/vnd.tcpdump.pcap")
        put = httpx.put(
            put_url,
            content=content,
            headers={"Content-Type": "application/vnd.tcpdump.pcap"},
            timeout=15,
        )
        assert put.is_success, put.text
        metadata = storage.head_object(object_key)
        assert metadata is not None
        assert metadata.size_bytes == len(content)

        destination = tmp_path / "downloaded.pcap"
        storage.download_file(object_key, destination)
        assert destination.read_bytes() == content

        get = httpx.get(storage.generate_presigned_get(object_key), timeout=15)
        assert get.is_success
        assert get.content == content

        repeated = httpx.put(
            put_url,
            content=content,
            headers={"Content-Type": "application/vnd.tcpdump.pcap"},
            timeout=15,
        )
        assert repeated.is_success
        objects = storage._client.list_objects_v2(Bucket=bucket).get("Contents", [])
        assert [item["Key"] for item in objects] == [object_key]

        storage.delete_object(object_key)
        assert storage.head_object(object_key) is None
    finally:
        if bucket_created:
            objects = storage._client.list_objects_v2(Bucket=bucket).get("Contents", [])
            for item in objects:
                storage.delete_object(str(item["Key"]))
            storage._client.delete_bucket(Bucket=bucket)
