# Configuration reference

Sahabino uses environment-based configuration. `.env.example` is the local
reference file; production values are rendered by the deployment automation and
must not reuse development credentials.

This page groups the available settings by subsystem so the main README does not
need to describe every variable individually.

## Database

| Variable | Purpose |
| --- | --- |
| `POSTGRES_DB` | PostgreSQL database created by the Compose service. |
| `POSTGRES_USER` | PostgreSQL application user. |
| `POSTGRES_PASSWORD` | PostgreSQL password used by local Compose. |
| `SAHABINO_DATABASE_URL` | SQLAlchemy/Psycopg database URL used by Sahabino processes. |

When Sahabino runs inside Compose, services use the internal PostgreSQL service
address. Host-side development uses the published localhost endpoint from
`.env.example`.

## Application logging

| Variable | Purpose |
| --- | --- |
| `SAHABINO_LOG_LEVEL` | Application log level. |
| `SAHABINO_LOG_FORMAT` | Console or structured JSON output mode. |
| `SAHABINO_ENVIRONMENT` | Environment label included in application behavior/logging. |

Compose uses structured logs for the API, crawler, ingestion worker, and network
analyzer so Alloy can forward them to Loki.

## Kafka and ingestion

| Variable | Purpose |
| --- | --- |
| `SAHABINO_KAFKA_BOOTSTRAP_SERVERS` | Kafka bootstrap address. |
| `SAHABINO_KAFKA_TOPIC_PARTITIONS` | Expected partition count for Sahabino topics. |
| `SAHABINO_KAFKA_TOPIC_REPLICATION_FACTOR` | Expected topic replication factor. |
| `SAHABINO_KAFKA_CONSUMER_AUTO_OFFSET_RESET` | Consumer offset-reset policy. |
| `SAHABINO_INGESTION_CONSUMER_GROUP_ID` | Consumer group used by the ingestion worker. |
| `SAHABINO_NETWORK_ANALYZER_CONSUMER_GROUP_ID` | Consumer group used by the network analyzer. |
| `SAHABINO_KAFKA_PRODUCER_QUEUE_FULL_MAX_RETRIES` | Bounded producer retry count when the local producer queue is full. |
| `SAHABINO_KAFKA_PRODUCER_QUEUE_FULL_POLL_TIMEOUT_SECONDS` | Poll interval used by that queue-full retry path. |

Topic topology is provisioned explicitly with:

```bash
uv run python -m sahabino.messaging.admin
```

## Google Play crawler

| Variable | Purpose |
| --- | --- |
| `SAHABINO_PLAYSTORE_CRAWL_INTERVAL_MINUTES` | Scheduler interval. |
| `SAHABINO_PLAYSTORE_MAX_CONCURRENT_APPS` | Maximum number of applications crawled concurrently. |
| `SAHABINO_PLAYSTORE_LANGUAGE_CODE` | Google Play language code. |
| `SAHABINO_PLAYSTORE_COUNTRY_CODE` | Google Play country code. |
| `SAHABINO_PLAYSTORE_REQUEST_TIMEOUT_SECONDS` | Per-request timeout. |
| `SAHABINO_PLAYSTORE_RETRY_MAX_ATTEMPTS` | Bounded request retry count. |
| `SAHABINO_PLAYSTORE_RETRY_MAX_DELAY_SECONDS` | Maximum retry backoff delay. |
| `SAHABINO_PLAYSTORE_RATE_LIMIT_ENABLED` | Enables the process-wide crawler rate limiter. |
| `SAHABINO_PLAYSTORE_RATE_LIMIT_REFILL_PER_SECOND` | Token refill rate. |
| `SAHABINO_PLAYSTORE_RATE_LIMIT_BURST_CAPACITY` | Token-bucket burst capacity. |
| `SAHABINO_PLAYSTORE_PROXY_ENABLED` | Enables crawler proxy selection. |
| `SAHABINO_PLAYSTORE_PROXY_URLS` | JSON array of crawler proxy URLs. |
| `SAHABINO_PLAYSTORE_PROXY_DIRECT_FALLBACK` | Allows direct requests when configured proxy policy permits it. |
| `SAHABINO_PLAYSTORE_PROXY_FAILURE_THRESHOLD` | Proxy failure threshold before unhealthy/cooldown handling. |
| `SAHABINO_PLAYSTORE_PROXY_COOLDOWN_SECONDS` | Proxy cooldown interval. |
| `SAHABINO_PLAYSTORE_PROXY_RATE_LIMIT_ROTATE_AFTER` | Rotation threshold for repeated rate-limit outcomes. |
| `SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_ENABLED` | Enables the crawler circuit breaker. |
| `SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | Failures required before opening the breaker. |
| `SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | Circuit-breaker cooldown interval. |
| `SAHABINO_PLAYSTORE_SECONDARY_ADAPTER_ENABLED` | Enables the bounded secondary adapter fallback. |
| `SAHABINO_APPLICATION_REGISTRY_BASE_URL` | Base URL used by the crawler to read active applications from the Registry API. |

Proxy credentials must never be committed. The detailed crawler policy is
documented in [crawler/README.md](crawler/README.md).

## Object storage and network analysis

| Variable | Purpose |
| --- | --- |
| `SAHABINO_OBJECT_STORAGE_ENDPOINT_URL` | Internal S3-compatible endpoint used by services. |
| `SAHABINO_OBJECT_STORAGE_PUBLIC_ENDPOINT_URL` | Endpoint used when generating client-facing presigned URLs. |
| `SAHABINO_OBJECT_STORAGE_BUCKET` | Capture bucket name. |
| `SAHABINO_OBJECT_STORAGE_ACCESS_KEY` | S3-compatible access key. |
| `SAHABINO_OBJECT_STORAGE_SECRET_KEY` | S3-compatible secret key. |
| `SAHABINO_OBJECT_STORAGE_REGION` | Signature region used by the S3-compatible client. |
| `SAHABINO_OBJECT_STORAGE_PRESIGN_EXPIRY_SECONDS` | Presigned URL expiry. |
| `SAHABINO_OBJECT_STORAGE_MAX_CAPTURE_SIZE_BYTES` | Maximum accepted capture size. |
| `SAHABINO_NETWORK_TSHARK_PATH` | TShark executable path. |
| `SAHABINO_NETWORK_TSHARK_TIMEOUT_SECONDS` | Analyzer timeout. |
| `SAHABINO_NETWORK_MAX_PARSED_RECORDS` | Maximum parsed-record guardrail. |
| `SAHABINO_NETWORK_ANALYZER_MAX_POLL_INTERVAL_MS` | Kafka poll interval budget for long-running analysis. |
| `SAHABINO_NETWORK_PENDING_UPLOAD_EXPIRY_SECONDS` | Expiry window for unfinished uploads. |
| `SAHABINO_NETWORK_STALE_ANALYSIS_SECONDS` | Threshold used to identify stale analysis work. |

See [network/README.md](network/README.md) for the capture lifecycle and analysis
semantics.

## Grafana development credentials

| Variable | Purpose |
| --- | --- |
| `GRAFANA_ADMIN_USER` | Local Grafana administrator username. |
| `GRAFANA_ADMIN_PASSWORD` | Local Grafana administrator password. |

The credentials in `.env.example` are development-only. Production credentials
are managed through the deployment/Vault flow documented in
[../deploy/ansible/README.md](../deploy/ansible/README.md).

## Local vs production configuration

`.env.example` is intended as a local-development baseline. Production should be
managed through the deployment assistant and Ansible rather than by manually
copying development values onto the server.

For production configuration, permissions, Vault behavior, and rerun policy, see
[Production deployment](../deploy/ansible/README.md).
# Production object-storage exposure

Containers always use `SAHABINO_OBJECT_STORAGE_ENDPOINT_URL=http://seaweedfs:8333`.
The distinct public endpoint is embedded in presigned URLs: private production
uses `http://127.0.0.1:8333` for an SSH local forward, while public mode requires
an operator-supplied HTTP(S) origin. Production credentials come from Ansible
Vault and the generated SeaweedFS bind source is outside the Git checkout at
`/opt/sahabino/runtime/seaweedfs-s3.json` (root:root, `0600`).
