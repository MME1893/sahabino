from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from sentiment.contracts import AnalysisResult
from sentiment.worker import (
    PendingObservation,
    SentimentWorker,
    WorkerAlreadyRunning,
    run_locked,
)


@dataclass
class MemoryObservation:
    review_id: int
    crawl_task_id: UUID
    content: str | None
    score: int = 5
    status: str = "pending"
    language: str | None = None
    label: str | None = None
    attempts: int = 0


class MemoryStore:
    def __init__(self, rows: list[MemoryObservation], *, lock_available: bool = True) -> None:
        self.rows = rows
        self.lock_available = lock_available
        self.unlock_calls = 0

    def try_lock(self) -> bool:
        return self.lock_available

    def unlock(self) -> None:
        self.unlock_calls += 1

    def count_pending(self) -> int:
        return sum(row.status == "pending" for row in self.rows)

    def fetch_pending_after(
        self, cursor: tuple[int, UUID] | None, limit: int
    ) -> list[PendingObservation]:
        rows = sorted(
            (row for row in self.rows if row.status == "pending"),
            key=lambda row: (row.review_id, row.crawl_task_id),
        )
        if cursor is not None:
            rows = [row for row in rows if (row.review_id, row.crawl_task_id) > cursor]
        return [
            PendingObservation(row.review_id, row.crawl_task_id, row.content)
            for row in rows[:limit]
        ]

    def find_reusable(self, identity: tuple[int, str | None]) -> AnalysisResult | None:
        reusable = [
            row
            for row in self.rows
            if (row.review_id, row.content) == identity and row.status in {"done", "skipped"}
        ]
        reusable.sort(key=lambda row: row.status != "done")
        if not reusable:
            return None
        row = reusable[0]
        return AnalysisResult(language=row.language, label=row.label, status=row.status)  # type: ignore[arg-type]

    def apply_result(self, identity: tuple[int, str | None], result: AnalysisResult) -> int:
        updated = 0
        for row in self.rows:
            if row.status == "pending" and (row.review_id, row.content) == identity:
                row.status = result.status
                row.language = result.language
                row.label = result.label
                updated += 1
        return updated

    def record_failure(
        self, identity: tuple[int, str | None], max_attempts: int
    ) -> tuple[int, int]:
        pending = failed = 0
        for row in self.rows:
            if row.status != "pending" or (row.review_id, row.content) != identity:
                continue
            row.attempts += 1
            if row.attempts >= max_attempts:
                row.status = "failed"
                failed += 1
            else:
                pending += 1
        return pending, failed


class FakeAnalyzer:
    def __init__(
        self,
        outcomes: dict[str | None, AnalysisResult | Exception] | None = None,
        *,
        crash: bool = False,
    ) -> None:
        self.outcomes = outcomes or {}
        self.crash = crash
        self.calls: list[list[str | None]] = []

    def analyze_batch(self, contents: list[str | None]) -> list[AnalysisResult | Exception]:
        self.calls.append(list(contents))
        if self.crash:
            raise KeyboardInterrupt
        default = AnalysisResult(language="en", label="positive", status="done")
        return [self.outcomes.get(content, default) for content in contents]


def _row(review_id: int, content: str | None, **values: object) -> MemoryObservation:
    return MemoryObservation(review_id, uuid4(), content, **values)  # type: ignore[arg-type]


def test_identical_pending_rows_are_inferred_once_and_all_completed() -> None:
    store = MemoryStore([_row(1, "same"), _row(1, "same"), _row(1, "same")])
    analyzer = FakeAnalyzer()

    stats = SentimentWorker(store, analyzer, batch_size=2).drain()

    assert analyzer.calls == [["same"]]
    assert [row.status for row in store.rows] == ["done", "done", "done"]
    assert stats.newly_analyzed_texts == 1
    assert stats.done_observations == 3


def test_reuse_is_scoped_to_review_and_exact_content_not_rating() -> None:
    store = MemoryStore(
        [
            _row(1, "same", status="done", language="en", label="positive", score=5),
            _row(1, "same", score=1),
            _row(1, "changed", score=5),
            _row(2, "same", score=5),
        ]
    )
    analyzer = FakeAnalyzer()

    stats = SentimentWorker(store, analyzer).drain()

    assert analyzer.calls == [["changed", "same"]]
    assert store.rows[1].status == "done"
    assert store.rows[1].label == "positive"
    assert stats.reused_results == 1
    assert stats.newly_analyzed_texts == 2


def test_deterministic_skip_is_reused() -> None:
    store = MemoryStore(
        [
            _row(1, "🙂", status="skipped"),
            _row(1, "🙂"),
        ]
    )
    analyzer = FakeAnalyzer()

    stats = SentimentWorker(store, analyzer).drain()

    assert analyzer.calls == []
    assert store.rows[1].status == "skipped"
    assert stats.reused_results == 1


def test_returning_from_changed_content_to_old_content_reuses_old_success() -> None:
    store = MemoryStore(
        [
            _row(1, "A", status="done", language="en", label="positive"),
            _row(1, "B", status="done", language="en", label="negative"),
            _row(1, "A"),
        ]
    )
    analyzer = FakeAnalyzer()

    SentimentWorker(store, analyzer).drain()

    assert analyzer.calls == []
    assert store.rows[2].label == "positive"


def test_failed_result_is_never_reused() -> None:
    store = MemoryStore([_row(1, "retry", status="failed", attempts=3), _row(1, "retry")])
    analyzer = FakeAnalyzer()

    SentimentWorker(store, analyzer).drain()

    assert analyzer.calls == [["retry"]]
    assert store.rows[0].status == "failed"
    assert store.rows[1].status == "done"


def test_failure_is_attempted_once_per_run_then_becomes_failed() -> None:
    row = _row(1, "temporary")
    store = MemoryStore([row])
    analyzer = FakeAnalyzer({"temporary": RuntimeError("temporary")})
    worker = SentimentWorker(store, analyzer, max_attempts=3)

    first = worker.drain()
    second = worker.drain()
    third = worker.drain()

    assert row.attempts == 3
    assert row.status == "failed"
    assert first.retry_pending_observations == 1
    assert second.retry_pending_observations == 1
    assert third.failed_observations == 1
    assert analyzer.calls == [["temporary"], ["temporary"], ["temporary"]]


def test_crash_leaves_pending_and_restart_completes_idempotently() -> None:
    row = _row(1, "restart")
    store = MemoryStore([row])

    with pytest.raises(KeyboardInterrupt):
        SentimentWorker(store, FakeAnalyzer(crash=True)).drain()

    assert row.status == "pending"
    assert row.attempts == 0

    SentimentWorker(store, FakeAnalyzer()).drain()
    assert row.status == "done"


def test_advisory_lock_prevents_a_concurrent_worker() -> None:
    store = MemoryStore([_row(1, "waiting")], lock_available=False)
    analyzer = FakeAnalyzer()

    with pytest.raises(WorkerAlreadyRunning):
        run_locked(
            store,
            analyzer,
            watch=False,
            idle_seconds=900,
            batch_size=32,
            max_attempts=3,
        )

    assert analyzer.calls == []
    assert store.unlock_calls == 0
