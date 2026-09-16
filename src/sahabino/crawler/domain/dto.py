from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ApplicationRef:
    application_id: UUID
    name: str
    package_name: str
    language_code: str | None = None
    country_code: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("application name must not be blank")
        if not self.package_name.strip():
            raise ValueError("package name must not be blank")
        if (self.language_code is None) != (self.country_code is None):
            raise ValueError("application locale must contain both language and country")
        if self.language_code is not None and not self.language_code.strip():
            raise ValueError("application language code must not be blank")
        if self.country_code is not None and not self.country_code.strip():
            raise ValueError("application country code must not be blank")

    def effective_locale(self, language_code: str, country_code: str) -> tuple[str, str]:
        return (
            self.language_code or language_code,
            self.country_code or country_code,
        )


@dataclass(frozen=True, slots=True)
class AdapterCapabilities:
    supports_proxy: bool
    transport_controlled: bool
    supports_reviews: bool


class AppDetailsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    min_installs: int = Field(ge=0)
    score: float = Field(ge=0, le=5)
    ratings_count: int = Field(ge=0)
    reviews_count: int = Field(ge=0)
    store_updated_on: date | None
    version: str | None
    ad_supported: bool
    collected_at: datetime = Field(default_factory=utc_now)
    source_adapter: str = Field(min_length=1)

    @field_validator("collected_at")
    @classmethod
    def collected_at_is_aware(cls, value: datetime) -> datetime:
        return _aware_utc(value, "collected_at")


class ReviewDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    external_review_id: str = Field(min_length=1)
    source_at: datetime
    author_name: str
    thumbs_up_count: int = Field(ge=0)
    score: int = Field(ge=1, le=5)
    content: str
    position: int = Field(ge=1, le=1000)
    observed_at: datetime = Field(default_factory=utc_now)
    source_adapter: str = Field(min_length=1)

    @field_validator("source_at", "observed_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "timestamp")
        return _aware_utc(value, str(field_name))


class ReviewsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reviews: tuple[ReviewDTO, ...] = Field(max_length=1000)
