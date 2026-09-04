# Contributing

## Git Workflow

Development follows an issue-driven workflow:

1. Create or select a GitHub issue.
2. Create a branch from `main`.
3. Implement the change using focused commits.
4. Open a Pull Request.
5. Merge into `main` after CI checks pass.

Avoid committing feature work directly to `main`.

## Branch Naming

Branches should start with the related GitHub issue number followed by a short description.

```text
<issue-number>-<short-description>
```

Examples:

```text
1-bootstrap-project-foundation
2-add-application-registry
3-add-kafka-messaging
```

## Commit Messages

This project follows the Conventional Commits convention.

```text
<type>(<scope>): <description>
```

The scope is optional but recommended when it makes the affected subsystem clearer.

### Types

| Type       | Purpose                                             |
| ---------- | --------------------------------------------------- |
| `feat`     | Introduces a new user-facing or system capability   |
| `fix`      | Fixes a bug                                         |
| `test`     | Adds or modifies tests                              |
| `docs`     | Documentation-only changes                          |
| `refactor` | Changes internal code without changing behavior     |
| `perf`     | Improves performance                                |
| `chore`    | Repository maintenance, dependencies, or tooling    |
| `ci`       | CI/CD configuration or workflow changes             |
| `build`    | Build system, packaging, or container build changes |

### Common Scopes

Use scopes only when they add useful context.

```text
repo
registry
db
crawler
messaging
ingestion
network
analytics
infra
test
docs
```

New scopes may be introduced when a clear subsystem requires one.

### Examples

```text
chore(repo): bootstrap Python project

chore(tooling): configure pre-commit hooks

feat(registry): add application creation endpoint

feat(db): add application persistence models

feat(crawler): publish Play Store statistics

fix(ingestion): prevent duplicate review revisions

test(network): cover retransmission calculation

ci(github): add GitHub Actions CI workflow

docs(architecture): document Kafka delivery semantics
```

### Commit Guidelines

A commit should represent one coherent logical change.

Prefer:

```text
feat(registry): add application deactivation
test(registry): cover application deactivation
```

Avoid vague messages such as:

```text
update
changes
fix stuff
final changes
```

Do not split changes into meaningless commits solely to create a longer Git history.

## Before Committing

Run:

```bash
uv run pre-commit run --all-files
```

Before opening a Pull Request, also verify:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

## Database-backed Development

Copy `.env.example` to `.env` and start the development PostgreSQL service before running the API from the host:

```bash
docker compose up -d postgres
uv run alembic upgrade head
uv run uvicorn sahabino.main:app --reload
```

Schema and reference-data changes belong in Alembic migrations. The application does not apply migrations automatically, and normal startup code must not call `Base.metadata.create_all()`.

## Integration Tests

Integration tests use `TEST_DATABASE_URL` when it is set. Otherwise, they start `postgres:16-alpine` automatically with Testcontainers, so a running Docker daemon is required for local integration tests:

```bash
uv run pytest
```

`TEST_DATABASE_URL` must point to a disposable test database. Integration tests apply migrations and clear application-related tables between tests, so it must never point to a development, staging, or production database containing data that needs to be preserved.

In GitHub Actions, the CI workflow starts a temporary PostgreSQL service and provides its connection string through `TEST_DATABASE_URL`. Testcontainers is therefore not used inside CI.

The GitHub Actions PostgreSQL service exists only for the duration of the CI job and is separate from local development and production databases.

## Container Validation

The GitHub Actions test workflow does not use `docker-compose.yml` to run the test suite. Docker Compose remains the local development environment for running the application and PostgreSQL together.

Before submitting container or Compose changes, validate the Compose configuration:

```bash
docker compose config
```
