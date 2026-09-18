from __future__ import annotations

import logging
from uuid import UUID

from sahabino.crawler.application.ports.publisher import CollectedEventPublisher
from sahabino.crawler.domain.dto import AppDetailsDTO, ApplicationRef, ReviewsDTO
from sahabino.crawler.domain.errors import MessagingPublishFailure
from sahabino.messaging.exceptions import ProducerError
from sahabino.messaging.playstore_events import (
    AppStatsCollectedV1,
    ReviewObservedV1,
    app_stats_envelope,
    review_observed_envelope,
)
from sahabino.messaging.producer import KafkaBatchMessage, KafkaProducer
from sahabino.messaging.topics import (
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
)

logger = logging.getLogger(__name__)


class KafkaCollectedEventPublisher(CollectedEventPublisher):
    def __init__(self, producer: KafkaProducer) -> None:
        self._producer = producer

    def publish_app_stats(
        self,
        *,
        crawl_task_id: UUID,
        application: ApplicationRef,
        details: AppDetailsDTO,
    ) -> None:
        payload = AppStatsCollectedV1(
            crawl_task_id=crawl_task_id,
            application_id=application.application_id,
            package_name=application.package_name,
            **details.model_dump(),
        )
        message = KafkaBatchMessage(
            topic=PLAYSTORE_APP_STATS_TOPIC,
            key=str(application.application_id),
            event=app_stats_envelope(payload),
        )
        self._publish((message,))

    def publish_reviews(
        self,
        *,
        crawl_task_id: UUID,
        application: ApplicationRef,
        reviews: ReviewsDTO,
    ) -> None:
        messages = tuple(
            KafkaBatchMessage(
                topic=PLAYSTORE_REVIEW_OBSERVED_TOPIC,
                key=str(application.application_id),
                event=review_observed_envelope(
                    ReviewObservedV1(
                        crawl_task_id=crawl_task_id,
                        application_id=application.application_id,
                        package_name=application.package_name,
                        **review.model_dump(),
                    )
                ),
            )
            for review in reviews.reviews
        )
        self._publish(messages)
        logger.info(
            "review batch published",
            extra={
                "event": "crawler.review.batch_published",
                "crawl_task_id": crawl_task_id,
                "application_id": application.application_id,
                "package_name": application.package_name,
                "record_count": len(messages),
                "topic": PLAYSTORE_REVIEW_OBSERVED_TOPIC,
            },
        )

    def close(self) -> None:
        self._producer.close()

    def _publish(self, messages: tuple[KafkaBatchMessage, ...]) -> None:
        try:
            self._producer.publish_batch(messages)
        except ProducerError as error:
            raise MessagingPublishFailure("Kafka delivery boundary failed") from error
