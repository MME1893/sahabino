from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from tests.integration.crawler.conftest import psycopg_dsn

PREVIOUS_REVISION = "20260916_0006"
CURRENT_REVISION = "20260917_0007"
INDEX_REVISION = "20260917_0008"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def test_migration_preserves_historical_identity_and_uses_safe_defaults(
    crawler_database_url: str,
) -> None:
    config = _config(crawler_database_url)
    command.downgrade(config, PREVIOUS_REVISION)
    dsn = psycopg_dsn(crawler_database_url)
    application_id = uuid4()
    historical_run_id = uuid4()
    old_image_run_id = uuid4()
    historical_task_id = uuid4()
    old_image_task_id = uuid4()
    observed_at = datetime(2026, 9, 16, 12, tzinfo=UTC)

    try:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "INSERT INTO applications (id, name, package_name) VALUES (%s, %s, %s)",
                (application_id, "Sentiment Migration", f"example.{uuid4().hex}"),
            )
            for run_id in (historical_run_id, old_image_run_id):
                connection.execute(
                    "INSERT INTO crawl_runs (id, trigger_type, status) "
                    "VALUES (%s, 'manual', 'running')",
                    (run_id,),
                )
            for task_id, run_id in (
                (historical_task_id, historical_run_id),
                (old_image_task_id, old_image_run_id),
            ):
                connection.execute(
                    """
                INSERT INTO crawl_tasks (
                    id, crawl_run_id, application_id, task_type, status,
                    language_code, country_code
                ) VALUES (%s, %s, %s, 'reviews', 'running', 'en', 'us')
                """,
                    (task_id, run_id, application_id),
                )
            connection.execute(
                """
                INSERT INTO reviews (
                    id, application_id, external_review_id, source_at, author_name,
                    thumbs_up_count, score, content, source_adapter,
                    first_observed_at, last_observed_at
                ) VALUES (
                    7001, %s, 'historical-review', %s, 'Reviewer', 0, 5,
                    'current text must not be copied', 'migration-test', %s, %s
                )
                """,
                (application_id, observed_at, observed_at, observed_at),
            )
            connection.execute(
                """
                INSERT INTO review_observations (
                    crawl_task_id, review_id, observed_at, position, score,
                    thumbs_up_count, source_adapter
                ) VALUES (%s, 7001, %s, 1, 5, 0, 'migration-test')
                """,
                (historical_task_id, observed_at),
            )

        command.upgrade(config, CURRENT_REVISION)

        with psycopg.connect(dsn) as connection:
            historical = connection.execute(
                """
                SELECT content, source_at, sentiment_status, sentiment_attempt_count
                FROM review_observations
                WHERE crawl_task_id = %s AND review_id = 7001
                """,
                (historical_task_id,),
            ).fetchone()
            assert historical == (None, None, "skipped", 0)

            connection.execute(
                """
                INSERT INTO review_observations (
                    crawl_task_id, review_id, observed_at, position, score,
                    thumbs_up_count, source_adapter
                ) VALUES (%s, 7001, %s, 2, 5, 0, 'old-ingestion-image')
                """,
                (old_image_task_id, observed_at),
            )
            old_image_row = connection.execute(
                """
                SELECT content, sentiment_status, sentiment_attempt_count
                FROM review_observations WHERE crawl_task_id = %s AND review_id = 7001
                """,
                (old_image_task_id,),
            ).fetchone()
            assert old_image_row == (None, "pending", 0)
            connection.commit()

            with pytest.raises(psycopg.errors.CheckViolation):
                connection.execute(
                    "UPDATE review_observations SET sentiment_status = 'unknown' "
                    "WHERE crawl_task_id = %s",
                    (old_image_task_id,),
                )
            connection.rollback()

            index = connection.execute(
                """
                SELECT indexdef FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND indexname = 'ix_review_observations_sentiment_pending'
                """
            ).fetchone()
            assert index is not None
            assert "WHERE (sentiment_status = 'pending'::text)" in index[0]

        command.downgrade(config, PREVIOUS_REVISION)
        with psycopg.connect(dsn) as connection:
            columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'review_observations'
                    """
                )
            }
            assert "content" not in columns
            assert connection.execute(
                "SELECT count(*) FROM review_observations WHERE review_id = 7001"
            ).fetchone() == (2,)
    finally:
        command.upgrade(config, "head")


def test_review_observation_review_id_index_upgrade_and_downgrade(
    crawler_database_url: str,
) -> None:
    config = _config(crawler_database_url)
    dsn = psycopg_dsn(crawler_database_url)
    command.downgrade(config, CURRENT_REVISION)

    try:
        with psycopg.connect(dsn) as connection:
            assert _review_id_index_definition(connection) is None

        command.upgrade(config, INDEX_REVISION)
        with psycopg.connect(dsn) as connection:
            index_definition = _review_id_index_definition(connection)
            assert index_definition is not None
            assert index_definition.startswith("CREATE INDEX ")
            assert " USING btree (review_id)" in index_definition

        command.downgrade(config, CURRENT_REVISION)
        with psycopg.connect(dsn) as connection:
            assert _review_id_index_definition(connection) is None
    finally:
        command.upgrade(config, "head")


def _review_id_index_definition(connection: psycopg.Connection[object]) -> str | None:
    row = connection.execute(
        """
        SELECT indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema()
          AND tablename = 'review_observations'
          AND indexname = 'ix_review_observations_review_id'
        """
    ).fetchone()
    return None if row is None else str(row[0])
