"""Add an index for review observation lookups by review ID.

Revision ID: 20260917_0008
Revises: 20260917_0007
Create Date: 2026-09-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260917_0008"
down_revision: str | None = "20260917_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_review_observations_review_id",
        "review_observations",
        ["review_id"],
        unique=False,
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_observations_review_id",
        table_name="review_observations",
    )
