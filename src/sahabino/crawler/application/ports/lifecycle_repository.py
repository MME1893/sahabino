from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sahabino.crawler.domain.dto import ApplicationRef
from sahabino.crawler.domain.results import (
    CrawlRunStatus,
    CrawlTaskStatus,
    CrawlTaskType,
    TriggerType,
)


class LifecycleTransaction(Protocol):
    def create_run(
        self,
        trigger_type: TriggerType,
        *,
        scheduled_for: datetime | None = None,
        crawler_version: str | None = None,
    ) -> UUID: ...

    def create_tasks(
        self,
        run_id: UUID,
        applications: list[ApplicationRef],
        language_code: str,
        country_code: str,
    ) -> dict[tuple[UUID, CrawlTaskType], UUID]: ...

    def begin_attempt(self, task_id: UUID) -> None: ...

    def mark_retrying(self, task_id: UUID) -> None: ...

    def mark_task_succeeded(self, task_id: UUID) -> None: ...

    def mark_task_failed(self, task_id: UUID, error_code: str, error_message: str) -> None: ...

    def finish_run(self, run_id: UUID, forced_status: CrawlRunStatus | None = None) -> None: ...

    def task_statuses(self, run_id: UUID) -> list[CrawlTaskStatus]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class LifecycleRepository(Protocol):
    def transaction(self) -> AbstractContextManager[LifecycleTransaction]: ...


LifecycleTransactionIterator = Iterator[LifecycleTransaction]
