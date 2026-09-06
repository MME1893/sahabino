from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from Sahabino environment variables."""

    database_url: str
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_partitions: int = Field(default=3, ge=1)
    kafka_topic_replication_factor: int = Field(default=1, ge=1)
    kafka_consumer_auto_offset_reset: Literal["earliest", "latest"] = "earliest"
    kafka_producer_queue_full_max_retries: int = Field(default=3, ge=0)
    kafka_producer_queue_full_poll_timeout_seconds: float = Field(default=0.1, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SAHABINO_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # Values are supplied by BaseSettings sources.
