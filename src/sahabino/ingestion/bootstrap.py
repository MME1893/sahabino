from __future__ import annotations

import logging
import signal
from types import FrameType

from sahabino.common.config import Settings, get_settings
from sahabino.common.observability import configure_logging
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.ingestion.worker import IngestionWorker
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.topics import (
    PLAYSTORE_APP_STATS_TOPIC,
    PLAYSTORE_REVIEW_OBSERVED_TOPIC,
)

SERVICE_NAME = "sahabino-ingestion"
INGESTION_TOPICS = (PLAYSTORE_APP_STATS_TOPIC, PLAYSTORE_REVIEW_OBSERVED_TOPIC)


def build_worker(settings: Settings) -> IngestionWorker:
    session_factory = create_sync_session_factory(settings.database_url)
    consumer = KafkaConsumer.from_settings(
        settings,
        group_id=settings.ingestion_consumer_group_id,
        topics=INGESTION_TOPICS,
    )
    return IngestionWorker(
        consumer=consumer,
        session_factory=session_factory,
        consumer_group=settings.ingestion_consumer_group_id,
    )


def _install_signal_handlers(worker: IngestionWorker) -> None:
    def request_stop(signum: int, frame: FrameType | None) -> None:
        _ = signum
        _ = frame
        worker.stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def main() -> None:
    settings = get_settings()
    configure_logging(
        service_name=SERVICE_NAME,
        level=settings.log_level,
        log_format=settings.log_format,
        environment=settings.environment,
    )
    logger = logging.getLogger(__name__)
    logger.info(
        "ingestion process started",
        extra={
            "event": "ingestion.process.started",
            "consumer_group": settings.ingestion_consumer_group_id,
        },
    )
    worker: IngestionWorker | None = None
    try:
        worker = build_worker(settings)
        _install_signal_handlers(worker)
        worker.run_forever()
    finally:
        if worker is not None:
            worker.close()
        logger.info(
            "ingestion process stopped",
            extra={
                "event": "ingestion.process.stopped",
                "consumer_group": settings.ingestion_consumer_group_id,
            },
        )
