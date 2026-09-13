from __future__ import annotations

from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sahabino.app_registry.models import Application
from sahabino.crawler.infrastructure.persistence.models import CrawlTask
from sahabino.ingestion.exceptions import IngestionConsistencyError
from sahabino.ingestion.models import (
    IngestedEvent,
    PlaystoreAppSnapshot,
    Review,
    ReviewObservation,
)
from sahabino.messaging.network_events import NetworkAnalysisCollectedV1
from sahabino.messaging.playstore_events import AppStatsCollectedV1, ReviewObservedV1
from sahabino.network.models import NetworkAnalysisResult, NetworkCapture


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

    def validate_network_analysis(self, payload: NetworkAnalysisCollectedV1) -> None:
        row = self._session.execute(
            select(NetworkCapture, Application.package_name)
            .join(Application, Application.id == NetworkCapture.application_id)
            .where(NetworkCapture.id == payload.capture_id)
        ).one_or_none()
        if row is None:
            raise IngestionConsistencyError("network capture does not exist")
        capture, package_name = row
        if (
            capture.analysis_id != payload.analysis_id
            or capture.application_id != payload.application_id
            or package_name != payload.package_name
            or capture.scenario != payload.scenario
            or capture.verified_sha256 != payload.verified_sha256
        ):
            raise IngestionConsistencyError(
                "network analysis does not match capture and application state"
            )

    def insert_network_analysis(self, payload: NetworkAnalysisCollectedV1) -> None:
        values: dict[str, object] = {
            "analysis_id": payload.analysis_id,
            "capture_id": payload.capture_id,
            "application_id": payload.application_id,
            "package_name": payload.package_name,
            "scenario": payload.scenario,
            "analyzed_at": payload.analyzed_at,
            "analyzer_version": payload.analyzer_version,
            "tshark_version": payload.tshark_version,
            "source_analyzer": payload.source_analyzer,
            "verified_sha256": payload.verified_sha256,
            **payload.capture.model_dump(exclude={"analysis_warnings"}),
            **payload.capabilities.model_dump(),
            **payload.traffic.model_dump(),
            **payload.protocol_mix.model_dump(),
            **payload.ip.model_dump(),
            **payload.flow.model_dump(),
            **payload.transfer.model_dump(),
            "analysis_warnings": list(payload.capture.analysis_warnings),
        }
        for model_name in ("tcp", "quic", "udp", "dns"):
            model = getattr(payload, model_name)
            if model is not None:
                model_values = model.model_dump()
                if model_name == "quic":
                    model_values["quic_versions_seen"] = (
                        list(model.quic_versions_seen)
                        if model.quic_versions_seen is not None
                        else None
                    )
                values.update(model_values)
                continue
            # The flattened relational shape uses NULL for an inapplicable metric group.
            names: tuple[str, ...]
            if model_name == "tcp":
                names = (
                    "tcp_connection_count",
                    "tcp_successful_handshake_count",
                    "tcp_incomplete_handshake_count",
                    "tcp_handshake_success_rate",
                    "tcp_initial_rtt_avg_ms",
                    "tcp_initial_rtt_p50_ms",
                    "tcp_initial_rtt_p95_ms",
                    "tcp_ack_rtt_min_ms",
                    "tcp_ack_rtt_p50_ms",
                    "tcp_ack_rtt_p95_ms",
                    "tcp_rtt_tail_inflation",
                    "tcp_data_segment_count",
                    "tcp_retransmission_count",
                    "tcp_fast_retransmission_count",
                    "tcp_spurious_retransmission_count",
                    "tcp_retransmitted_payload_bytes",
                    "tcp_retransmission_rate",
                    "tcp_recovery_tax",
                    "tcp_zero_window_count",
                    "tcp_window_full_count",
                    "tcp_zero_window_duration_ms",
                    "tcp_active_duration_ms",
                    "tcp_receiver_stall_ratio",
                    "tcp_reset_count",
                    "tcp_reset_rate",
                    "tcp_out_of_order_count",
                    "tcp_duplicate_ack_count",
                    "tcp_lost_segment_indicator_count",
                )
            elif model_name == "quic":
                names = (
                    "quic_identified_connection_count",
                    "quic_version_count",
                    "quic_versions_seen",
                    "quic_retry_count",
                    "quic_version_negotiation_count",
                    "quic_0rtt_observed_count",
                    "quic_initial_rtt_avg_ms",
                    "quic_initial_rtt_p50_ms",
                    "quic_initial_rtt_p95_ms",
                    "quic_spin_rtt_sample_count",
                    "quic_spin_rtt_min_ms",
                    "quic_spin_rtt_p50_ms",
                    "quic_spin_rtt_p95_ms",
                )
            elif model_name == "udp":
                names = (
                    "udp_flow_count",
                    "udp_datagram_count",
                    "udp_network_bytes",
                    "udp_payload_bytes",
                    "udp_payload_size_p50",
                    "udp_payload_size_p95",
                    "udp_bidirectional_byte_ratio",
                )
            else:
                names = (
                    "dns_query_count",
                    "dns_response_count",
                    "dns_failure_count",
                    "dns_rtt_p50_ms",
                    "dns_rtt_p95_ms",
                )
            values.update(dict.fromkeys(names))
        self._session.execute(insert(NetworkAnalysisResult).values(**values))
