from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

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
EXPECTED_LOCALES = {
    "org.telegram.messenger": ("en", "us"),
    "com.whatsapp": ("en", "us"),
    "com.instagram.android": ("en", "us"),
    "com.facebook.katana": ("en", "us"),
    "com.zhiliaoapp.musically": ("en", "us"),
    "com.myirancell": ("fa", "ir"),
    "ir.mci.ecareapp": ("fa", "ir"),
    "ir.rightel.myrightel": ("fa", "ir"),
    "com.shatelland.namava.mobile": ("fa", "ir"),
    "com.likotv": ("fa", "ir"),
    "ir.tamashakhonehtv": ("fa", "ir"),
    "com.plus9.fandogh": ("fa", "ir"),
    "com.BrainLadder.AmirzaGP": ("fa", "ir"),
    "com.plus9.samavar": ("fa", "ir"),
    "ir.android.baham": ("fa", "ir"),
    "app.pinno": ("fa", "ir"),
}
UNRELATED_APPLICATION_ID = UUID("898f535f-8d68-4f67-9f3a-918e125f97d3")


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _seeded_applications(
    connection: psycopg.Connection[Any],
) -> list[tuple[str, bool, str, bool, str, str]]:
    return connection.execute(
        """
        SELECT applications.package_name, applications.is_active, categories.code,
               application_categories.is_primary, applications.language_code,
               applications.country_code
        FROM applications
        JOIN application_categories
          ON application_categories.application_id = applications.id
        JOIN categories ON categories.id = application_categories.category_id
        WHERE applications.package_name = ANY(%s)
        ORDER BY applications.package_name
        """,
        (list(EXPECTED_APPLICATIONS),),
    ).fetchall()


def test_clean_upgrade_seeds_required_applications(
    database_url: str,
    db_connection: psycopg.Connection[Any],
) -> None:
    config = _config(database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    db_connection.execute(
        """
        INSERT INTO applications (id, name, package_name)
        VALUES (%s, 'Unrelated Application', 'com.example.unrelated')
        """,
        (UNRELATED_APPLICATION_ID,),
    )
    db_connection.execute(
        """
        INSERT INTO application_categories (application_id, category_id, is_primary)
        SELECT %s, id, true FROM categories WHERE code = 'video'
        """,
        (UNRELATED_APPLICATION_ID,),
    )
    db_connection.commit()
    command.upgrade(config, "head")

    seeded = _seeded_applications(db_connection)

    assert len(seeded) == 16
    assert all(is_active and is_primary for _, is_active, _, is_primary, _, _ in seeded)
    assert {
        package_name: category_code for package_name, _, category_code, _, _, _ in seeded
    } == EXPECTED_APPLICATIONS
    assert {
        package_name: (language_code, country_code)
        for package_name, _, _, _, language_code, country_code in seeded
    } == EXPECTED_LOCALES
    assert next(row for row in seeded if row[0] == "ir.rightel.myrightel")[1] is True

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

    unrelated = db_connection.execute(
        """
        SELECT applications.name, applications.package_name, categories.code,
               application_categories.is_primary
        FROM applications
        JOIN application_categories
          ON application_categories.application_id = applications.id
        JOIN categories ON categories.id = application_categories.category_id
        WHERE applications.id = %s
        """,
        (UNRELATED_APPLICATION_ID,),
    ).fetchone()
    assert unrelated == ("Unrelated Application", "com.example.unrelated", "video", True)


def test_fresh_upgrade_seeds_required_applications(
    database_url: str,
    db_connection: psycopg.Connection[Any],
) -> None:
    config = _config(database_url)
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    seeded = _seeded_applications(db_connection)
    assert len(seeded) == len(EXPECTED_APPLICATIONS)
    assert all(is_active and is_primary for _, is_active, _, is_primary, _, _ in seeded)
    assert {
        package_name: category_code for package_name, _, category_code, _, _, _ in seeded
    } == EXPECTED_APPLICATIONS
    assert {
        package_name: (language_code, country_code)
        for package_name, _, _, _, language_code, country_code in seeded
    } == EXPECTED_LOCALES
