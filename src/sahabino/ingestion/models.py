from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from sahabino.app_registry import models as registry_models
from sahabino.crawler.infrastructure.persistence import models as crawler_models
from sahabino.db.base import Base

_ = (registry_models, crawler_models)


class IngestedEvent(Base):
    __tablename__ = "ingested_events"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="schema_version_positive"),
        CheckConstraint("partition >= 0", name="partition_non_negative"),
        CheckConstraint('"offset" >= 0', name="offset_non_negative"),
        UniqueConstraint(
            "topic",
            "partition",
            "offset",
            name="uq_ingested_events_topic_partition_offset",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(SmallInteger)
    topic: Mapped[str] = mapped_column(Text)
    partition: Mapped[int] = mapped_column(Integer)
    offset: Mapped[int] = mapped_column(BigInteger)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PlaystoreAppSnapshot(Base):
    __tablename__ = "playstore_app_snapshots"
    __table_args__ = (
        UniqueConstraint("crawl_task_id", name="uq_playstore_app_snapshots_crawl_task_id"),
        CheckConstraint("min_installs >= 0", name="min_installs_non_negative"),
        CheckConstraint("score >= 0 AND score <= 5", name="score_range"),
        CheckConstraint("ratings_count >= 0", name="ratings_count_non_negative"),
        CheckConstraint("reviews_count >= 0", name="reviews_count_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    crawl_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("crawl_tasks.id", ondelete="CASCADE")
    )
    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT")
    )
    package_name: Mapped[str] = mapped_column(Text)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    min_installs: Mapped[int] = mapped_column(BigInteger)
    score: Mapped[float] = mapped_column(Float)
    ratings_count: Mapped[int] = mapped_column(BigInteger)
    reviews_count: Mapped[int] = mapped_column(BigInteger)
    store_updated_on: Mapped[date | None] = mapped_column(Date)
    version: Mapped[str | None] = mapped_column(Text)
    ad_supported: Mapped[bool] = mapped_column(Boolean)
    source_adapter: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "external_review_id",
            name="uq_reviews_application_id_external_review_id",
        ),
        CheckConstraint("thumbs_up_count >= 0", name="thumbs_up_count_non_negative"),
        CheckConstraint("score >= 1 AND score <= 5", name="score_range"),
        CheckConstraint("first_observed_at <= last_observed_at", name="observation_order"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("applications.id", ondelete="RESTRICT")
    )
    external_review_id: Mapped[str] = mapped_column(Text)
    source_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    author_name: Mapped[str] = mapped_column(Text)
    thumbs_up_count: Mapped[int] = mapped_column(BigInteger)
    score: Mapped[int] = mapped_column(SmallInteger)
    content: Mapped[str] = mapped_column(Text)
    source_adapter: Mapped[str] = mapped_column(Text)
    first_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewObservation(Base):
    __tablename__ = "review_observations"
    __table_args__ = (
        CheckConstraint("position >= 1 AND position <= 1000", name="position_range"),
        CheckConstraint("score >= 1 AND score <= 5", name="score_range"),
        CheckConstraint("thumbs_up_count >= 0", name="thumbs_up_count_non_negative"),
    )

    crawl_task_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("crawl_tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    review_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("reviews.id", ondelete="CASCADE"), primary_key=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    position: Mapped[int] = mapped_column(SmallInteger)
    score: Mapped[int] = mapped_column(SmallInteger)
    thumbs_up_count: Mapped[int] = mapped_column(BigInteger)
    source_adapter: Mapped[str] = mapped_column(Text)
