from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg import Connection

from .contracts import AnalysisResult, SentimentAnalyzer

ADVISORY_LOCK_ID = 7_249_192_026_091_701


class WorkerAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True)
class PendingObservation:
    review_id: int
    crawl_task_id: UUID
    content: str | None

    @property
    def identity(self) -> tuple[int, str | None]:
        return (self.review_id, self.content)

    @property
    def cursor(self) -> tuple[int, UUID]:
        return (self.review_id, self.crawl_task_id)


@dataclass
class RunStats:
    pending_observations: int = 0
    reused_results: int = 0
    newly_analyzed_texts: int = 0
    done_observations: int = 0
    skipped_observations: int = 0
    failed_observations: int = 0
    retry_pending_observations: int = 0
    duration_seconds: float = 0.0


class SentimentStore(Protocol):
    def try_lock(self) -> bool: ...

    def unlock(self) -> None: ...

    def count_pending(self) -> int: ...

    def fetch_pending_after(
        self, cursor: tuple[int, UUID] | None, limit: int
    ) -> Sequence[PendingObservation]: ...

    def find_reusable(self, identity: tuple[int, str | None]) -> AnalysisResult | None: ...

    def apply_result(self, identity: tuple[int, str | None], result: AnalysisResult) -> int: ...

    def record_failure(
        self, identity: tuple[int, str | None], max_attempts: int
    ) -> tuple[int, int]: ...


class PostgresStore:
    def __init__(self, connection: Connection[Any]) -> None:
        self._connection = connection
        self._locked = False

    def try_lock(self) -> bool:
        row = self._connection.execute(
            "SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_ID,)
        ).fetchone()
        self._locked = bool(row and row[0])
        return self._locked

    def unlock(self) -> None:
        if self._locked:
            self._connection.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))
            self._locked = False

    def count_pending(self) -> int:
        row = self._connection.execute(
            "SELECT count(*) FROM review_observations WHERE sentiment_status = 'pending'"
        ).fetchone()
        return int(row[0]) if row else 0

    def fetch_pending_after(
        self, cursor: tuple[int, UUID] | None, limit: int
    ) -> list[PendingObservation]:
        parameters: tuple[object, ...]
        cursor_clause = ""
        if cursor is None:
            parameters = (limit,)
        else:
            cursor_clause = "AND (review_id, crawl_task_id) > (%s, %s)"
            parameters = (cursor[0], cursor[1], limit)
        rows = self._connection.execute(
            f"""
            SELECT review_id, crawl_task_id, content
            FROM review_observations
            WHERE sentiment_status = 'pending'
              {cursor_clause}
            ORDER BY review_id, crawl_task_id
            LIMIT %s
            """,
            parameters,
        ).fetchall()
        return [
            PendingObservation(review_id=row[0], crawl_task_id=row[1], content=row[2])
            for row in rows
        ]

    def find_reusable(self, identity: tuple[int, str | None]) -> AnalysisResult | None:
        row = self._connection.execute(
            """
            SELECT sentiment_language, sentiment_label, sentiment_status
            FROM review_observations
            WHERE review_id = %s
              AND content IS NOT DISTINCT FROM %s
              AND (
                  (sentiment_status = 'done'
                   AND sentiment_language IN ('fa', 'en')
                   AND sentiment_label IN ('positive', 'neutral', 'negative'))
                  OR (sentiment_status = 'skipped' AND sentiment_label IS NULL)
              )
            ORDER BY CASE WHEN sentiment_status = 'done' THEN 0 ELSE 1 END,
                     sentiment_processed_at DESC NULLS LAST
            LIMIT 1
            """,
            identity,
        ).fetchone()
        if row is None:
            return None
        return AnalysisResult(language=row[0], label=row[1], status=row[2])

    def apply_result(self, identity: tuple[int, str | None], result: AnalysisResult) -> int:
        cursor = self._connection.execute(
            """
            UPDATE review_observations
            SET sentiment_language = %s,
                sentiment_label = %s,
                sentiment_status = %s,
                sentiment_processed_at = now()
            WHERE sentiment_status = 'pending'
              AND review_id = %s
              AND content IS NOT DISTINCT FROM %s
            """,
            (result.language, result.label, result.status, identity[0], identity[1]),
        )
        return cursor.rowcount

    def record_failure(
        self, identity: tuple[int, str | None], max_attempts: int
    ) -> tuple[int, int]:
        rows = self._connection.execute(
            """
            UPDATE review_observations
            SET sentiment_attempt_count = sentiment_attempt_count + 1,
                sentiment_language = NULL,
                sentiment_label = NULL,
                sentiment_status = CASE
                    WHEN sentiment_attempt_count + 1 >= %s THEN 'failed'
                    ELSE 'pending'
                END,
                sentiment_processed_at = CASE
                    WHEN sentiment_attempt_count + 1 >= %s THEN now()
                    ELSE NULL
                END
            WHERE sentiment_status = 'pending'
              AND review_id = %s
              AND content IS NOT DISTINCT FROM %s
            RETURNING sentiment_status
            """,
            (max_attempts, max_attempts, identity[0], identity[1]),
        ).fetchall()
        pending = sum(row[0] == "pending" for row in rows)
        failed = sum(row[0] == "failed" for row in rows)
        return pending, failed


class SentimentWorker:
    def __init__(
        self,
        store: SentimentStore,
        analyzer: SentimentAnalyzer,
        *,
        batch_size: int = 32,
        max_attempts: int = 3,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._store = store
        self._analyzer = analyzer
        self._batch_size = batch_size
        self._max_attempts = max_attempts

    def drain(self) -> RunStats:
        started = time.monotonic()
        stats = RunStats(pending_observations=self._store.count_pending())
        cursor: tuple[int, UUID] | None = None
        attempted: set[tuple[int, str | None]] = set()

        while True:
            rows = list(self._store.fetch_pending_after(cursor, self._batch_size))
            if not rows:
                break
            cursor = rows[-1].cursor
            groups: OrderedDict[tuple[int, str | None], None] = OrderedDict()
            for row in rows:
                if row.identity not in attempted:
                    groups[row.identity] = None
                    attempted.add(row.identity)

            needs_analysis: list[tuple[int, str | None]] = []
            for identity in groups:
                reusable = self._store.find_reusable(identity)
                if reusable is None:
                    needs_analysis.append(identity)
                    continue
                updated = self._store.apply_result(identity, reusable)
                stats.reused_results += updated
                if reusable.status == "done":
                    stats.done_observations += updated
                else:
                    stats.skipped_observations += updated

            if not needs_analysis:
                continue
            stats.newly_analyzed_texts += len(needs_analysis)
            try:
                outcomes = list(
                    self._analyzer.analyze_batch([identity[1] for identity in needs_analysis])
                )
                if len(outcomes) != len(needs_analysis):
                    raise RuntimeError("inference returned an unexpected batch size")
            except Exception as error:
                outcomes = [error] * len(needs_analysis)

            for identity, outcome in zip(needs_analysis, outcomes, strict=True):
                if isinstance(outcome, AnalysisResult):
                    updated = self._store.apply_result(identity, outcome)
                    if outcome.status == "done":
                        stats.done_observations += updated
                    else:
                        stats.skipped_observations += updated
                    continue
                pending, failed = self._store.record_failure(identity, self._max_attempts)
                stats.retry_pending_observations += pending
                stats.failed_observations += failed

        stats.duration_seconds = round(time.monotonic() - started, 3)
        return stats


def _logger() -> logging.Logger:
    logger = logging.getLogger("sentiment-worker")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return logger


def _log(logger: logging.Logger, event: str, **values: object) -> None:
    logger.info(json.dumps({"event": event, **values}, separators=(",", ":"), sort_keys=True))


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process review sentiment observations")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="drain current pending work and exit")
    mode.add_argument("--watch", action="store_true", help="continue polling after draining work")
    parser.add_argument(
        "--idle-seconds",
        type=_positive_integer,
        default=int(os.getenv("SENTIMENT_IDLE_SECONDS", "900")),
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_integer,
        default=int(os.getenv("SENTIMENT_BATCH_SIZE", "32")),
    )
    parser.add_argument(
        "--max-attempts",
        type=_positive_integer,
        default=int(os.getenv("SENTIMENT_MAX_ATTEMPTS", "3")),
    )
    return parser.parse_args(argv)


def run_locked(
    store: SentimentStore,
    analyzer: SentimentAnalyzer,
    *,
    watch: bool,
    idle_seconds: int,
    batch_size: int,
    max_attempts: int,
    sleep: Any = time.sleep,
    logger: logging.Logger | None = None,
) -> list[RunStats]:
    if not store.try_lock():
        raise WorkerAlreadyRunning("another sentiment worker holds the PostgreSQL advisory lock")
    active_logger = logger or _logger()
    runs: list[RunStats] = []
    try:
        while True:
            stats = SentimentWorker(
                store,
                analyzer,
                batch_size=batch_size,
                max_attempts=max_attempts,
            ).drain()
            runs.append(stats)
            _log(active_logger, "run_complete", **asdict(stats))
            if not watch:
                return runs
            _log(active_logger, "idle", sleep_seconds=idle_seconds)
            sleep(idle_seconds)
    finally:
        store.unlock()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logger = _logger()
    dsn = os.getenv("SENTIMENT_DATABASE_URL", "")
    try:
        with psycopg.connect(dsn, autocommit=True) as connection:
            store = PostgresStore(connection)
            if not store.try_lock():
                raise WorkerAlreadyRunning(
                    "another sentiment worker holds the PostgreSQL advisory lock"
                )
            try:
                from .inference import build_inference_from_environment

                analyzer = build_inference_from_environment()
                while True:
                    stats = SentimentWorker(
                        store,
                        analyzer,
                        batch_size=args.batch_size,
                        max_attempts=args.max_attempts,
                    ).drain()
                    _log(logger, "run_complete", **asdict(stats))
                    if not args.watch:
                        return 0
                    _log(logger, "idle", sleep_seconds=args.idle_seconds)
                    time.sleep(args.idle_seconds)
            finally:
                store.unlock()
    except WorkerAlreadyRunning:
        _log(logger, "worker_not_started", reason="advisory_lock_unavailable")
        return 2
    except Exception as error:
        safe_message = str(error) if error.__class__.__name__ == "ModelUnavailableError" else None
        _log(
            logger,
            "worker_failed",
            error_type=error.__class__.__name__,
            **({"message": safe_message} if safe_message else {}),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
