from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError

from sahabino.crawler.domain.dto import ApplicationRef
from sahabino.crawler.domain.results import (
    CrawlRunStatus,
    CrawlTaskStatus,
    CrawlTaskType,
    TriggerType,
)
from sahabino.crawler.infrastructure.persistence.models import CrawlRun, CrawlTask
from sahabino.crawler.infrastructure.persistence.repository import (
    SqlAlchemyLifecycleRepository,
)
from sahabino.db.sync_session import create_sync_engine, create_sync_session_factory

from .conftest import psycopg_dsn


def _application(database_url: str, name: str = "Example") -> ApplicationRef:
    application_id = uuid4()
    package_name = f"com.example.{application_id.hex}"
    with psycopg.connect(psycopg_dsn(database_url), autocommit=True) as connection:
        connection.execute(
            "INSERT INTO applications (id, name, package_name, is_active) "
            "VALUES (%s, %s, %s, true)",
            (application_id, name, package_name),
        )
    return ApplicationRef(application_id, name, package_name)


def test_alembic_created_lifecycle_tables_constraints_indexes_and_foreign_keys(
    crawler_database_url: str,
) -> None:
    engine = create_sync_engine(crawler_database_url)
    try:
        inspector = inspect(engine)
        assert {"crawl_runs", "crawl_tasks"}.issubset(inspector.get_table_names())
        task_indexes = {item["name"] for item in inspector.get_indexes("crawl_tasks")}
        assert {
            "ix_crawl_tasks_run_status",
            "ix_crawl_tasks_application_id",
        }.issubset(task_indexes)
        foreign_tables = {
            item["referred_table"] for item in inspector.get_foreign_keys("crawl_tasks")
        }
        assert foreign_tables == {"crawl_runs", "applications"}
        checks = {item["name"] for item in inspector.get_check_constraints("crawl_tasks")}
        assert {
            "ck_crawl_tasks_task_type",
            "ck_crawl_tasks_status",
            "ck_crawl_tasks_attempt_count_non_negative",
        }.issubset(checks)
    finally:
        engine.dispose()


def test_task_lifecycle_independent_results_and_partial_run_status(
    crawler_database_url: str,
) -> None:
    application = _application(crawler_database_url)
    session_factory = create_sync_session_factory(crawler_database_url)
    repository = SqlAlchemyLifecycleRepository(session_factory)

    with repository.transaction() as transaction:
        run_id = transaction.create_run(TriggerType.MANUAL, crawler_version="test")
        task_ids = transaction.create_tasks(run_id, [application], "en", "us")
        transaction.commit()

    app_task = task_ids[(application.application_id, CrawlTaskType.APP_DETAILS)]
    review_task = task_ids[(application.application_id, CrawlTaskType.REVIEWS)]
    with repository.transaction() as transaction:
        transaction.begin_attempt(app_task)
        transaction.mark_task_succeeded(app_task)
        transaction.begin_attempt(review_task)
        transaction.mark_retrying(review_task)
        transaction.commit()
    with repository.transaction() as transaction:
        transaction.begin_attempt(review_task)
        transaction.mark_task_failed(review_task, "UPSTREAM_FAILURE", "temporary outage")
        transaction.finish_run(run_id)
        transaction.commit()

    with session_factory() as session:
        run = session.get(CrawlRun, run_id)
        tasks = session.scalars(select(CrawlTask).where(CrawlTask.crawl_run_id == run_id)).all()
        assert run is not None
        assert run.status == CrawlRunStatus.PARTIALLY_FAILED.value
        assert run.finished_at is not None
        by_type = {task.task_type: task for task in tasks}
        assert by_type[CrawlTaskType.APP_DETAILS.value].status == CrawlTaskStatus.SUCCEEDED
        assert by_type[CrawlTaskType.REVIEWS.value].status == CrawlTaskStatus.FAILED
        assert by_type[CrawlTaskType.REVIEWS.value].attempt_count == 2


def test_zero_tasks_finishes_succeeded_and_always_sets_finished_at(
    crawler_database_url: str,
) -> None:
    session_factory = create_sync_session_factory(crawler_database_url)
    repository = SqlAlchemyLifecycleRepository(session_factory)
    with repository.transaction() as transaction:
        run_id = transaction.create_run(TriggerType.SCHEDULED, scheduled_for=datetime.now(UTC))
        transaction.finish_run(run_id)
        transaction.commit()

    with session_factory() as session:
        run = session.get(CrawlRun, run_id)
        assert run is not None
        assert run.status == CrawlRunStatus.SUCCEEDED.value
        assert run.finished_at is not None


def test_unique_task_and_status_constraints_are_enforced(
    crawler_database_url: str,
) -> None:
    application = _application(crawler_database_url)
    engine = create_sync_engine(crawler_database_url)
    run_id = uuid4()
    task_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO crawl_runs (id, trigger_type, status) "
                "VALUES (:id, 'manual', 'running')"
            ),
            {"id": run_id},
        )
        values = {
            "id": task_id,
            "run_id": run_id,
            "application_id": application.application_id,
        }
        connection.execute(
            text(
                "INSERT INTO crawl_tasks "
                "(id, crawl_run_id, application_id, task_type, status, "
                "language_code, country_code) VALUES "
                "(:id, :run_id, :application_id, 'reviews', 'pending', 'en', 'us')"
            ),
            values,
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO crawl_tasks "
                "(id, crawl_run_id, application_id, task_type, status, "
                "language_code, country_code) VALUES "
                "(:id, :run_id, :application_id, 'reviews', 'pending', 'en', 'us')"
            ),
            {**values, "id": uuid4()},
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE crawl_tasks SET status = 'unknown' WHERE id = :id"),
            {"id": task_id},
        )
    engine.dispose()


def test_uncommitted_repository_transaction_rolls_back(
    crawler_database_url: str,
) -> None:
    session_factory = create_sync_session_factory(crawler_database_url)
    repository = SqlAlchemyLifecycleRepository(session_factory)
    with repository.transaction() as transaction:
        transaction.create_run(TriggerType.MANUAL)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(CrawlRun)) == 0
