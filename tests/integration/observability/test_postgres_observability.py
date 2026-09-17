from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parents[3]
READER_SQL = ROOT / "deploy/ansible/roles/sahabino_deploy/files/grafana_reader.sql"
DASHBOARD_DIR = ROOT / "infrastructure/observability/grafana/dashboards"
PASSWORD = "Observability$Reader-2026"
POSTGRES_IMAGE = "postgres:16-alpine"


def _sqlalchemy_url(value: str) -> str:
    return make_url(value).set(drivername="postgresql+psycopg").render_as_string(False)


def _run_migrations(database_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    command.upgrade(config, "head")


@pytest.fixture(scope="module")
def postgres() -> Iterator[tuple[PostgresContainer, str]]:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as container:
        database_url = _sqlalchemy_url(container.get_connection_url())
        _run_migrations(database_url)
        yield container, database_url


def _dsn(database_url: str, *, user: str | None = None, password: str | None = None) -> str:
    url = make_url(database_url).set(drivername="postgresql")
    if user is not None:
        url = url.set(username=user, password=password)
    return url.render_as_string(hide_password=False)


def _run_reader_sql(container: PostgresContainer, password: str, *, rotate: bool = False) -> str:
    container_id = container.get_wrapped_container().id
    script = (
        f"\\set reader_password '{password}'\n"
        f"\\set rotate_password '{str(rotate).lower()}'\n" + READER_SQL.read_text()
    )
    result = subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container_id,
            "psql",
            "--no-psqlrc",
            "--username",
            container.username,
            "--dbname",
            container.dbname,
        ],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _seed_representative_data(database_url: str) -> str:
    with psycopg.connect(_dsn(database_url), autocommit=True) as connection:
        application_id = connection.execute(
            "SELECT id::text FROM applications ORDER BY name LIMIT 1"
        ).fetchone()[0]
        run_id = uuid4()
        details_task_id = uuid4()
        review_task_id = uuid4()
        connection.execute(
            """
            INSERT INTO crawl_runs (
                id, trigger_type, status, started_at, finished_at, created_at
            ) VALUES (%s, 'manual', 'partially_failed', now() - interval '2 minutes',
                      now(), now() - interval '2 minutes')
            """,
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO crawl_tasks (
                id, crawl_run_id, application_id, task_type, status,
                language_code, country_code, attempt_count, started_at,
                finished_at, error_code, created_at
            ) VALUES
                (%s, %s, %s, 'app_details', 'succeeded', 'en', 'us', 1,
                 now() - interval '90 seconds', now(), NULL, now() - interval '2 minutes'),
                (%s, %s, %s, 'reviews', 'failed', 'fa', 'ir', 2,
                 now() - interval '60 seconds', now(), 'UPSTREAM', now() - interval '2 minutes')
            """,
            (details_task_id, run_id, application_id, review_task_id, run_id, application_id),
        )
        review_id = connection.execute(
            """
            INSERT INTO reviews (
                application_id, external_review_id, source_at, author_name,
                thumbs_up_count, score, content, source_adapter,
                first_observed_at, last_observed_at
            ) VALUES (%s, 'external-private-id', now() - interval '1 day',
                      'Private Author', 3, 5, 'Private review content', 'test', now(), now())
            RETURNING id
            """,
            (application_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO review_observations (
                crawl_task_id, review_id, observed_at, position, score,
                thumbs_up_count, source_adapter, source_at, sentiment_status,
                sentiment_label, sentiment_attempt_count
            ) VALUES (%s, %s, now(), 1, 5, 3, 'test', now() - interval '1 day',
                      'done', 'positive', 1)
            """,
            (review_task_id, review_id),
        )
    return application_id


def _render_grafana_sql(sql: str, application_id: str) -> str:
    rendered = re.sub(
        r"\$__timeGroupAlias\(([^,]+),\s*'1h'\)",
        r'''date_trunc('hour', \1) AS "time"''',
        sql,
    )
    rendered = re.sub(
        r"\$__timeFilter\(([^)]+)\)",
        r"\1 BETWEEN now() - interval '30 days' AND now()",
        rendered,
    )
    return rendered.replace("${application_id:sqlstring}", f"'{application_id}'")


def test_reader_is_idempotent_restricted_and_executes_every_dashboard_query(
    postgres: tuple[PostgresContainer, str],
) -> None:
    container, database_url = postgres
    application_id = _seed_representative_data(database_url)

    first = _run_reader_sql(container, PASSWORD)
    second = _run_reader_sql(container, PASSWORD)
    assert "GRAFANA_READER_CREATED" in first
    assert "GRAFANA_READER_VERIFIED" in second

    reader_dsn = _dsn(database_url, user="grafana_reader", password=PASSWORD)
    with psycopg.connect(reader_dsn, autocommit=True) as connection:
        assert connection.execute("SELECT count(id) FROM reviews").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(review_id) FROM review_observations").fetchone()[0]
            == 1
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT content FROM reviews")
        with pytest.raises(
            (psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)
        ):
            connection.execute("DELETE FROM reviews")

        for path in sorted(DASHBOARD_DIR.glob("sahabino-*.json")):
            if path.name == "sahabino-logging-smoke.json":
                continue
            dashboard = json.loads(path.read_text())
            for panel in dashboard["panels"]:
                for target in panel["targets"]:
                    connection.execute(_render_grafana_sql(target["rawSql"], application_id))

    with psycopg.connect(_dsn(database_url), autocommit=True) as connection:
        attributes = connection.execute(
            """
            SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication,
                   rolbypassrls, rolconnlimit
            FROM pg_roles WHERE rolname = 'grafana_reader'
            """
        ).fetchone()
        assert attributes == (False, False, False, False, False, 5)
        assert (
            connection.execute(
                "SELECT has_table_privilege('grafana_reader', 'reviews', 'SELECT')"
            ).fetchone()[0]
            is False
        )
        assert (
            connection.execute(
                "SELECT has_column_privilege('grafana_reader', 'reviews', 'id', 'SELECT')"
            ).fetchone()[0]
            is True
        )
        assert (
            connection.execute(
                "SELECT has_column_privilege('grafana_reader', 'reviews', 'content', 'SELECT')"
            ).fetchone()[0]
            is False
        )
        assert (
            connection.execute(
                "SELECT has_table_privilege('grafana_reader', 'reviews', 'INSERT')"
            ).fetchone()[0]
            is False
        )


def test_reader_has_bounded_verification_headroom_after_grafana_pool_connections(
    postgres: tuple[PostgresContainer, str],
) -> None:
    container, database_url = postgres
    reader_dsn = _dsn(database_url, user="grafana_reader", password=PASSWORD)
    grafana_pool = [psycopg.connect(reader_dsn) for _ in range(3)]

    try:
        with psycopg.connect(reader_dsn) as verification_connection:
            assert verification_connection.execute("SELECT 1").fetchone() == (1,)
        assert "GRAFANA_READER_VERIFIED" in _run_reader_sql(container, PASSWORD)
    finally:
        for connection in grafana_pool:
            connection.close()


def test_existing_reader_password_requires_explicit_rotation(
    postgres: tuple[PostgresContainer, str],
) -> None:
    container, database_url = postgres
    replacement = "Different$Reader-Password-2026"

    _run_reader_sql(container, replacement, rotate=False)
    with psycopg.connect(_dsn(database_url, user="grafana_reader", password=PASSWORD)):
        pass
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(_dsn(database_url, user="grafana_reader", password=replacement))

    _run_reader_sql(container, replacement, rotate=True)
    with psycopg.connect(_dsn(database_url, user="grafana_reader", password=replacement)):
        pass
