from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg

from sentiment.contracts import AnalysisResult
from sentiment.worker import PostgresStore, SentimentWorker
from tests.integration.crawler.conftest import psycopg_dsn


class FakeAnalyzer:
    def __init__(self, outcome: AnalysisResult | Exception | None = None) -> None:
        self.outcome = outcome or AnalysisResult(language="en", label="positive", status="done")
        self.calls: list[list[str | None]] = []

    def analyze_batch(self, contents: list[str | None]) -> list[AnalysisResult | Exception]:
        self.calls.append(list(contents))
        return [self.outcome for _ in contents]


def _seed_base(connection: psycopg.Connection[Any]) -> UUID:
    application_id = uuid4()
    connection.execute(
        "INSERT INTO applications (id, name, package_name) VALUES (%s, %s, %s)",
        (application_id, "Worker Integration", f"example.{uuid4().hex}"),
    )
    return application_id


def _seed_review(
    connection: psycopg.Connection[Any],
    *,
    application_id: UUID,
    review_id: int,
    external_review_id: str,
    observations: list[tuple[str, int, int]],
) -> None:
    observed_at = datetime(2026, 9, 17, 12, tzinfo=UTC)
    connection.execute(
        """
        INSERT INTO reviews (
            id, application_id, external_review_id, source_at, author_name,
            thumbs_up_count, score, content, source_adapter,
            first_observed_at, last_observed_at
        ) VALUES (%s, %s, %s, %s, 'Reviewer', 0, 5, %s, 'worker-test', %s, %s)
        """,
        (
            review_id,
            application_id,
            external_review_id,
            observed_at,
            observations[-1][0],
            observed_at,
            observed_at,
        ),
    )
    for position, (content, score, thumbs_up_count) in enumerate(observations, start=1):
        run_id = uuid4()
        task_id = uuid4()
        connection.execute(
            "INSERT INTO crawl_runs (id, trigger_type, status) VALUES (%s, 'manual', 'running')",
            (run_id,),
        )
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
            INSERT INTO review_observations (
                crawl_task_id, review_id, observed_at, position, score,
                thumbs_up_count, source_adapter, content, source_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'worker-test', %s, %s)
            """,
            (
                task_id,
                review_id,
                observed_at,
                position,
                score,
                thumbs_up_count,
                content,
                observed_at,
            ),
        )


def test_postgres_deduplication_uses_review_and_exact_content(
    crawler_database_url: str,
) -> None:
    dsn = psycopg_dsn(crawler_database_url)
    with psycopg.connect(dsn, autocommit=True) as connection:
        application_id = _seed_base(connection)
        _seed_review(
            connection,
            application_id=application_id,
            review_id=8101,
            external_review_id="review-one",
            observations=[("same text", 5, 0)],
        )
        _seed_review(
            connection,
            application_id=application_id,
            review_id=8102,
            external_review_id="review-two",
            observations=[("same text", 5, 0)],
        )
        store = PostgresStore(connection)
        initial_analyzer = FakeAnalyzer()

        first = SentimentWorker(store, initial_analyzer).drain()

        assert initial_analyzer.calls == [["same text", "same text"]]
        assert first.newly_analyzed_texts == 2

        _seed_additional_observations(
            connection,
            application_id=application_id,
            review_id=8101,
            observations=[("same text", 1, 99), ("changed text", 5, 0), ("changed text", 4, 8)],
        )
        next_analyzer = FakeAnalyzer()

        second = SentimentWorker(store, next_analyzer).drain()

        assert next_analyzer.calls == [["changed text"]]
        assert second.reused_results == 1
        assert second.newly_analyzed_texts == 1
        rows = connection.execute(
            """
            SELECT content, sentiment_status, sentiment_label, count(*)
            FROM review_observations
            WHERE review_id = 8101
            GROUP BY content, sentiment_status, sentiment_label
            ORDER BY content
            """
        ).fetchall()
        assert rows == [
            ("changed text", "done", "positive", 2),
            ("same text", "done", "positive", 2),
        ]


def _seed_additional_observations(
    connection: psycopg.Connection[Any],
    *,
    application_id: UUID,
    review_id: int,
    observations: list[tuple[str, int, int]],
) -> None:
    observed_at = datetime(2026, 9, 17, 13, tzinfo=UTC)
    for position, (content, score, thumbs_up_count) in enumerate(observations, start=20):
        run_id = uuid4()
        task_id = uuid4()
        connection.execute(
            "INSERT INTO crawl_runs (id, trigger_type, status) VALUES (%s, 'manual', 'running')",
            (run_id,),
        )
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
            INSERT INTO review_observations (
                crawl_task_id, review_id, observed_at, position, score,
                thumbs_up_count, source_adapter, content, source_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 'worker-test', %s, %s)
            """,
            (
                task_id,
                review_id,
                observed_at,
                position,
                score,
                thumbs_up_count,
                content,
                observed_at,
            ),
        )


def test_postgres_retry_restart_and_advisory_lock(crawler_database_url: str) -> None:
    dsn = psycopg_dsn(crawler_database_url)
    with (
        psycopg.connect(dsn, autocommit=True) as first_connection,
        psycopg.connect(dsn, autocommit=True) as second_connection,
    ):
        application_id = _seed_base(first_connection)
        _seed_review(
            first_connection,
            application_id=application_id,
            review_id=8201,
            external_review_id="retry-review",
            observations=[("retry me", 2, 0)],
        )
        first_store = PostgresStore(first_connection)
        second_store = PostgresStore(second_connection)
        assert first_store.try_lock()
        assert not second_store.try_lock()
        first_store.unlock()
        assert second_store.try_lock()
        second_store.unlock()

        failing = FakeAnalyzer(RuntimeError("temporary"))
        SentimentWorker(first_store, failing, max_attempts=2).drain()
        assert first_connection.execute(
            "SELECT sentiment_status, sentiment_attempt_count FROM review_observations "
            "WHERE review_id = 8201"
        ).fetchone() == ("pending", 1)

        succeeding = FakeAnalyzer()
        SentimentWorker(second_store, succeeding, max_attempts=2).drain()
        assert second_connection.execute(
            "SELECT sentiment_status, sentiment_label, sentiment_attempt_count "
            "FROM review_observations WHERE review_id = 8201"
        ).fetchone() == ("done", "positive", 1)
