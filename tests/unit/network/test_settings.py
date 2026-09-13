from __future__ import annotations

import pytest
from pydantic import ValidationError

from sahabino.common.config import Settings


def test_network_settings_have_safe_local_defaults_and_secret_boundaries() -> None:
    settings = Settings(database_url="postgresql+psycopg://localhost/sahabino")

    assert settings.network_analyzer_consumer_group_id == "sahabino-network-analyzer-v1"
    assert settings.object_storage_endpoint_url == "http://localhost:8333"
    assert settings.object_storage_public_endpoint_url == "http://localhost:8333"
    assert settings.object_storage_presign_expiry_seconds == 900
    assert settings.object_storage_max_capture_size_bytes == 256 * 1024**2
    assert settings.network_max_parsed_records == 250_000
    assert settings.network_analyzer_max_poll_interval_ms == 900_000
    assert "development-secret" not in repr(settings.object_storage_secret_key)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("network_analyzer_consumer_group_id", " "),
        ("object_storage_bucket", ""),
        ("object_storage_presign_expiry_seconds", 0),
        ("object_storage_max_capture_size_bytes", 0),
        ("network_tshark_timeout_seconds", 0),
        ("network_max_parsed_records", 0),
        ("network_analyzer_max_poll_interval_ms", 0),
    ],
)
def test_invalid_network_settings_are_rejected(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(
            database_url="postgresql+psycopg://localhost/sahabino",
            **{field: value},
        )
