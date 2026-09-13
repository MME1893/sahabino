from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sahabino.common.config import Settings
from sahabino.network.exceptions import CaptureObjectMissingError, StorageUnavailableError


def _is_missing_object_error(error: Exception) -> bool:
    response = getattr(error, "response", {})
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = response.get("Error", {}).get("Code")
    return status == 404 or code in {"404", "NoSuchKey", "NotFound"}


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    size_bytes: int
    content_type: str | None = None


class ObjectStorage(Protocol):
    def generate_presigned_put(self, object_key: str, content_type: str) -> str: ...

    def generate_presigned_get(self, object_key: str) -> str: ...

    def head_object(self, object_key: str) -> ObjectMetadata | None: ...

    def download_file(self, object_key: str, destination: Path) -> None: ...

    def delete_object(self, object_key: str) -> None: ...

    def ensure_bucket(self) -> bool: ...


class S3ObjectStorage:
    """One S3-compatible adapter with separate internal and client-facing endpoints."""

    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self._bucket = settings.object_storage_bucket
        self._region = settings.object_storage_region
        self._expiry = settings.object_storage_presign_expiry_seconds
        credentials = {
            "aws_access_key_id": settings.object_storage_access_key.get_secret_value(),
            "aws_secret_access_key": settings.object_storage_secret_key.get_secret_value(),
            "region_name": self._region,
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }
        self._client: Any = boto3.client(
            "s3", endpoint_url=settings.object_storage_endpoint_url, **credentials
        )
        self._public_client: Any = boto3.client(
            "s3", endpoint_url=settings.object_storage_public_endpoint_url, **credentials
        )

    def generate_presigned_put(self, object_key: str, content_type: str) -> str:
        try:
            return str(
                self._public_client.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": self._bucket,
                        "Key": object_key,
                        "ContentType": content_type,
                    },
                    ExpiresIn=self._expiry,
                )
            )
        except Exception as error:
            raise StorageUnavailableError("Could not create an upload URL") from error

    def generate_presigned_get(self, object_key: str) -> str:
        try:
            return str(
                self._public_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": object_key},
                    ExpiresIn=self._expiry,
                )
            )
        except Exception as error:
            raise StorageUnavailableError("Could not create a download URL") from error

    def head_object(self, object_key: str) -> ObjectMetadata | None:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except Exception as error:
            if _is_missing_object_error(error):
                return None
            raise StorageUnavailableError("Could not inspect the capture object") from error
        return ObjectMetadata(
            size_bytes=int(response["ContentLength"]),
            content_type=response.get("ContentType"),
        )

    def download_file(self, object_key: str, destination: Path) -> None:
        try:
            self._client.download_file(self._bucket, object_key, str(destination))
        except Exception as error:
            if _is_missing_object_error(error):
                raise CaptureObjectMissingError(
                    "The completed capture object no longer exists"
                ) from error
            raise StorageUnavailableError("Could not download the capture object") from error

    def delete_object(self, object_key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=object_key)
        except Exception as error:
            raise StorageUnavailableError("Could not delete the capture object") from error

    def ensure_bucket(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return False
        except Exception as error:
            response = getattr(error, "response", {})
            status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = response.get("Error", {}).get("Code")
            if status not in {404} and code not in {"404", "NoSuchBucket", "NotFound"}:
                raise StorageUnavailableError("Could not inspect the capture bucket") from error

        parameters: dict[str, object] = {"Bucket": self._bucket}
        if self._region != "us-east-1":
            parameters["CreateBucketConfiguration"] = {"LocationConstraint": self._region}
        try:
            self._client.create_bucket(**parameters)
        except Exception as error:
            raise StorageUnavailableError("Could not create the capture bucket") from error
        return True
