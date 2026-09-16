"""Add per-application locales and raise the review position limit.

Revision ID: 20260916_0006
Revises: 20260913_0005
Create Date: 2026-09-16

Downgrade drops the locale columns and therefore loses any locale values stored
after upgrade. It refuses to restore the old review constraint when observations
with positions above 100 exist.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0006"
down_revision: str | None = "20260913_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPLICATION_LOCALES = (
    ("org.telegram.messenger", "en", "us"),
    ("com.whatsapp", "en", "us"),
    ("com.instagram.android", "en", "us"),
    ("com.facebook.katana", "en", "us"),
    ("com.zhiliaoapp.musically", "en", "us"),
    ("com.myirancell", "fa", "ir"),
    ("ir.mci.ecareapp", "fa", "ir"),
    ("ir.rightel.myrightel", "fa", "ir"),
    ("com.shatelland.namava.mobile", "fa", "ir"),
    ("com.likotv", "fa", "ir"),
    ("ir.tamashakhonehtv", "fa", "ir"),
    ("com.plus9.fandogh", "fa", "ir"),
    ("com.BrainLadder.AmirzaGP", "fa", "ir"),
    ("com.plus9.samavar", "fa", "ir"),
    ("ir.android.baham", "fa", "ir"),
    ("app.pinno", "fa", "ir"),
)


def _applications_table() -> sa.Table:
    return sa.Table(
        "applications",
        sa.MetaData(),
        sa.Column("package_name", sa.Text()),
        sa.Column("language_code", sa.Text()),
        sa.Column("country_code", sa.Text()),
    )


def upgrade() -> None:
    op.add_column("applications", sa.Column("language_code", sa.Text(), nullable=True))
    op.add_column("applications", sa.Column("country_code", sa.Text(), nullable=True))

    applications = _applications_table()
    connection = op.get_bind()
    for package_name, language_code, country_code in _APPLICATION_LOCALES:
        connection.execute(
            applications.update()
            .where(applications.c.package_name == package_name)
            .values(language_code=language_code, country_code=country_code)
        )

    op.execute(
        "ALTER TABLE applications "
        "ADD CONSTRAINT ck_applications_locale_pair "
        "CHECK ((language_code IS NULL AND country_code IS NULL) OR "
        "(language_code IS NOT NULL AND country_code IS NOT NULL)) NOT VALID"
    )
    op.execute("ALTER TABLE applications VALIDATE CONSTRAINT ck_applications_locale_pair")

    op.drop_constraint(
        op.f("ck_review_observations_ck_review_observations_position_range"),
        "review_observations",
        type_="check",
    )
    op.execute(
        "ALTER TABLE review_observations "
        "ADD CONSTRAINT ck_review_observations_position_range "
        "CHECK (position >= 1 AND position <= 1000) NOT VALID"
    )
    op.execute(
        "ALTER TABLE review_observations VALIDATE CONSTRAINT ck_review_observations_position_range"
    )


def downgrade() -> None:
    connection = op.get_bind()
    op.execute("LOCK TABLE review_observations IN SHARE ROW EXCLUSIVE MODE")
    has_unsupported_positions = connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM review_observations WHERE position > 100)")
    ).scalar_one()
    if has_unsupported_positions:
        raise RuntimeError(
            "Cannot downgrade to review position limit 100 while "
            "review_observations contains positions above 100"
        )

    op.drop_constraint(
        op.f("ck_review_observations_position_range"),
        "review_observations",
        type_="check",
    )

    op.execute(
        "ALTER TABLE review_observations "
        "ADD CONSTRAINT "
        "ck_review_observations_ck_review_observations_position_range "
        "CHECK (position >= 1 AND position <= 100) NOT VALID"
    )

    op.execute(
        "ALTER TABLE review_observations "
        "VALIDATE CONSTRAINT "
        "ck_review_observations_ck_review_observations_position_range"
    )
    op.drop_constraint(
        op.f("ck_applications_locale_pair"),
        "applications",
        type_="check",
    )
    op.drop_column("applications", "country_code")
    op.drop_column("applications", "language_code")
