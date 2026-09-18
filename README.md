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

For the initial production bootstrap, use the deployment assistant from a reviewed
checkout. On an already-installed host, use the one-time-installed launcher
`/usr/local/sbin/sahabino-deploy`, following its frozen safety contract:

```bash
# Initial/bootstrap workflow only (review deployment instructions first):
sudo deploy/ansible/sahabino-deploy.sh
# Already-installed launcher; a DEPLOY command, not a health check:
# sudo /usr/local/sbin/sahabino-deploy --deploy --revision origin/main
```

**Do not copy local development commands to the production VPS.** In particular,
`alembic upgrade`, topic provisioning, and the full smoke scripts can modify
services or data.

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

Sentiment and BI are independent of the core ingestion lifecycle:

```text
PostgreSQL review_observations -- optional, standalone sentiment worker --> sentiment columns
PostgreSQL (restricted readers) --> optional Grafana PostgreSQL dashboards / standalone Metabase
```

The sentiment worker does not consume Kafka; Metabase `plan` audits report
*definitions*, and `apply` changes definitions. Neither command runs a crawl or
refreshes source data. Actual visibility requires running workers, valid database
grants, relevant data and an uncached or refreshed dashboard query.

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
| **Standalone Sentiment** | Optional, separately deployed CPU worker that classifies eligible review observations and writes outcomes to PostgreSQL; not a continuous service in the production Compose project. |
| **Metabase BI** | Independent Compose project and restricted PostgreSQL reader; version-controlled saved questions and dashboards are synchronized explicitly, not by crawling. |
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
sentiment/               independent worker and its tests
bi/                      independent Metabase Compose, reports and sync tooling
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
- [Production operations](docs/operations/README.md) — routine service controls,
  health checks, logs, SSH forwarding, and PostgreSQL/Kafka inspection.
- [Production acceptance](docs/operations/PRODUCTION_ACCEPTANCE.md) — safe
  observation-first test sequence; explicitly approved crawler/sentiment write tests;
  database, Kafka, network, Grafana and Metabase output checks and evidence checklist.
- [Standalone sentiment](sentiment/README.md) — model cache, migrations, isolated
  worker execution and status validation.
- [Standalone BI](bi/README.md) and [BI reports](bi/reports/README.md) —
  independent Metabase deployment, capabilities and KPI contract.
- [Observability](docs/observability/README.md) — optional PostgreSQL-backed
  Grafana dashboards, reader grants and log validation.
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

For production, use the deployment assistant for releases, the
[operations runbook](docs/operations/README.md) for routine service control,
and the [production acceptance checklist](docs/operations/PRODUCTION_ACCEPTANCE.md)
for a full, evidence-based verification. A healthy container/API response alone
does not establish successful crawling, sentiment, ingestion, or dashboard results.

A full local runtime smoke test is also available:

```bash
bash scripts/smoke-full-pipeline.sh
```

Detailed behavior, flags, and side effects are documented in the
[smoke-test guide](docs/testing/smoke-full-pipeline.md). This local script is
**not** a safe read-only acceptance command for production.

### Production deployment (local-backup safety contract)

Use the reviewed `deploy/ansible/sahabino-deploy.sh` launcher installed once to
`/usr/local/sbin/sahabino-deploy`. It fetches the latest remote branch commit,
stages that revision's automation, takes verified local-only persistent-data
backups and reconciles missing-image containers after the backup gate. See
[`deploy/ansible/README.md`](deploy/ansible/README.md#frozen-local-only-deployment-contract-2026-09-17).
There is **no automatic database restore** and local backups do not protect
against complete VPS failure. Do not deploy if the backup preflight fails.
