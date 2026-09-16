from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config

PREVIOUS_REVISION = "20260913_0005"
CURRENT_REVISION = "20260916_0006"
APPLICATION_ID = UUID("8730ef85-dcee-47c7-b315-20dc4cfcb64f")
UNRELATED_APPLICATION_ID = UUID("36fdb73c-24eb-4678-9007-c0cabaf5c336")
RUN_ID = UUID("68d11ba5-dd59-4617-95ac-b81e439ddc66")
TASK_ID = UUID("8b884f34-c2b5-4709-a9e2-d11fdf6114d4")


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _insert_review(
    connection: psycopg.Connection[Any],
    *,
    review_id: int,
    external_review_id: str,
    position: int,
) -> None:
    observed_at = datetime(2026, 9, 15, 12, tzinfo=UTC)
    connection.execute(
        """
        INSERT INTO reviews (
            id, application_id, external_review_id, source_at, author_name,
            thumbs_up_count, score, content, source_adapter,
            first_observed_at, last_observed_at
        ) VALUES (%s, %s, %s, %s, 'Reviewer', 3, 5, 'preserved review',
                  'migration-test', %s, %s)
        """,
        (
            review_id,
            APPLICATION_ID,
            external_review_id,
            observed_at,
            observed_at,
            observed_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO review_observations (
            crawl_task_id, review_id, observed_at, position, score,
            thumbs_up_count, source_adapter
        ) VALUES (%s, %s, %s, %s, 5, 3, 'migration-test')
        """,
        (TASK_ID, review_id, observed_at, position),
    )


def test_locale_and_review_limit_migration_preserves_rows_and_refuses_unsafe_downgrade(
    database_url: str,
    db_connection: psycopg.Connection[Any],
) -> None:
    config = _config(database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    deactivated_at = datetime(2026, 9, 14, 8, tzinfo=UTC)
    db_connection.execute(
        """
        INSERT INTO applications (
            id, name, package_name, is_active, deactivated_at
        ) VALUES
            (%s, 'Namava Custom Name', 'com.shatelland.namava.mobile', false, %s),
            (%s, 'Unrelated', 'com.example.unrelated', true, NULL)
        """,
        (APPLICATION_ID, deactivated_at, UNRELATED_APPLICATION_ID),
    )
    db_connection.execute(
        """
        INSERT INTO crawl_runs (id, trigger_type, status)
        VALUES (%s, 'manual', 'running')
        """,
        (RUN_ID,),
    )
    db_connection.execute(
        """
        INSERT INTO crawl_tasks (
            id, crawl_run_id, application_id, task_type, status,
            language_code, country_code
        ) VALUES (%s, %s, %s, 'reviews', 'running', 'en', 'us')
        """,
        (TASK_ID, RUN_ID, APPLICATION_ID),
    )
    _insert_review(
        db_connection,
        review_id=9001,
        external_review_id="existing-review",
        position=100,
    )
    db_connection.commit()

    command.upgrade(config, "head")

    application_rows = db_connection.execute(
        """
        SELECT id, name, package_name, is_active, deactivated_at,
               language_code, country_code
        FROM applications
        ORDER BY name
        """
    ).fetchall()
    assert application_rows == [
        (
            APPLICATION_ID,
            "Namava Custom Name",
            "com.shatelland.namava.mobile",
            False,
            deactivated_at,
            "fa",
            "ir",
        ),
        (
            UNRELATED_APPLICATION_ID,
            "Unrelated",
            "com.example.unrelated",
            True,
            None,
            None,
            None,
        ),
    ]
    assert db_connection.execute(
        """
        SELECT reviews.id, reviews.external_review_id, reviews.content,
               review_observations.position
        FROM reviews
        JOIN review_observations ON review_observations.review_id = reviews.id
        WHERE reviews.id = 9001
        """
    ).fetchone() == (9001, "existing-review", "preserved review", 100)

    _insert_review(
        db_connection,
        review_id=9002,
        external_review_id="position-1000",
        position=1000,
    )
    db_connection.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_review(
            db_connection,
            review_id=9003,
            external_review_id="position-1001",
            position=1001,
        )
    db_connection.rollback()

    with pytest.raises(RuntimeError, match="positions above 100"):
        command.downgrade(config, PREVIOUS_REVISION)

    assert db_connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        CURRENT_REVISION,
    )
    assert db_connection.execute(
        "SELECT position FROM review_observations WHERE review_id = 9002"
    ).fetchone() == (1000,)
