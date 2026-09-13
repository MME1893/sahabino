from __future__ import annotations

from typing import cast

from sahabino.ingestion.repository import IngestionRepository
from sahabino.messaging.events import EventEnvelope
from sahabino.messaging.network_events import (
    NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE,
    NetworkAnalysisCollectedV1,
)
from sahabino.messaging.playstore_events import (
    APP_STATS_EVENT_TYPE,
    REVIEW_OBSERVED_EVENT_TYPE,
    AppStatsCollectedV1,
    ReviewObservedV1,
)

SupportedEvent = (
    EventEnvelope[AppStatsCollectedV1]
    | EventEnvelope[ReviewObservedV1]
    | EventEnvelope[NetworkAnalysisCollectedV1]
)


def handle_app_stats(
    event: EventEnvelope[AppStatsCollectedV1], repository: IngestionRepository
) -> None:
    payload = event.payload
    repository.validate_crawl_task(
        crawl_task_id=payload.crawl_task_id,
        application_id=payload.application_id,
        expected_task_type="app_details",
    )
    repository.insert_app_snapshot(payload)


def handle_review_observed(
    event: EventEnvelope[ReviewObservedV1], repository: IngestionRepository
) -> None:
    payload = event.payload
    repository.validate_crawl_task(
        crawl_task_id=payload.crawl_task_id,
        application_id=payload.application_id,
        expected_task_type="reviews",
    )
    review_id = repository.upsert_review(payload)
    repository.insert_review_observation(payload, review_id=review_id)


def handle_network_analysis(
    event: EventEnvelope[NetworkAnalysisCollectedV1], repository: IngestionRepository
) -> None:
    repository.validate_network_analysis(event.payload)
    repository.insert_network_analysis(event.payload)


def handle_event(event: SupportedEvent, repository: IngestionRepository) -> None:
    if event.event_type == APP_STATS_EVENT_TYPE:
        handle_app_stats(cast(EventEnvelope[AppStatsCollectedV1], event), repository)
        return
    if event.event_type == REVIEW_OBSERVED_EVENT_TYPE:
        handle_review_observed(cast(EventEnvelope[ReviewObservedV1], event), repository)
        return
    if event.event_type == NETWORK_ANALYSIS_COLLECTED_EVENT_TYPE:
        handle_network_analysis(cast(EventEnvelope[NetworkAnalysisCollectedV1], event), repository)
        return
    raise AssertionError(f"validated event type is not routed: {event.event_type}")
