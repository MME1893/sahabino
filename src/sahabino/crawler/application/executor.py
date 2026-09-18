from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from datetime import datetime
from time import monotonic
from uuid import UUID

from sahabino.crawler.application.ports.lifecycle_repository import LifecycleRepository
from sahabino.crawler.application.ports.registry import ApplicationRegistryPort
from sahabino.crawler.application.tasks import ApplicationCrawlCommand
from sahabino.crawler.domain.dto import ApplicationRef
from sahabino.crawler.domain.results import CrawlRunStatus, CrawlTaskType, TriggerType

logger = logging.getLogger(__name__)


class CrawlerService:
    def __init__(
        self,
        *,
        registry: ApplicationRegistryPort,
        lifecycle: LifecycleRepository,
        application_command: ApplicationCrawlCommand,
        max_concurrent_apps: int,
        language_code: str,
        country_code: str,
        crawler_version: str | None = None,
        executor_factory: Callable[..., Executor] = ThreadPoolExecutor,
    ) -> None:
        if max_concurrent_apps < 1:
            raise ValueError("maximum concurrent applications must be at least one")
        self._registry = registry
        self._lifecycle = lifecycle
        self._command = application_command
        self._maximum_workers = max_concurrent_apps
        self._language = language_code
        self._country = country_code
        self._crawler_version = crawler_version
        self._executor_factory = executor_factory

    def crawl_once(
        self,
        trigger_type: TriggerType,
        *,
        scheduled_for: datetime | None = None,
    ) -> UUID:
        started_at = monotonic()
        run_id = self._create_run(trigger_type, scheduled_for)
        run_context = {
            "crawl_run_id": run_id,
            "trigger_type": trigger_type.value,
        }
        if scheduled_for is not None:
            run_context["scheduled_for"] = scheduled_for.isoformat()
        logger.info(
            "crawl run started",
            extra={"event": "crawler.run.started", **run_context},
        )
        try:
            applications = self._registry.list_active_applications()
        except Exception as error:
            logger.exception(
                "crawl run failed while loading applications",
                extra={
                    "event": "crawler.run.failed",
                    "duration_seconds": monotonic() - started_at,
                    **run_context,
                },
            )
            self._finish_failed_preserving(run_id, error)
            # here intentionally we just return id
            # because each task execute independently
            return run_id

        try:
            task_ids = self._create_tasks(run_id, applications)
            with self._executor_factory(
                max_workers=self._maximum_workers,
                thread_name_prefix="playstore-app",
            ) as executor:
                # futures = [
                #     executor.submit(
                #         self._command.execute,
                #         application,
                #         {
                #             task_type: task_ids[(application.application_id, task_type)]
                #             for task_type in CrawlTaskType
                #         },
                #     )
                #     for application in applications
                # ]
                futures = []
                for application in applications:
                    tasks_for_app = {}

                    for task_type in CrawlTaskType:
                        key = (
                            application.application_id,
                            task_type,
                        )

                        tasks_for_app[task_type] = task_ids[key]

                    future = executor.submit(
                        self._command.execute,
                        application,
                        tasks_for_app,
                    )

                    futures.append(future)

                for future in futures:
                    future.result()
            self._finish_run(run_id)
            logger.info(
                "crawl run completed",
                extra={
                    "event": "crawler.run.completed",
                    "application_count": len(applications),
                    "duration_seconds": monotonic() - started_at,
                    **run_context,
                },
            )

        except Exception as error:
            logger.exception(
                "crawl run failed",
                extra={
                    "event": "crawler.run.failed",
                    "duration_seconds": monotonic() - started_at,
                    **run_context,
                },
            )
            self._finish_failed_preserving(run_id, error)

            # just returning same exception
            raise

        return run_id

    def _create_run(self, trigger_type: TriggerType, scheduled_for: datetime | None) -> UUID:

        with self._lifecycle.transaction() as transaction:
            run_id = transaction.create_run(
                trigger_type,
                scheduled_for=scheduled_for,
                crawler_version=self._crawler_version,
            )

            transaction.commit()

            return run_id

    def _create_tasks(
        self, run_id: UUID, applications: list[ApplicationRef]
    ) -> dict[tuple[UUID, CrawlTaskType], UUID]:

        with self._lifecycle.transaction() as transaction:
            task_ids = transaction.create_tasks(
                run_id,
                applications,
                self._language,
                self._country,
            )

            transaction.commit()

            return task_ids

    def _finish_run(self, run_id: UUID, forced_status: CrawlRunStatus | None = None) -> None:

        with self._lifecycle.transaction() as transaction:
            transaction.finish_run(run_id, forced_status)
            transaction.commit()

    def _finish_failed_preserving(self, run_id: UUID, error: Exception) -> None:
        try:
            self._finish_run(run_id, CrawlRunStatus.FAILED)
        except Exception as finalization_error:
            # we may keep all error caused by DB, registry and ...
            raise ExceptionGroup(
                "crawl orchestration and run finalization failed",
                [error, finalization_error],
            ) from error
