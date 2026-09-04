# Sahabino

A data collection and analytics platform for monitoring application metrics,
reviews, and network-quality measurements.

The current service provides an application registry and read-only category
reference data through a FastAPI API backed by PostgreSQL.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16, or Docker with Docker Compose
- A running Docker daemon when integration tests use Testcontainers

## Local setup

Install the locked development environment and create a local configuration file:

```bash
uv sync
cp .env.example .env
uv run pre-commit install
```

The example configuration connects to PostgreSQL on `localhost:5432` with
development-only credentials. Database configuration is read from
`SAHABINO_DATABASE_URL` and must use a Psycopg 3 SQLAlchemy URL, for example:

```text
postgresql+psycopg://sahabino:sahabino@localhost:5432/sahabino
```

Start only PostgreSQL with Docker, apply the schema and seeded reference data,
then run the API locally:

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn sahabino.main:app --reload
```

Migrations are intentionally explicit; the API does not run Alembic at startup.

Once running, the interactive Swagger UI is available at
<http://localhost:8000/docs> and the OpenAPI document at
<http://localhost:8000/openapi.json>.

## Docker Compose

The Compose stack contains only PostgreSQL and the API. Build the API, start the
database, run migrations as a one-off command, and then start the service:

```bash
docker compose build api
docker compose up -d postgres
docker compose run --rm api uv run --no-sync alembic upgrade head
docker compose up -d api
```

PostgreSQL data is retained in the named `postgres_data` volume. Compose supplies
the API with its container-network database URL; `.env.example` is for processes
run directly on the host.

## API

The implemented endpoints are:

```text
POST   /applications
GET    /applications
GET    /applications/{application_id}
PATCH  /applications/{application_id}
DELETE /applications/{application_id}
GET    /categories
```

`GET /applications` accepts the optional `active=true` or `active=false` filter.
Deleting an application deactivates it without removing its database row.
Categories are seeded by Alembic and are read-only through the API.

## Tests and quality checks

Run the full suite and repository checks with:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pre-commit run --all-files
```

Run only the PostgreSQL-backed integration suite with:

```bash
uv run pytest tests/integration
```

When `TEST_DATABASE_URL` is not set, database integration tests start
`postgres:16-alpine` through Testcontainers. This requires a running Docker
daemon. To use an existing PostgreSQL instance instead, set `TEST_DATABASE_URL`
to a Psycopg 3 SQLAlchemy URL before running pytest. The test suite applies
Alembic migrations to the selected test database; it does not use
`Base.metadata.create_all()`.
