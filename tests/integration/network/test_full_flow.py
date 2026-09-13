from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sahabino.common.config import Settings
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.ingestion.models import IngestedEvent
from sahabino.ingestion.worker import IngestionWorker
from sahabino.messaging.consumer import KafkaConsumer
from sahabino.messaging.network_events import NetworkAnalysisCollectedV1
from sahabino.messaging.producer import KafkaProducer
from sahabino.messaging.topics import (
    NETWORK_ANALYSIS_COLLECTED_TOPIC,
    NETWORK_CAPTURE_READY_TOPIC,
)
from sahabino.network.analyzer import TsharkOutput
from sahabino.network.metrics import PacketRecord
from sahabino.network.models import NetworkAnalysisResult, NetworkCapture
from sahabino.network.publisher import KafkaNetworkPublisher, NetworkPublisher
from sahabino.network.storage import S3ObjectStorage
from sahabino.network.worker import NetworkAnalyzerWorker
from scripts.generate_network_fixture import build_fixture

DEADLINE_SECONDS = 30.0


class DeterministicEngine:
    """Packet engine for orchestration; the separate system test exercises real TShark."""

    def read_packets(self, capture_path: Path) -> TsharkOutput:
        assert capture_path.read_bytes() == build_fixture()
        common: dict[str, Any] = {
            "frame_len": 74,
            "captured_len": 74,
            "ip_version": 4,
            "network_bytes": 60,
            "src_port": 50000,
            "dst_port": 443,
            "transport": "tcp",
        }
        packets = [
            PacketRecord(
                timestamp=1.00,
                direction="tx",
                src_ip="10.0.0.1",
                dst_ip="10.0.0.2",
                l4_payload_bytes=0,
                tcp_syn=True,
                **common,
            ),
            PacketRecord(
                timestamp=1.05,
                direction="rx",
                src_ip="10.0.0.2",
                dst_ip="10.0.0.1",
                src_port=443,
                dst_port=50000,
                l4_payload_bytes=0,
                tcp_syn=True,
                tcp_ack=True,
                tcp_initial_rtt_seconds=0.05,
                frame_len=74,
                captured_len=74,
                ip_version=4,
                network_bytes=60,
                transport="tcp",
            ),
            PacketRecord(
                timestamp=1.10,
                direction="tx",
                src_ip="10.0.0.1",
                dst_ip="10.0.0.2",
                l4_payload_bytes=10,
                tcp_ack=True,
                tcp_ack_rtt_seconds=0.05,
                **common,
            ),
            PacketRecord(
                timestamp=1.20,
                direction="tx",
                src_ip="10.0.0.1",
                dst_ip="10.0.0.2",
                l4_payload_bytes=10,
                tcp_ack=True,
                tcp_ack_rtt_seconds=0.10,
                **common,
            ),
        ]
        return TsharkOutput(
            version="TShark deterministic-integration",
            supported_fields=set(),
            packets=packets,
        )


class RecordingPublisher(NetworkPublisher):
    def __init__(self, delegate: KafkaNetworkPublisher) -> None:
        self.delegate = delegate
        self.analysis_payloads: list[NetworkAnalysisCollectedV1] = []

    def publish_ready(self, capture: NetworkCapture, package_name: str) -> None:
        self.delegate.publish_ready(capture, package_name)

    def publish_analysis(
        self,
        capture: NetworkCapture,
        payload: NetworkAnalysisCollectedV1,
    ) -> None:
        self.analysis_payloads.append(payload)
        self.delegate.publish_analysis(capture, payload)


def _process(worker: NetworkAnalyzerWorker | IngestionWorker, expected: int) -> None:
    processed = 0
    deadline = time.monotonic() + DEADLINE_SECONDS
    while processed < expected and time.monotonic() < deadline:
        processed += int(worker.process_next(timeout=0.5))
    assert processed == expected


def test_complete_capture_store_analyze_publish_ingest_is_idempotent(
    network_api_client: TestClient,
    create_network_application: Any,
    network_settings: Settings,
    network_storage: S3ObjectStorage,
) -> None:
    capture_bytes = build_fixture()
    sha256 = hashlib.sha256(capture_bytes).hexdigest()
    application = create_network_application(package_name="com.example.fullflow")
    request = {
        "application_id": application["id"],
        "scenario": "upload",
        "filename": "full-flow.pcapng",
        "capture_size_bytes": len(capture_bytes),
        "transfer_file_size_bytes": 20,
        "sha256": sha256,
    }
    idempotency_key = str(uuid4())
    first = network_api_client.post(
        "/network-captures", json=request, headers={"Idempotency-Key": idempotency_key}
    ).json()
    duplicate = network_api_client.post(
        "/network-captures", json=request, headers={"Idempotency-Key": idempotency_key}
    ).json()
    assert duplicate["capture_id"] == first["capture_id"]

    upload = httpx.put(
        first["upload_url"],
        content=capture_bytes,
        headers={"Content-Type": "application/x-pcapng"},
        timeout=15,
    )
    assert upload.is_success
    complete_path = f"/network-captures/{first['capture_id']}/complete"
    assert network_api_client.post(complete_path).status_code == 200
    assert network_api_client.post(complete_path).status_code == 200

    session_factory = create_sync_session_factory(network_settings.database_url)
    analyzer_group = f"network-analyzer-integration-{uuid4().hex}"
    analyzer_consumer = KafkaConsumer(
        network_settings.kafka_bootstrap_servers,
        group_id=analyzer_group,
        topics=(NETWORK_CAPTURE_READY_TOPIC,),
    )
    delegate = KafkaNetworkPublisher(KafkaProducer.from_settings(network_settings))
    recording_publisher = RecordingPublisher(delegate)
    analyzer = NetworkAnalyzerWorker(
        consumer=analyzer_consumer,
        session_factory=session_factory,
        storage=network_storage,
        publisher=recording_publisher,
        engine=DeterministicEngine(),
        consumer_group=analyzer_group,
    )
    try:
        _process(analyzer, expected=2)
    finally:
        analyzer.close()

    assert len(recording_publisher.analysis_payloads) == 1
    with session_factory() as session:
        capture = session.get(NetworkCapture, UUID(first["capture_id"]))
        assert capture is not None
        assert capture.status == "analyzed"
        assert capture.verified_sha256 == sha256
        assert capture.analysis_attempt_count == 1
        # Duplicate the same stable analysis event before ingestion.
        recording_publisher.publish_analysis(capture, recording_publisher.analysis_payloads[0])
    delegate.close()

    ingestion_group = f"network-ingestion-integration-{uuid4().hex}"
    ingestion_consumer = KafkaConsumer(
        network_settings.kafka_bootstrap_servers,
        group_id=ingestion_group,
        topics=(NETWORK_ANALYSIS_COLLECTED_TOPIC,),
    )
    ingestion = IngestionWorker(
        consumer=ingestion_consumer,
        session_factory=session_factory,
        consumer_group=ingestion_group,
    )
    try:
        _process(ingestion, expected=2)
    finally:
        ingestion.close()

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(NetworkCapture)) == 1
        assert session.scalar(select(func.count()).select_from(NetworkAnalysisResult)) == 1
        assert session.scalar(select(func.count()).select_from(IngestedEvent)) == 1
        result = session.scalar(select(NetworkAnalysisResult))
        assert result is not None
        assert result.analysis_id == first["analysis_id"]
        assert result.comparison_ready is True
        assert result.effective_file_throughput_mbps is not None

    second_application = create_network_application(package_name="com.example.contentreuse")
    reused = network_api_client.post(
        "/network-captures",
        json={**request, "application_id": second_application["id"]},
        headers={"Idempotency-Key": str(uuid4())},
    ).json()
    assert reused["capture_id"] != first["capture_id"]
    assert reused["upload_required"] is False

    with session_factory() as session:
        captures = list(session.scalars(select(NetworkCapture).order_by(NetworkCapture.id)))
        assert len(captures) == 2
        assert len({capture.object_key for capture in captures}) == 1
        object_key = captures[0].object_key
    objects = network_storage._client.list_objects_v2(  # type: ignore[attr-defined]
        Bucket=network_settings.object_storage_bucket,
        Prefix=object_key,
    ).get("Contents", [])
    assert len(objects) == 1
