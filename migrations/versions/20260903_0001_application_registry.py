"""Create the application registry schema and seed categories.

Revision ID: 20260903_0001
Revises:
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("package_name", sa.Text(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "(is_active IS TRUE AND deactivated_at IS NULL) OR "
            "(is_active IS FALSE AND deactivated_at IS NOT NULL)",
            name="ck_applications_active_deactivation",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_applications"),
        sa.UniqueConstraint(
            "package_name",
            name="uq_applications_package_name",
        ),
    )

    op.create_table(
        "categories",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(),
            nullable=False,
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("code", name="uq_categories_code"),
    )

    op.create_table(
        "application_categories",
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["applications.id"],
            name="fk_application_categories_application_id_applications",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_application_categories_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "application_id",
            "category_id",
            name="pk_application_categories",
        ),
    )
    op.create_index(
        "ix_application_categories_category_id",
        "application_categories",
        ["category_id"],
    )
    op.create_index(
        "uq_application_primary_category",
        "application_categories",
        ["application_id"],
        unique=True,
        postgresql_where=sa.text("is_primary IS TRUE"),
    )

    categories = sa.table(
        "categories",
        sa.column("code", sa.Text()),
        sa.column("name", sa.Text()),
    )
    op.bulk_insert(
        categories,
        [
            {"code": "messaging", "name": "Messaging"},
            {"code": "operator", "name": "Mobile Operator"},
            {"code": "video", "name": "Video Streaming"},
            {"code": "word_game", "name": "Word Game"},
            {"code": "chat_dating", "name": "Chat & Dating"},
            {"code": "social_network", "name": "Social Network"},
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "uq_application_primary_category",
        table_name="application_categories",
        postgresql_where=sa.text("is_primary IS TRUE"),
    )
    op.drop_index(
        "ix_application_categories_category_id",
        table_name="application_categories",
    )
    op.drop_table("application_categories")
    op.drop_table("categories")
    op.drop_table("applications")
