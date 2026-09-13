from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
from alembic import command
from alembic.config import Config

PREVIOUS_REVISION = "20260912_0004"

EXPECTED_APPLICATIONS = {
    "org.telegram.messenger": "messaging",
    "com.whatsapp": "messaging",
    "com.myirancell": "operator",
    "ir.mci.ecareapp": "operator",
    "ir.rightel.myrightel": "operator",
    "com.shatelland.namava.mobile": "video",
    "com.likotv": "video",
    "ir.tamashakhonehtv": "video",
    "com.plus9.fandogh": "word_game",
    "com.BrainLadder.AmirzaGP": "word_game",
    "com.plus9.samavar": "word_game",
    "ir.android.baham": "chat_dating",
    "app.pinno": "chat_dating",
    "com.instagram.android": "social_network",
    "com.facebook.katana": "social_network",
    "com.zhiliaoapp.musically": "social_network",
}


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_clean_upgrade_seeds_required_applications(
    database_url: str,
    db_connection: psycopg.Connection[Any],
) -> None:
    config = _config(database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, "head")

    seeded = db_connection.execute(
        """
        SELECT applications.package_name, applications.is_active, categories.code,
               application_categories.is_primary
        FROM applications
        JOIN application_categories
          ON application_categories.application_id = applications.id
        JOIN categories ON categories.id = application_categories.category_id
        WHERE applications.package_name = ANY(%s)
        ORDER BY applications.package_name
        """,
        (list(EXPECTED_APPLICATIONS),),
    ).fetchall()

    assert len(seeded) == 16
    assert all(is_active and is_primary for _, is_active, _, is_primary in seeded)
    assert {package_name: category_code for package_name, _, category_code, _ in seeded} == (
        EXPECTED_APPLICATIONS
    )

    package_counts = db_connection.execute(
        """
        SELECT package_name, count(*)
        FROM applications
        WHERE package_name = ANY(%s)
        GROUP BY package_name
        """,
        (list(EXPECTED_APPLICATIONS),),
    ).fetchall()
    assert len(package_counts) == 16
    assert all(count == 1 for _, count in package_counts)
