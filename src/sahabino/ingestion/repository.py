from __future__ import annotations

from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sahabino.crawler.infrastructure.persistence.models import CrawlTask
from sahabino.ingestion.exceptions import IngestionConsistencyError
from sahabino.ingestion.models import (
    IngestedEvent,
    PlaystoreAppSnapshot,
    Review,
    ReviewObservation,
)
from sahabino.messaging.playstore_events import AppStatsCollectedV1, ReviewObservedV1


class IngestionRepository:
    """Concrete PostgreSQL operations for one worker-owned transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim_event(
        self,
        *,
        event_id: UUID,
        event_type: str,
        schema_version: int,
        topic: str,
        partition: int,
        offset: int,
    ) -> bool:
        statement = (
            insert(IngestedEvent)
            .values(
                event_id=event_id,
                event_type=event_type,
                schema_version=schema_version,
                topic=topic,
                partition=partition,
                offset=offset,
            )
            .on_conflict_do_nothing()
            .returning(IngestedEvent.event_id)
        )
        return self._session.execute(statement).scalar_one_or_none() is not None

    def validate_crawl_task(
        self,
        *,
        crawl_task_id: UUID,
        application_id: UUID,
        expected_task_type: str,
    ) -> None:
        task_id = self._session.scalar(
            select(CrawlTask.id).where(
                CrawlTask.id == crawl_task_id,
                CrawlTask.application_id == application_id,
                CrawlTask.task_type == expected_task_type,
            )
        )
        if task_id is None:
            raise IngestionConsistencyError(
                "crawl task is missing or does not match the event application and task type"
            )

    def insert_app_snapshot(self, payload: AppStatsCollectedV1) -> None:
        statement = (
            insert(PlaystoreAppSnapshot)
            .values(
                crawl_task_id=payload.crawl_task_id,
                application_id=payload.application_id,
                package_name=payload.package_name,
                collected_at=payload.collected_at,
                min_installs=payload.min_installs,
                score=payload.score,
                ratings_count=payload.ratings_count,
                reviews_count=payload.reviews_count,
                store_updated_on=payload.store_updated_on,
                version=payload.version,
                ad_supported=payload.ad_supported,
                source_adapter=payload.source_adapter,
            )
            .on_conflict_do_nothing(index_elements=[PlaystoreAppSnapshot.crawl_task_id])
        )
        self._session.execute(statement)

    def upsert_review(self, payload: ReviewObservedV1) -> int:
        insert_statement = insert(Review).values(
            application_id=payload.application_id,
            external_review_id=payload.external_review_id,
            source_at=payload.source_at,
            author_name=payload.author_name,
            thumbs_up_count=payload.thumbs_up_count,
            score=payload.score,
            content=payload.content,
            source_adapter=payload.source_adapter,
            first_observed_at=payload.observed_at,
            last_observed_at=payload.observed_at,
        )
        incoming_is_current = insert_statement.excluded.last_observed_at >= Review.last_observed_at
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=[Review.application_id, Review.external_review_id],
            set_={
                "source_at": case(
                    (incoming_is_current, insert_statement.excluded.source_at),
                    else_=Review.source_at,
                ),
                "author_name": case(
                    (incoming_is_current, insert_statement.excluded.author_name),
                    else_=Review.author_name,
                ),
                "thumbs_up_count": case(
                    (incoming_is_current, insert_statement.excluded.thumbs_up_count),
                    else_=Review.thumbs_up_count,
                ),
                "score": case(
                    (incoming_is_current, insert_statement.excluded.score), else_=Review.score
                ),
                "content": case(
                    (incoming_is_current, insert_statement.excluded.content),
                    else_=Review.content,
                ),
                "source_adapter": case(
                    (incoming_is_current, insert_statement.excluded.source_adapter),
                    else_=Review.source_adapter,
                ),
                "first_observed_at": func.least(
                    Review.first_observed_at, insert_statement.excluded.first_observed_at
                ),
                "last_observed_at": case(
                    (incoming_is_current, insert_statement.excluded.last_observed_at),
                    else_=Review.last_observed_at,
                ),
                "updated_at": func.now(),
            },
        ).returning(Review.id)
        return self._session.execute(upsert_statement).scalar_one()

    def insert_review_observation(self, payload: ReviewObservedV1, *, review_id: int) -> None:
        statement = (
            insert(ReviewObservation)
            .values(
                crawl_task_id=payload.crawl_task_id,
                review_id=review_id,
                observed_at=payload.observed_at,
                position=payload.position,
                score=payload.score,
                thumbs_up_count=payload.thumbs_up_count,
                source_adapter=payload.source_adapter,
            )
            .on_conflict_do_nothing(
                index_elements=[ReviewObservation.crawl_task_id, ReviewObservation.review_id]
            )
        )
        self._session.execute(statement)
