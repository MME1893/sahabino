from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text

# just for manual test
from sahabino.app_registry import models as registry_models
from sahabino.db.base import Base

_ = registry_models


class CrawlRun(Base):
    __tablename__ = "crawl_runs"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('scheduled', 'manual')",
            name="trigger_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partially_failed', 'failed')",
            name="status",
        ),
        Index("ix_crawl_runs_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    trigger_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="running", server_default=text("'running'"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crawler_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CrawlTask(Base):
    __tablename__ = "crawl_tasks"
    __table_args__ = (
        UniqueConstraint(
            "crawl_run_id",
            "application_id",
            "task_type",
            name="uq_crawl_tasks_run_application_type",
        ),
        CheckConstraint("task_type IN ('app_details', 'reviews')", name="task_type"),
        CheckConstraint(
            "status IN ('pending', 'running', 'retrying', 'succeeded', 'failed')",
            name="status",
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        Index("ix_crawl_tasks_run_status", "crawl_run_id", "status"),
        Index("ix_crawl_tasks_application_id", "application_id"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True, default=uuid4)
    crawl_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("crawl_runs.id", ondelete="CASCADE"),
    )
    application_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="RESTRICT"),
    )
    task_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default=text("'pending'"))
    language_code: Mapped[str] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
