from __future__ import annotations

import logging
import signal
from types import FrameType

from sahabino.common.config import Settings, get_settings
from sahabino.common.observability import configure_logging
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.producer import KafkaProducer
from sahabino.messaging.topics import NETWORK_CAPTURE_READY_TOPIC
from sahabino.network.analyzer import TSharkEngine
from sahabino.network.publisher import KafkaNetworkPublisher
from sahabino.network.storage import S3ObjectStorage
from sahabino.network.worker import NetworkAnalyzerWorker

SERVICE_NAME = "sahabino-network-analyzer"


def build_worker(settings: Settings) -> tuple[NetworkAnalyzerWorker, KafkaNetworkPublisher]:
    engine = TSharkEngine(
        executable=settings.network_tshark_path,
        timeout_seconds=settings.network_tshark_timeout_seconds,
        max_parsed_records=settings.network_max_parsed_records,
    )
    engine.validate_runtime()
    publisher = KafkaNetworkPublisher(KafkaProducer.from_settings(settings))
    worker = NetworkAnalyzerWorker(
        consumer=KafkaConsumer.from_settings(
            settings,
            group_id=settings.network_analyzer_consumer_group_id,
            topics=(NETWORK_CAPTURE_READY_TOPIC,),
            max_poll_interval_ms=settings.network_analyzer_max_poll_interval_ms,
        ),
        session_factory=create_sync_session_factory(settings.database_url),
        storage=S3ObjectStorage(settings),
        publisher=publisher,
        engine=engine,
        consumer_group=settings.network_analyzer_consumer_group_id,
        stale_analysis_seconds=settings.network_stale_analysis_seconds,
    )
    return worker, publisher


def _install_signal_handlers(worker: NetworkAnalyzerWorker) -> None:
    def request_stop(signum: int, frame: FrameType | None) -> None:
        _ = signum
        _ = frame
        worker.stop()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def run_worker() -> None:
    settings = get_settings()
    configure_logging(
        service_name=SERVICE_NAME,
        level=settings.log_level,
        log_format=settings.log_format,
        environment=settings.environment,
    )
    logger = logging.getLogger(__name__)
    worker: NetworkAnalyzerWorker | None = None
    publisher: KafkaNetworkPublisher | None = None
    logger.info(
        "network analyzer process started",
        extra={
            "event": "network.analysis.process.started",
            "consumer_group": settings.network_analyzer_consumer_group_id,
        },
    )
    try:
        worker, publisher = build_worker(settings)
        _install_signal_handlers(worker)
        worker.run_forever()
    finally:
        if worker is not None:
            worker.close()
        if publisher is not None:
            publisher.close()
        logger.info(
            "network analyzer process stopped",
            extra={"event": "network.analysis.process.stopped"},
        )
