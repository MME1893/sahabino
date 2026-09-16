#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import re
import sys

REQUIRED_KEYS = frozenset(
    {
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "GRAFANA_ADMIN_USER",
        "GRAFANA_ADMIN_PASSWORD",
        "SAHABINO_API_BIND_ADDRESS",
        "SAHABINO_API_PORT",
        "SAHABINO_ENVIRONMENT",
        "SAHABINO_LOG_FORMAT",
        "SAHABINO_LOG_LEVEL",
        "SAHABINO_KAFKA_TOPIC_PARTITIONS",
        "SAHABINO_KAFKA_TOPIC_REPLICATION_FACTOR",
        "SAHABINO_KAFKA_CONSUMER_AUTO_OFFSET_RESET",
        "SAHABINO_KAFKA_PRODUCER_QUEUE_FULL_MAX_RETRIES",
        "SAHABINO_KAFKA_PRODUCER_QUEUE_FULL_POLL_TIMEOUT_SECONDS",
        "SAHABINO_INGESTION_CONSUMER_GROUP_ID",
        "SAHABINO_NETWORK_ANALYZER_CONSUMER_GROUP_ID",
        "SAHABINO_NETWORK_ANALYZER_MAX_POLL_INTERVAL_MS",
        "SAHABINO_NETWORK_MAX_PARSED_RECORDS",
        "SAHABINO_OBJECT_STORAGE_EXPOSURE",
        "SAHABINO_OBJECT_STORAGE_BIND_ADDRESS",
        "SAHABINO_OBJECT_STORAGE_ENDPOINT_URL",
        "SAHABINO_OBJECT_STORAGE_PUBLIC_ENDPOINT_URL",
        "SAHABINO_OBJECT_STORAGE_BUCKET",
        "SAHABINO_OBJECT_STORAGE_ACCESS_KEY",
        "SAHABINO_OBJECT_STORAGE_SECRET_KEY",
        "SAHABINO_OBJECT_STORAGE_REGION",
        "SAHABINO_OBJECT_STORAGE_PRESIGN_EXPIRY_SECONDS",
        "SAHABINO_OBJECT_STORAGE_MAX_CAPTURE_SIZE_BYTES",
        "SAHABINO_NETWORK_TSHARK_PATH",
        "SAHABINO_NETWORK_TSHARK_TIMEOUT_SECONDS",
        "SAHABINO_NETWORK_PENDING_UPLOAD_EXPIRY_SECONDS",
        "SAHABINO_NETWORK_STALE_ANALYSIS_SECONDS",
        "SAHABINO_SEAWEEDFS_CONFIG_PATH",
        "SAHABINO_PLAYSTORE_CRAWL_INTERVAL_MINUTES",
        "SAHABINO_PLAYSTORE_MAX_CONCURRENT_APPS",
        "SAHABINO_PLAYSTORE_LANGUAGE_CODE",
        "SAHABINO_PLAYSTORE_COUNTRY_CODE",
        "SAHABINO_PLAYSTORE_REQUEST_TIMEOUT_SECONDS",
        "SAHABINO_PLAYSTORE_RETRY_MAX_ATTEMPTS",
        "SAHABINO_PLAYSTORE_RETRY_MAX_DELAY_SECONDS",
        "SAHABINO_PLAYSTORE_RATE_LIMIT_ENABLED",
        "SAHABINO_PLAYSTORE_RATE_LIMIT_REFILL_PER_SECOND",
        "SAHABINO_PLAYSTORE_RATE_LIMIT_BURST_CAPACITY",
        "SAHABINO_PLAYSTORE_PROXY_ENABLED",
        "SAHABINO_PLAYSTORE_PROXY_URLS",
        "SAHABINO_PLAYSTORE_PROXY_DIRECT_FALLBACK",
        "SAHABINO_PLAYSTORE_PROXY_FAILURE_THRESHOLD",
        "SAHABINO_PLAYSTORE_PROXY_COOLDOWN_SECONDS",
        "SAHABINO_PLAYSTORE_PROXY_RATE_LIMIT_ROTATE_AFTER",
        "SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_ENABLED",
        "SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_FAILURE_THRESHOLD",
        "SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_COOLDOWN_SECONDS",
        "SAHABINO_PLAYSTORE_SECONDARY_ADAPTER_ENABLED",
    }
)
KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def validate_environment(path: pathlib.Path) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise ValueError("candidate must be a non-empty regular file")

    found: set[str] = set()
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = raw_line.partition("=")
        if not separator or not KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid assignment at line {number}")
        if key in found:
            raise ValueError(f"duplicate variable {key}")
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError(f"invalid value encoding for {key}")
        if key in REQUIRED_KEYS and not value:
            raise ValueError(f"required variable {key} is empty")
        found.add(key)

    missing = sorted(REQUIRED_KEYS - found)
    if missing:
        raise ValueError("missing required variables: " + ", ".join(missing))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: validate_production_env.py FILE", file=sys.stderr)
        return 2
    try:
        validate_environment(pathlib.Path(argv[1]))
    except (OSError, UnicodeError, ValueError) as error:
        print(f"invalid production environment candidate: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
