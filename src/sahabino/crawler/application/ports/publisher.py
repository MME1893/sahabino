from typing import Protocol
from uuid import UUID

from sahabino.crawler.domain.dto import AppDetailsDTO, ApplicationRef, ReviewsDTO


class CollectedEventPublisher(Protocol):
    def publish_app_stats(
        self,
        *,
        crawl_task_id: UUID,
        application: ApplicationRef,
        details: AppDetailsDTO,
    ) -> None: ...

    def publish_reviews(
        self,
        *,
        crawl_task_id: UUID,
        application: ApplicationRef,
        reviews: ReviewsDTO,
    ) -> None: ...

    def close(self) -> None: ...
