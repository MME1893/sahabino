"""add crawler lifecycle tables

Revision ID: 20260906_0002
Revises: 20260903_0001
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260906_0002"
down_revision: str | None = "20260903_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crawl_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crawler_version", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "trigger_type IN ('scheduled', 'manual')",
            name="trigger_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'partially_failed', 'failed')",
            name="status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crawl_runs"),
    )
    op.create_index(
        "ix_crawl_runs_status_created_at",
        "crawl_runs",
        ["status", "created_at"],
    )

    op.create_table(
        "crawl_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("crawl_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("language_code", sa.Text(), nullable=False),
        sa.Column("country_code", sa.Text(), nullable=False),
        sa.Column(
            "attempt_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "task_type IN ('app_details', 'reviews')",
            name="task_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'retrying', 'succeeded', 'failed')",
            name="status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="attempt_count_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["crawl_run_id"],
            ["crawl_runs.id"],
            name="fk_crawl_tasks_crawl_run_id_crawl_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_crawl_tasks_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_crawl_tasks"),
        sa.UniqueConstraint(
            "crawl_run_id",
            "application_id",
            "task_type",
            name="uq_crawl_tasks_run_application_type",
        ),
    )
    op.create_index(
        "ix_crawl_tasks_run_status",
        "crawl_tasks",
        ["crawl_run_id", "status"],
    )
    op.create_index(
        "ix_crawl_tasks_application_id",
        "crawl_tasks",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_crawl_tasks_application_id", table_name="crawl_tasks")
    op.drop_index("ix_crawl_tasks_run_status", table_name="crawl_tasks")
    op.drop_table("crawl_tasks")
    op.drop_index("ix_crawl_runs_status_created_at", table_name="crawl_runs")
    op.drop_table("crawl_runs")
