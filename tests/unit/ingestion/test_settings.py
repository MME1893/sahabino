from __future__ import annotations

import pytest
from pydantic import ValidationError

from sahabino.common.config import Settings


def test_default_ingestion_consumer_group() -> None:
    settings = Settings(database_url="postgresql+psycopg://localhost/sahabino")

    assert settings.ingestion_consumer_group_id == "sahabino-ingestion-v1"


def test_configurable_ingestion_consumer_group() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://localhost/sahabino",
        ingestion_consumer_group_id=" custom-ingestion ",
    )

    assert settings.ingestion_consumer_group_id == "custom-ingestion"


def test_blank_ingestion_consumer_group_is_rejected() -> None:
    with pytest.raises(ValidationError, match="consumer group ID must not be blank"):
        Settings(
            database_url="postgresql+psycopg://localhost/sahabino",
            ingestion_consumer_group_id="   ",
        )
