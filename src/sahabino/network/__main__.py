from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from sahabino.common.config import get_settings
from sahabino.db.sync_session import create_sync_session_factory
from sahabino.messaging.producer import KafkaProducer
from sahabino.network.analyzer import TSharkEngine, analyze_capture
from sahabino.network.bootstrap import run_worker
from sahabino.network.operations import (
    cleanup_capture_object,
    reconcile_captures,
    retry_capture,
)
from sahabino.network.publisher import KafkaNetworkPublisher
from sahabino.network.storage import S3ObjectStorage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sahabino.network")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("worker", help="run the Kafka-driven network analyzer")
    commands.add_parser("storage-init", help="create or verify the capture bucket")
    commands.add_parser("reconcile", help="recover stuck capture lifecycle records")
    retry = commands.add_parser("retry", help="retry one terminal analysis failure")
    retry.add_argument("capture_id", type=UUID)
    cleanup = commands.add_parser("cleanup", help="safely remove one terminal raw object")
    cleanup.add_argument("capture_id", type=UUID)
    analyze = commands.add_parser("analyze", help="analyze a local PCAP without DB or Kafka")
    analyze.add_argument("--pcap", type=Path, required=True)
    analyze.add_argument("--scenario", choices=("upload", "download"), required=True)
    analyze.add_argument("--transfer-file-size-bytes", type=int, required=True)
    analyze.add_argument(
        "--tshark",
        default=os.environ.get("SAHABINO_NETWORK_TSHARK_PATH", "tshark"),
    )
    analyze.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.environ.get("SAHABINO_NETWORK_TSHARK_TIMEOUT_SECONDS", "120")),
    )
    analyze.add_argument(
        "--max-parsed-records",
        type=int,
        default=int(os.environ.get("SAHABINO_NETWORK_MAX_PARSED_RECORDS", "250000")),
    )
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.command == "worker":
        run_worker()
        return
    if arguments.command == "analyze":
        metrics = analyze_capture(
            arguments.pcap,
            arguments.scenario,
            arguments.transfer_file_size_bytes,
            engine=TSharkEngine(
                arguments.tshark,
                arguments.timeout_seconds,
                arguments.max_parsed_records,
            ),
        )
        print(metrics.model_dump_json(indent=2))
        return

    settings = get_settings()
    storage = S3ObjectStorage(settings)
    if arguments.command == "storage-init":
        created = storage.ensure_bucket()
        print("Capture bucket created" if created else "Capture bucket already exists")
        return

    session_factory = create_sync_session_factory(settings.database_url)
    if arguments.command == "cleanup":
        affected = cleanup_capture_object(
            arguments.capture_id,
            session_factory=session_factory,
            storage=storage,
        )
        print(json.dumps({"capture_id": str(arguments.capture_id), "rows": affected}))
        return

    publisher = KafkaNetworkPublisher(KafkaProducer.from_settings(settings))
    try:
        if arguments.command == "reconcile":
            result = reconcile_captures(
                session_factory=session_factory,
                storage=storage,
                publisher=publisher,
                settings=settings,
            )
            print(json.dumps(result, sort_keys=True))
        elif arguments.command == "retry":
            capture = retry_capture(
                arguments.capture_id,
                session_factory=session_factory,
                publisher=publisher,
            )
            print(json.dumps({"capture_id": str(capture.id), "status": capture.status}))
    finally:
        publisher.close()


if __name__ == "__main__":
    main()
