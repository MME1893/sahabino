"""add Kafka ingestion persistence tables

Revision ID: 20260911_0003
Revises: 20260906_0002
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260911_0003"
down_revision: str | None = "20260906_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingested_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("partition", sa.Integer(), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "schema_version >= 1", name="ck_ingested_events_schema_version_positive"
        ),
        sa.CheckConstraint("partition >= 0", name="ck_ingested_events_partition_non_negative"),
        sa.CheckConstraint('"offset" >= 0', name="ck_ingested_events_offset_non_negative"),
        sa.PrimaryKeyConstraint("event_id", name="pk_ingested_events"),
        sa.UniqueConstraint(
            "topic",
            "partition",
            "offset",
            name="uq_ingested_events_topic_partition_offset",
        ),
    )

    op.create_table(
        "playstore_app_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("crawl_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("package_name", sa.Text(), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("min_installs", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("ratings_count", sa.BigInteger(), nullable=False),
        sa.Column("reviews_count", sa.BigInteger(), nullable=False),
        sa.Column("store_updated_on", sa.Date(), nullable=True),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("ad_supported", sa.Boolean(), nullable=False),
        sa.Column("source_adapter", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "min_installs >= 0",
            name="ck_playstore_app_snapshots_min_installs_non_negative",
        ),
        sa.CheckConstraint(
            "score >= 0 AND score <= 5",
            name="ck_playstore_app_snapshots_score_range",
        ),
        sa.CheckConstraint(
            "ratings_count >= 0",
            name="ck_playstore_app_snapshots_ratings_count_non_negative",
        ),
        sa.CheckConstraint(
            "reviews_count >= 0",
            name="ck_playstore_app_snapshots_reviews_count_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["crawl_task_id"],
            ["crawl_tasks.id"],
            name="fk_playstore_app_snapshots_crawl_task_id_crawl_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_playstore_app_snapshots_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_playstore_app_snapshots"),
        sa.UniqueConstraint("crawl_task_id", name="uq_playstore_app_snapshots_crawl_task_id"),
    )

    op.create_table(
        "reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_review_id", sa.Text(), nullable=False),
        sa.Column("source_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("thumbs_up_count", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_adapter", sa.Text(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("thumbs_up_count >= 0", name="ck_reviews_thumbs_up_count_non_negative"),
        sa.CheckConstraint("score >= 1 AND score <= 5", name="ck_reviews_score_range"),
        sa.CheckConstraint(
            "first_observed_at <= last_observed_at",
            name="ck_reviews_observation_order",
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_reviews_application_id_applications",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reviews"),
        sa.UniqueConstraint(
            "application_id",
            "external_review_id",
            name="uq_reviews_application_id_external_review_id",
        ),
    )

    op.create_table(
        "review_observations",
        sa.Column("crawl_task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("review_id", sa.BigInteger(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("thumbs_up_count", sa.BigInteger(), nullable=False),
        sa.Column("source_adapter", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "position >= 1 AND position <= 100",
            name="ck_review_observations_position_range",
        ),
        sa.CheckConstraint(
            "score >= 1 AND score <= 5",
            name="ck_review_observations_score_range",
        ),
        sa.CheckConstraint(
            "thumbs_up_count >= 0",
            name="ck_review_observations_thumbs_up_count_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["crawl_task_id"],
            ["crawl_tasks.id"],
            name="fk_review_observations_crawl_task_id_crawl_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["reviews.id"],
            name="fk_review_observations_review_id_reviews",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("crawl_task_id", "review_id", name="pk_review_observations"),
    )


def downgrade() -> None:
    op.drop_table("review_observations")
    op.drop_table("reviews")
    op.drop_table("playstore_app_snapshots")
    op.drop_table("ingested_events")
