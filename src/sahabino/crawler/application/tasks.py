from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sahabino.crawler.application.client import (
    ApplicationPlayStoreClient,
    OperationHooks,
    ResilientPlayStoreClient,
)
from sahabino.crawler.application.ports.lifecycle_repository import LifecycleRepository
from sahabino.crawler.application.ports.publisher import CollectedEventPublisher
from sahabino.crawler.domain.dto import ApplicationRef
from sahabino.crawler.domain.errors import CrawlerError, safe_error_message
from sahabino.crawler.domain.results import CrawlTaskType


class ApplicationCrawlCommand:
    """One application-level concurrency command with independent persisted tasks."""

    def __init__(
        self,
        *,
        playstore: ResilientPlayStoreClient,
        lifecycle: LifecycleRepository,
        publisher: CollectedEventPublisher,
        language_code: str,
        country_code: str,
        review_limit: int = 100,
    ) -> None:
        self._playstore = playstore
        self._lifecycle = lifecycle
        self._publisher = publisher
        self._language = language_code
        self._country = country_code
        self._review_limit = review_limit

    def execute(
        self,
        application: ApplicationRef,
        task_ids: dict[CrawlTaskType, UUID],
    ) -> None:
        try:
            with self._playstore.open_application(str(application.application_id)) as client:
                self._run_details(client, application, task_ids[CrawlTaskType.APP_DETAILS])

                self._run_reviews(client, application, task_ids[CrawlTaskType.REVIEWS])

        except CrawlerError as error:
            finalization_errors = self._fail_open_tasks(task_ids.values(), error)

            if finalization_errors:
                raise ExceptionGroup(
                    "crawler failure and task finalization failure",
                    [error, *finalization_errors],
                ) from error

        except Exception as error:
            finalization_errors = self._fail_open_tasks(task_ids.values(), error)
            if finalization_errors:
                raise ExceptionGroup(
                    "application command and task finalization failed",
                    [error, *finalization_errors],
                ) from error
            raise

    def _run_details(
        self,
        client: ApplicationPlayStoreClient,
        application: ApplicationRef,
        task_id: UUID,
    ) -> None:
        try:
            details = client.get_app(
                application.package_name,
                self._language,
                self._country,
                hooks=self._hooks(task_id),
            )
            self._publisher.publish_app_stats(
                crawl_task_id=task_id,
                application=application,
                details=details,
            )
        except CrawlerError as error:
            self._mark_expected_failure(task_id, error)
        except Exception as error:
            self._mark_unexpected_failure(task_id, error)
            raise
        else:
            self._mark_succeeded(task_id)

    def _run_reviews(
        self,
        client: ApplicationPlayStoreClient,
        application: ApplicationRef,
        task_id: UUID,
    ) -> None:
        try:
            reviews = client.get_reviews(
                application.package_name,
                self._language,
                self._country,
                self._review_limit,
                hooks=self._hooks(task_id),
            )
            self._publisher.publish_reviews(
                crawl_task_id=task_id,
                application=application,
                reviews=reviews,
            )
        except CrawlerError as error:
            self._mark_expected_failure(task_id, error)
        except Exception as error:
            self._mark_unexpected_failure(task_id, error)
            raise
        else:
            self._mark_succeeded(task_id)

    def _hooks(self, task_id: UUID) -> OperationHooks:
        return OperationHooks(
            before_attempt=lambda _attempt: self._begin_attempt(task_id),
            before_retry=lambda _error: self._mark_retrying(task_id),
        )

    def _begin_attempt(self, task_id: UUID) -> None:
        with self._lifecycle.transaction() as transaction:
            transaction.begin_attempt(task_id)
            transaction.commit()

    def _mark_retrying(self, task_id: UUID) -> None:
        with self._lifecycle.transaction() as transaction:
            transaction.mark_retrying(task_id)
            transaction.commit()

    def _mark_succeeded(self, task_id: UUID) -> None:
        with self._lifecycle.transaction() as transaction:
            transaction.mark_task_succeeded(task_id)
            transaction.commit()

    def _mark_failed(self, task_id: UUID, error: BaseException) -> None:
        code = error.code if isinstance(error, CrawlerError) else CrawlerError.code
        with self._lifecycle.transaction() as transaction:
            transaction.mark_task_failed(task_id, code, safe_error_message(error))
            transaction.commit()

    def _mark_expected_failure(self, task_id: UUID, error: CrawlerError) -> None:
        try:
            self._mark_failed(task_id, error)
        except Exception as finalization_error:
            raise ExceptionGroup(
                "crawler failure and task finalization failed",
                [error, finalization_error],
            ) from error

    def _mark_unexpected_failure(self, task_id: UUID, error: Exception) -> None:
        try:
            self._mark_failed(task_id, error)
        except Exception as finalization_error:
            raise ExceptionGroup(
                "application failure and task finalization failed",
                [error, finalization_error],
            ) from error

    def _fail_open_tasks(
        self,
        task_ids: Iterable[UUID],
        error: BaseException,
    ) -> list[Exception]:
        finalization_errors: list[Exception] = []
        for task_id in task_ids:
            try:
                self._mark_failed(task_id, error)
            except ValueError:
                continue
            except Exception as finalization_error:
                finalization_errors.append(finalization_error)
        return finalization_errors
