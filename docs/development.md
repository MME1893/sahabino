# Development setup

This guide contains the local setup details intentionally kept out of the main
project README.

## Prerequisites

The development workflow expects:

- Python 3.12;
- `uv`;
- Docker with Docker Compose;
- a running Docker daemon for Testcontainers-backed integration tests.

The network analyzer image contains TShark for the containerized network-analysis
path.

## Install the Python environment

```bash
uv sync
cp .env.example .env
uv run pre-commit install
```

`.env.example` contains development values only. Configuration groups are
explained in [configuration.md](configuration.md).

## Docker-first local startup

Build the application images:

```bash
docker compose build api crawler ingestion
```

Start PostgreSQL and Kafka:

```bash
docker compose up -d postgres kafka
```

Apply database migrations:

```bash
docker compose run --rm api uv run --no-sync alembic upgrade head
```

Create or verify the required Kafka topics:

```bash
docker compose run --rm api uv run --no-sync python -m sahabino.messaging.admin
```

Start the core application services:

```bash
docker compose up -d api crawler ingestion
```

Check status:

```bash
docker compose ps
```

The API documentation is available at:

```text
http://localhost:8000/docs
```

## Run the API from the host

When developing the API directly on the host, start PostgreSQL first, apply the
schema, and then run Uvicorn:

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn sahabino.main:app --reload
```

The local `.env.example` is configured so host processes can reach development
PostgreSQL and Kafka through their published localhost ports.

## Kafka administration

Start/check Kafka and provision Sahabino topics:

```bash
docker compose up -d kafka
docker compose ps kafka
uv run python -m sahabino.messaging.admin
```

To query the broker directly:

```bash
docker compose exec kafka \
  /opt/kafka/bin/kafka-broker-api-versions.sh \
  --bootstrap-server localhost:19092
```

Topic creation is explicit; application startup does not silently create the
required topology.

## Crawler

Run one manual crawler cycle:

```bash
uv run python -m sahabino.crawler crawl-once
```

Run the scheduler:

```bash
uv run python -m sahabino.crawler scheduler
```

See [crawler/README.md](crawler/README.md) for architecture, adapters, retry and
proxy behavior, configuration, and test strategy.

## Ingestion

Run the ingestion worker directly:

```bash
uv run python -m sahabino.ingestion
```

The ingestion worker consumes Kafka events and persists their database effects
before committing offsets.

## Observability

Start the optional local logging stack:

```bash
docker compose --profile observability up -d
```

Local endpoints:

```text
Grafana: http://localhost:3000
Loki:    http://localhost:3100
Alloy:   http://localhost:12345
```

Application services emit structured JSON logs under Compose; Alloy collects the
labeled containers and forwards those logs to Loki for inspection in Grafana.

## Network-analysis profile

Network capture analysis is opt-in. Start the required profile with the flow
documented in [network/README.md](network/README.md).

The profile adds S3-compatible capture storage and the dedicated analyzer worker.

## Database migrations

Migrations are explicit:

```bash
uv run alembic upgrade head
```

Normal API, crawler, and ingestion startup must not apply migrations implicitly.

## Full runtime smoke test

Run the real local end-to-end pipeline with:

```bash
bash scripts/smoke-full-pipeline.sh
```

Rebuild application images first when needed:

```bash
bash scripts/smoke-full-pipeline.sh --build
```

See [testing/smoke-full-pipeline.md](testing/smoke-full-pipeline.md) for exact
assertions, options, and side effects.

## Tests and quality checks

Run the normal repository checks with:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pre-commit run --all-files
```

Useful focused suites include:

```bash
uv run pytest tests/integration/app_registry
uv run pytest tests/integration/kafka
uv run pytest tests/integration/crawler
uv run pytest tests/integration/ingestion
uv run pytest tests/integration/network
```

See [../CONTRIBUTING.md](../CONTRIBUTING.md) for the project Git and commit
workflow.
# Network profile

Network services remain opt-in locally: use `docker compose --profile network
up`. The committed SeaweedFS configuration contains development-only
credentials. Run `scripts/smoke-network-pipeline.sh` manually only against a
local/test stack (or another deliberately isolated disposable environment),
never production; production deployment intentionally performs readiness and
connectivity checks without inserting synthetic captures.
