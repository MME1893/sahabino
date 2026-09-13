from __future__ import annotations

from pathlib import Path

import pytest

from sahabino.network.exceptions import CaptureObjectMissingError, StorageUnavailableError
from sahabino.network.storage import S3ObjectStorage


class _ClientError(Exception):
    def __init__(self, *, status: int, code: str) -> None:
        self.response = {
            "ResponseMetadata": {"HTTPStatusCode": status},
            "Error": {"Code": code},
        }


class _FailingClient:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def download_file(self, bucket: str, object_key: str, destination: str) -> None:
        _ = (bucket, object_key, destination)
        raise self.error


def _storage(error: Exception) -> S3ObjectStorage:
    storage = object.__new__(S3ObjectStorage)
    storage._bucket = "captures"  # type: ignore[attr-defined]
    storage._client = _FailingClient(error)  # type: ignore[attr-defined]
    return storage


def test_download_classifies_confirmed_missing_object(tmp_path: Path) -> None:
    storage = _storage(_ClientError(status=404, code="NoSuchKey"))

    with pytest.raises(CaptureObjectMissingError):
        storage.download_file("missing", tmp_path / "capture.pcap")


@pytest.mark.parametrize(
    "error",
    [
        _ClientError(status=503, code="ServiceUnavailable"),
        TimeoutError("timed out"),
    ],
)
def test_download_keeps_transient_storage_failures_retryable(
    tmp_path: Path, error: Exception
) -> None:
    storage = _storage(error)

    with pytest.raises(StorageUnavailableError):
        storage.download_file("temporary", tmp_path / "capture.pcap")
