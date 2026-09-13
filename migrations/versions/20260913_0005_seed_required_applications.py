"""Seed the required Google Play applications.

Revision ID: 20260913_0005
Revises: 20260912_0004
Create Date: 2026-09-13
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260913_0005"
down_revision: str | None = "20260912_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The fixed IDs let downgrade identify applications created by this migration without
# treating an application that merely has the same package name as migration-owned.
_APPLICATIONS = (
    ("ab9affb3-0c68-507c-a818-795a24359cd5", "Telegram", "org.telegram.messenger", "messaging"),
    ("bcbcbf5f-331e-58d8-976d-2e498b4eaba5", "WhatsApp", "com.whatsapp", "messaging"),
    ("35f3029b-ee2b-5f42-81d0-b9259a4c40ea", "MyIrancell", "com.myirancell", "operator"),
    ("3740db0d-90d6-5ae8-8be0-bf7d8ca944b4", "MyMCI", "ir.mci.ecareapp", "operator"),
    ("7fe2b1f3-9be7-5762-ac18-560d7caafffe", "MyRightel", "ir.rightel.myrightel", "operator"),
    ("953ead21-30ee-5aa0-b045-f10e7ba979af", "Namava", "com.shatelland.namava.mobile", "video"),
    ("22a49886-e179-5411-ad7e-fb4ad1a2def1", "Lenz", "com.likotv", "video"),
    ("079b7804-90f5-5f84-ab62-f88ce69a41b1", "Tamashakhonehtv", "ir.tamashakhonehtv", "video"),
    ("1ae94e2a-2dfe-59ad-8409-366d4abdb0c4", "Fandogh", "com.plus9.fandogh", "word_game"),
    ("da2c65fe-a199-5577-ae9f-b9ed21a25b9b", "Amirza", "com.BrainLadder.AmirzaGP", "word_game"),
    ("c7574882-2b3a-5f67-a2a9-80b9df2690de", "Samavar", "com.plus9.samavar", "word_game"),
    ("5854a015-a5a2-59a1-91f2-b2960a42ee29", "Baham", "ir.android.baham", "chat_dating"),
    ("3dd800a0-045a-5621-a011-a3b2fb405ba4", "Pinno", "app.pinno", "chat_dating"),
    (
        "1518aa07-ad7f-5709-b00c-3c6466a2b7dc",
        "Instagram",
        "com.instagram.android",
        "social_network",
    ),
    ("dc44cf8d-5f00-56d6-9427-4fffb21eef4b", "Facebook", "com.facebook.katana", "social_network"),
    (
        "cdcc54d4-e347-5af0-b931-141b801104fb",
        "TikTok",
        "com.zhiliaoapp.musically",
        "social_network",
    ),
)


def _tables() -> tuple[sa.Table, sa.Table, sa.Table]:
    metadata = sa.MetaData()
    applications = sa.Table(
        "applications",
        metadata,
        sa.Column("id", postgresql.UUID(as_uuid=True)),
        sa.Column("name", sa.Text()),
        sa.Column("package_name", sa.Text()),
        sa.Column("is_active", sa.Boolean()),
    )
    categories = sa.Table(
        "categories",
        metadata,
        sa.Column("id", sa.BigInteger()),
        sa.Column("code", sa.Text()),
    )
    application_categories = sa.Table(
        "application_categories",
        metadata,
        sa.Column("application_id", postgresql.UUID(as_uuid=True)),
        sa.Column("category_id", sa.BigInteger()),
        sa.Column("is_primary", sa.Boolean()),
    )
    return applications, categories, application_categories


def upgrade() -> None:
    applications, categories, application_categories = _tables()
    connection = op.get_bind()

    for application_id, name, package_name, category_code in _APPLICATIONS:
        seed_id = UUID(application_id)
        connection.execute(
            postgresql.insert(applications)
            .values(id=seed_id, name=name, package_name=package_name, is_active=True)
            .on_conflict_do_nothing()
        )

        # Only the deterministic ID can receive the seed association. If the package
        # already belonged to another row, the insert above did nothing and this select
        # returns no rows, leaving that user-managed application untouched.
        assignment = sa.select(
            applications.c.id,
            categories.c.id,
            sa.literal(True),
        ).where(
            applications.c.id == seed_id,
            applications.c.package_name == package_name,
            categories.c.code == category_code,
        )
        connection.execute(
            postgresql.insert(application_categories)
            .from_select(
                ["application_id", "category_id", "is_primary"],
                assignment,
            )
            .on_conflict_do_nothing()
        )


def downgrade() -> None:
    applications, _, application_categories = _tables()
    connection = op.get_bind()

    for application_id, _, package_name, _ in _APPLICATIONS:
        seed_id = UUID(application_id)
        owned_application = sa.and_(
            applications.c.id == seed_id,
            applications.c.package_name == package_name,
        )
        connection.execute(
            application_categories.delete().where(
                application_categories.c.application_id.in_(
                    sa.select(applications.c.id).where(owned_application)
                )
            )
        )
        connection.execute(applications.delete().where(owned_application))
