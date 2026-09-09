from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sahabino.messaging.events import EventEnvelope

APP_STATS_EVENT_TYPE = "playstore.app_stats.collected"
REVIEW_OBSERVED_EVENT_TYPE = "playstore.review.observed"
PLAYSTORE_SCHEMA_VERSION = 1


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("event timestamps must be timezone-aware")
    return value.astimezone(UTC)


class AppStatsCollectedV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    crawl_task_id: UUID
    application_id: UUID
    package_name: str = Field(min_length=1)
    collected_at: datetime
    min_installs: int = Field(ge=0)
    score: float = Field(ge=0, le=5)
    ratings_count: int = Field(ge=0)
    reviews_count: int = Field(ge=0)
    store_updated_on: date | None
    version: str | None
    ad_supported: bool
    source_adapter: str = Field(min_length=1)

    @field_validator("collected_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return _utc(value)


class ReviewObservedV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    crawl_task_id: UUID
    application_id: UUID
    package_name: str = Field(min_length=1)
    observed_at: datetime
    position: int = Field(ge=1, le=100)
    external_review_id: str = Field(min_length=1)
    source_at: datetime
    author_name: str
    thumbs_up_count: int = Field(ge=0)
    score: int = Field(ge=1, le=5)
    content: str
    source_adapter: str = Field(min_length=1)

    @field_validator("observed_at", "source_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        return _utc(value)


def app_stats_envelope(payload: AppStatsCollectedV1) -> EventEnvelope[AppStatsCollectedV1]:
    return EventEnvelope(
        event_type=APP_STATS_EVENT_TYPE,
        schema_version=PLAYSTORE_SCHEMA_VERSION,
        occurred_at=payload.collected_at,
        payload=payload,
    )


def review_observed_envelope(
    payload: ReviewObservedV1,
) -> EventEnvelope[ReviewObservedV1]:
    return EventEnvelope(
        event_type=REVIEW_OBSERVED_EVENT_TYPE,
        schema_version=PLAYSTORE_SCHEMA_VERSION,
        occurred_at=payload.observed_at,
        payload=payload,
    )
