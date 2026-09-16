# Sahabino

Sahabino is a data collection and analytics platform for monitoring application
metadata, reviews, and network-quality measurements.

It combines an application registry, scheduled Google Play crawling, Kafka-based
event delivery, idempotent ingestion into PostgreSQL, optional packet-capture
analysis, and centralized production logging.

## Quick install

For a local Docker-based setup:

```bash
cp .env.example .env

docker compose build api crawler ingestion
docker compose up -d postgres kafka

docker compose run --rm api uv run --no-sync alembic upgrade head
docker compose run --rm api uv run --no-sync python -m sahabino.messaging.admin

docker compose up -d api crawler ingestion
```

Open the API documentation at:

```text
http://localhost:8000/docs
```

To add the local logging stack:

```bash
docker compose --profile observability up -d
```

For a production deployment, use the deployment assistant instead of manually
reconstructing the Compose and Ansible sequence:

```bash
sudo deploy/ansible/sahabino-deploy.sh
```

See [Development setup](docs/development.md) for local installation details and
[Production deployment](deploy/ansible/README.md) for the complete server flow.

## System flow

The main application pipeline is:

```text
Application Registry
        │
        ▼
Scheduled Google Play Crawler
        │
        ├── crawl lifecycle ───────────────► PostgreSQL
        │
        └── collected application/review events
                            │
                            ▼
                           Kafka
                            │
                            ▼
                     Ingestion Worker
                            │
                            ▼
                        PostgreSQL
```

Network analysis is an opt-in profile for local development and is enabled by
default in production alongside the observability profile:

```text
Network Capture API
        │
        ▼
S3-compatible Object Storage
        │
        ▼
Kafka capture-ready event
        │
        ▼
TShark Network Analyzer
        │
        ▼
Kafka analysis event
        │
        ▼
Ingestion Worker
        │
        ▼
PostgreSQL
```

Production application logs follow a separate observability path:

```text
API / Crawler / Ingestion
        │
        ▼
 structured stdout logs
        │
        ▼
       Alloy
        │
        ▼
        Loki
        │
        ▼
      Grafana
```

## Core components

| Component | Responsibility |
| --- | --- |
| **Application Registry** | Stores and manages the applications Sahabino monitors. |
| **Crawler** | Runs scheduled Google Play collection and records crawl lifecycle state. |
| **Messaging** | Defines event contracts, topics, producers, consumers, and topic provisioning. |
| **Ingestion** | Consumes collected events and applies idempotent PostgreSQL writes. |
| **Network Analysis** | Handles capture metadata, object storage, TShark analysis, and analysis events. |
| **Observability** | Collects structured application logs through Alloy, Loki, and Grafana. |
| **Deployment** | Uses Ansible plus the deployment assistant for repeatable production provisioning and releases. |

## Architecture principles

A few boundaries are intentionally kept explicit:

- schema changes are applied through Alembic migrations rather than application startup;
- the crawler records lifecycle state directly, but analytical crawler output is delivered through Kafka;
- ingestion commits Kafka offsets only after the corresponding database transaction succeeds;
- message handling is designed for at-least-once delivery with idempotent database effects;
- network-capture files are stored outside PostgreSQL and analyzed asynchronously;
- production observability services are operational infrastructure, not application dependencies;
- production deployment keeps secrets, generated runtime state, and the Ansible controller outside normal source-control state where appropriate.

## Repository layout

```text
src/sahabino/
├── app_registry/        application registry API and persistence
├── crawler/             Google Play collection pipeline
├── ingestion/           Kafka-to-PostgreSQL ingestion
├── messaging/           Kafka contracts and clients
├── network/             capture and network-analysis pipeline
├── common/              shared configuration/logging
└── db/                  database infrastructure

deploy/ansible/          production provisioning and deployment
infrastructure/          observability and network-service configuration
migrations/              Alembic database migrations
scripts/                 smoke-test and utility scripts
tests/                   unit, integration, system, and external smoke tests
```

## Documentation

Use the focused guides below instead of treating this README as an operations
manual:

- [Development setup](docs/development.md) — local environment, Docker workflow,
  migrations, topic provisioning, and developer commands.
- [Configuration reference](docs/configuration.md) — environment variables and
  configuration groups.
- [Production deployment](deploy/ansible/README.md) — deploy assistant, Ansible,
  Vault, Deploy Key, permissions, backup/restore, and deployment troubleshooting.
- [Production operations](docs/operations/README.md) — start/stop/up/down, health
  checks, logs, SSH forwarding, database/Kafka checks, and routine operations.
- [Crawler documentation](docs/crawler/README.md) — crawler architecture,
  resilience, adapters, configuration, testing, and maintenance.
- [Network analysis](docs/network/README.md) — capture lifecycle, storage,
  analysis, API, metrics, and operational behavior.
- [Full runtime smoke test](docs/testing/smoke-full-pipeline.md) — end-to-end
  validation of the real Compose pipeline.
- [Contributing](CONTRIBUTING.md) — Git workflow, commit conventions, and quality
  checks.

## API surface

The API currently exposes application-registry and network-capture operations.
The interactive OpenAPI documentation is the canonical way to inspect request
and response schemas while the service is running:

```text
http://localhost:8000/docs
```

Main endpoint groups:

```text
/applications
/categories
/network-captures
```

## Running and verification

For local development, use the commands in
[Development setup](docs/development.md).

For production, use the deployment assistant for releases and the
[operations runbook](docs/operations/README.md) for routine service control and
health checks.

A full local runtime smoke test is also available:

```bash
bash scripts/smoke-full-pipeline.sh
```

Detailed behavior, flags, and side effects are documented in the
[smoke-test guide](docs/testing/smoke-full-pipeline.md).
