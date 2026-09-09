from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Protocol, cast
from uuid import UUID

from sahabino.crawler.application.executor import CrawlerService
from sahabino.crawler.domain.results import TriggerType


class SchedulerPort(Protocol):
    def add_job(self, function: Callable[[], object], trigger: str, **kwargs: Any) -> object: ...

    def start(self) -> None: ...


def _blocking_scheduler() -> SchedulerPort:
    from apscheduler.schedulers.blocking import BlockingScheduler

    return cast(SchedulerPort, BlockingScheduler(timezone="UTC"))


class CrawlerScheduler:
    def __init__(
        self,
        crawler: CrawlerService,
        *,
        interval_minutes: int,
        scheduler_factory: Callable[[], SchedulerPort] = _blocking_scheduler,
    ) -> None:
        if interval_minutes < 1:
            raise ValueError("crawl interval must be at least one minute")
        self._crawler = crawler
        self._interval = interval_minutes
        self._scheduler_factory = scheduler_factory
        self._run_lock = Lock()

    def run_scheduled_once(self) -> UUID | None:
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            return self._crawler.crawl_once(
                TriggerType.SCHEDULED,
                scheduled_for=datetime.now(UTC),
            )
        finally:
            self._run_lock.release()

    def start(self) -> None:
        scheduler = self._scheduler_factory()
        scheduler.add_job(
            self.run_scheduled_once,
            "interval",
            minutes=self._interval,
            id="playstore-hourly-crawl",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(UTC),
        )
        scheduler.start()
