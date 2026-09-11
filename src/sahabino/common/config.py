from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from Sahabino environment variables."""

    database_url: str
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    environment: str = "development"
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_partitions: int = Field(default=3, ge=1)
    kafka_topic_replication_factor: int = Field(default=1, ge=1)
    kafka_consumer_auto_offset_reset: Literal["earliest", "latest"] = "earliest"
    ingestion_consumer_group_id: str = "sahabino-ingestion-v1"
    kafka_producer_queue_full_max_retries: int = Field(default=3, ge=0)
    kafka_producer_queue_full_poll_timeout_seconds: float = Field(default=0.1, gt=0)

    playstore_crawl_interval_minutes: int = Field(default=60, ge=1)
    playstore_max_concurrent_apps: int = Field(default=3, ge=1)
    playstore_language_code: str = Field(default="en", pattern=r"^[a-z]{2,3}$")
    playstore_country_code: str = Field(default="us", pattern=r"^[a-z]{2}$")
    playstore_request_timeout_seconds: float = Field(default=20.0, gt=0)
    playstore_retry_max_attempts: int = Field(default=3, ge=1)
    playstore_retry_max_delay_seconds: float = Field(default=30.0, gt=0)
    playstore_rate_limit_enabled: bool = True
    playstore_rate_limit_refill_per_second: float = Field(default=1.0, gt=0)
    playstore_rate_limit_burst_capacity: int = Field(default=2, ge=1)
    playstore_proxy_enabled: bool = False
    playstore_proxy_urls: list[SecretStr] = Field(default_factory=list)
    playstore_proxy_direct_fallback: bool = True
    playstore_proxy_failure_threshold: int = Field(default=2, ge=1)
    playstore_proxy_cooldown_seconds: float = Field(default=60.0, ge=0)
    playstore_proxy_rate_limit_rotate_after: int = Field(default=2, ge=1)
    playstore_circuit_breaker_enabled: bool = True
    playstore_circuit_breaker_failure_threshold: int = Field(default=5, ge=1)
    playstore_circuit_breaker_cooldown_seconds: float = Field(default=60.0, ge=0)
    playstore_secondary_adapter_enabled: bool = True
    application_registry_base_url: str = "http://localhost:8000"

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        environment = value.strip()
        if not environment:
            raise ValueError("environment must not be blank")
        return environment

    @field_validator("ingestion_consumer_group_id")
    @classmethod
    def validate_ingestion_consumer_group_id(cls, value: str) -> str:
        consumer_group = value.strip()
        if not consumer_group:
            raise ValueError("ingestion consumer group ID must not be blank")
        return consumer_group

    @model_validator(mode="after")
    def validate_proxy_configuration(self) -> "Settings":
        if (
            self.playstore_proxy_enabled
            and not self.playstore_proxy_urls
            and not self.playstore_proxy_direct_fallback
        ):
            raise ValueError(
                "proxy mode requires at least one URL or direct fallback must be enabled"
            )
        if any(not value.get_secret_value().strip() for value in self.playstore_proxy_urls):
            raise ValueError("proxy URLs must not be blank")
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SAHABINO_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # Values are supplied by BaseSettings sources.
