"""Add standalone sentiment state to review observations.

Revision ID: 20260917_0007
Revises: 20260916_0006
Create Date: 2026-09-17

Existing observations intentionally receive ``skipped`` without copying the
current value from ``reviews.content``.  After the column is added, its default
is changed to ``pending`` for future inserts, including inserts from the old
ingestion image that does not yet know about the new columns.

Downgrade removes all observation content and sentiment processing state.  The
existing rows, composite primary key, and foreign keys are preserved, but the
removed data cannot be recovered without a backup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0007"
down_revision: str | None = "20260916_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_observations", sa.Column("content", sa.Text(), nullable=True))
    op.add_column(
        "review_observations",
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("review_observations", sa.Column("sentiment_language", sa.Text(), nullable=True))
    op.add_column("review_observations", sa.Column("sentiment_label", sa.Text(), nullable=True))
    op.add_column(
        "review_observations",
        sa.Column(
            "sentiment_status",
            sa.Text(),
            server_default=sa.text("'skipped'"),
            nullable=False,
        ),
    )
    op.add_column(
        "review_observations",
        sa.Column("sentiment_processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "review_observations",
        sa.Column(
            "sentiment_attempt_count",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )

    op.execute(
        "ALTER TABLE review_observations "
        "ADD CONSTRAINT ck_review_observations_sentiment_status_valid "
        "CHECK (sentiment_status IN ('pending', 'done', 'skipped', 'failed')) NOT VALID"
    )
    op.execute(
        "ALTER TABLE review_observations "
        "ADD CONSTRAINT ck_review_observations_sentiment_label_valid "
        "CHECK (sentiment_label IS NULL OR "
        "sentiment_label IN ('positive', 'neutral', 'negative')) NOT VALID"
    )
    op.execute(
        "ALTER TABLE review_observations "
        "VALIDATE CONSTRAINT ck_review_observations_sentiment_status_valid"
    )
    op.execute(
        "ALTER TABLE review_observations "
        "VALIDATE CONSTRAINT ck_review_observations_sentiment_label_valid"
    )

    op.alter_column(
        "review_observations",
        "sentiment_status",
        existing_type=sa.Text(),
        server_default=sa.text("'pending'"),
        existing_nullable=False,
    )
    op.create_index(
        "ix_review_observations_sentiment_pending",
        "review_observations",
        ["review_id", "crawl_task_id"],
        unique=False,
        postgresql_where=sa.text("sentiment_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_observations_sentiment_pending",
        table_name="review_observations",
        postgresql_where=sa.text("sentiment_status = 'pending'"),
    )
    op.drop_constraint(
        op.f("ck_review_observations_sentiment_label_valid"),
        "review_observations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_review_observations_sentiment_status_valid"),
        "review_observations",
        type_="check",
    )
    op.drop_column("review_observations", "sentiment_attempt_count")
    op.drop_column("review_observations", "sentiment_processed_at")
    op.drop_column("review_observations", "sentiment_status")
    op.drop_column("review_observations", "sentiment_label")
    op.drop_column("review_observations", "sentiment_language")
    op.drop_column("review_observations", "source_at")
    op.drop_column("review_observations", "content")
