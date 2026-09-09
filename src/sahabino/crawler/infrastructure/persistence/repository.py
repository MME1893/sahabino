from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sahabino.crawler.application.ports.lifecycle_repository import LifecycleTransaction
from sahabino.crawler.domain.dto import ApplicationRef
from sahabino.crawler.domain.results import (
    CrawlRunStatus,
    CrawlTaskStatus,
    CrawlTaskType,
    TriggerType,
)
from sahabino.crawler.infrastructure.persistence.models import CrawlRun, CrawlTask


def _now() -> datetime:
    return datetime.now(UTC)


class SqlAlchemyLifecycleTransaction:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_run(
        self,
        trigger_type: TriggerType,
        *,
        scheduled_for: datetime | None = None,
        crawler_version: str | None = None,
    ) -> UUID:
        run = CrawlRun(
            trigger_type=trigger_type.value,
            status=CrawlRunStatus.RUNNING.value,
            scheduled_for=scheduled_for,
            started_at=_now(),
            crawler_version=crawler_version,
        )
        self._session.add(run)
        self._session.flush()
        return run.id

    def create_tasks(
        self,
        run_id: UUID,
        applications: list[ApplicationRef],
        language_code: str,
        country_code: str,
    ) -> dict[tuple[UUID, CrawlTaskType], UUID]:
        result: dict[tuple[UUID, CrawlTaskType], UUID] = {}
        for application in applications:
            for task_type in CrawlTaskType:
                task = CrawlTask(
                    crawl_run_id=run_id,
                    application_id=application.application_id,
                    task_type=task_type.value,
                    status=CrawlTaskStatus.PENDING.value,
                    language_code=language_code,
                    country_code=country_code,
                )
                self._session.add(task)
                self._session.flush()
                result[(application.application_id, task_type)] = task.id
        return result

    def begin_attempt(self, task_id: UUID) -> None:
        task = self._task(task_id)
        if task.status not in {
            CrawlTaskStatus.PENDING.value,
            CrawlTaskStatus.RETRYING.value,
        }:
            raise ValueError(f"cannot begin an attempt for task in {task.status!r}")
        task.status = CrawlTaskStatus.RUNNING.value
        task.attempt_count += 1
        task.started_at = task.started_at or _now()
        task.error_code = None
        task.error_message = None
        self._session.flush()

    def mark_retrying(self, task_id: UUID) -> None:
        task = self._task(task_id)
        if task.status != CrawlTaskStatus.RUNNING.value:
            raise ValueError(f"cannot retry task in {task.status!r}")
        task.status = CrawlTaskStatus.RETRYING.value
        self._session.flush()

    def mark_task_succeeded(self, task_id: UUID) -> None:
        task = self._task(task_id)
        if task.status != CrawlTaskStatus.RUNNING.value:
            raise ValueError(f"cannot succeed task in {task.status!r}")
        task.status = CrawlTaskStatus.SUCCEEDED.value
        task.finished_at = _now()
        task.error_code = None
        task.error_message = None
        self._session.flush()

    def mark_task_failed(self, task_id: UUID, error_code: str, error_message: str) -> None:
        task = self._task(task_id)
        if task.status not in {
            CrawlTaskStatus.PENDING.value,
            CrawlTaskStatus.RUNNING.value,
            CrawlTaskStatus.RETRYING.value,
        }:
            raise ValueError(f"cannot fail task in {task.status!r}")
        task.status = CrawlTaskStatus.FAILED.value
        task.finished_at = _now()
        task.error_code = error_code
        task.error_message = error_message[:500]
        self._session.flush()

    def finish_run(self, run_id: UUID, forced_status: CrawlRunStatus | None = None) -> None:
        run = self._session.get(CrawlRun, run_id)
        if run is None:
            raise LookupError(f"crawl run {run_id} was not found")
        if forced_status is None:
            statuses = self.task_statuses(run_id)
            succeeded = statuses.count(CrawlTaskStatus.SUCCEEDED)
            failed = statuses.count(CrawlTaskStatus.FAILED)
            if not statuses or succeeded == len(statuses):
                forced_status = CrawlRunStatus.SUCCEEDED
            elif failed == len(statuses):
                forced_status = CrawlRunStatus.FAILED
            else:
                forced_status = CrawlRunStatus.PARTIALLY_FAILED
        run.status = forced_status.value
        run.finished_at = _now()
        self._session.flush()

    def task_statuses(self, run_id: UUID) -> list[CrawlTaskStatus]:
        statuses = self._session.scalars(
            select(CrawlTask.status).where(CrawlTask.crawl_run_id == run_id)
        ).all()
        return [CrawlTaskStatus(value) for value in statuses]

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def _task(self, task_id: UUID) -> CrawlTask:
        task = self._session.get(CrawlTask, task_id)
        if task is None:
            raise LookupError(f"crawl task {task_id} was not found")
        return task


class SqlAlchemyLifecycleRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @contextmanager
    def transaction(self) -> Generator[LifecycleTransaction]:
        with self._session_factory() as session:
            transaction = SqlAlchemyLifecycleTransaction(session)
            try:
                yield transaction
            except BaseException:
                transaction.rollback()
                raise
