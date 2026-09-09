# 04 - Crawler Operations and Configuration Guide

## 1. Runtime modes

The crawler is an independently runnable process.

### One manual run

```bash
uv run python -m sahabino.crawler crawl-once
```

The command prints the crawl run UUID. The persisted trigger type is `manual`.

### Blocking scheduler

```bash
uv run python -m sahabino.crawler scheduler
```

The scheduler:

- runs once immediately;
- then runs at `SAHABINO_PLAYSTORE_CRAWL_INTERVAL_MINUTES`;
- records trigger type `scheduled`;
- coalesces missed APScheduler runs;
- permits only one APScheduler instance of the job;
- uses an additional non-blocking in-process lock to skip overlap;
- releases the lock even when a crawl raises.

## 2. Required infrastructure

The crawler expects:

- PostgreSQL reachable through `SAHABINO_DATABASE_URL`;
- Kafka reachable through `SAHABINO_KAFKA_BOOTSTRAP_SERVERS`;
- the Sahabino API/Application Registry reachable through `SAHABINO_APPLICATION_REGISTRY_BASE_URL`;
- outbound connectivity to Google Play, directly and/or through configured proxies.

Normal crawler startup intentionally does **not**:

- run Alembic migrations;
- create Kafka topics.

Those are deployment/provisioning responsibilities.

## 3. Recommended local startup sequence

Using Docker for infrastructure and running the API/crawler commands from the host:

```bash
docker compose up -d postgres kafka
uv run alembic upgrade head
uv run python -m sahabino.messaging.admin
uv run uvicorn sahabino.main:app --reload
```

Then, in another terminal:

```bash
uv run python -m sahabino.crawler crawl-once
```

or:

```bash
uv run python -m sahabino.crawler scheduler
```

For controlled manual testing, avoid running the scheduler concurrently with a manual crawl unless overlap is intentional.

## 4. Docker Compose deployment

The repository defines `postgres`, `kafka`, `api`, and `crawler` services.

Recommended sequence from the repository README:

```bash
docker compose build api crawler
docker compose up -d postgres kafka
docker compose run --rm api uv run --no-sync alembic upgrade head
docker compose run --rm api uv run --no-sync python -m sahabino.messaging.admin
docker compose up -d api crawler
```

### Connectivity inside Compose

- API -> PostgreSQL using the `postgres` service hostname.
- Crawler -> API using `http://api:8000`.
- Crawler -> Kafka using `kafka:19092`.
- Host -> Kafka using `localhost:9092`.

Kafka auto-topic creation is disabled.

```mermaid
flowchart LR
    HOST[Developer host]
    PG[(postgres:5432)]
    K[(kafka\nHOST 9092 / INTERNAL 19092)]
    API[api:8000]
    C[crawler scheduler]
    GP[Google Play]

    HOST -->|localhost:5432| PG
    HOST -->|localhost:9092| K
    HOST -->|localhost:8000| API
    API --> PG
    C --> API
    C --> PG
    C -->|kafka:19092| K
    C --> GP
```

## 5. Configuration reference

Pydantic Settings uses the `SAHABINO_` prefix and optionally reads `.env`.

### Core dependencies

| Environment variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_DATABASE_URL` | required | SQLAlchemy database URL |
| `SAHABINO_KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka bootstrap servers |
| `SAHABINO_APPLICATION_REGISTRY_BASE_URL` | `http://localhost:8000` | Base URL used by crawler Registry HTTP client |

### Kafka settings

| Variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_KAFKA_TOPIC_PARTITIONS` | `3` | topology used by explicit provisioning tools |
| `SAHABINO_KAFKA_TOPIC_REPLICATION_FACTOR` | `1` | topic replication factor |
| `SAHABINO_KAFKA_CONSUMER_AUTO_OFFSET_RESET` | `earliest` | shared consumer setting |
| `SAHABINO_KAFKA_PRODUCER_QUEUE_FULL_MAX_RETRIES` | `3` | bounded retries while local producer queue is full |
| `SAHABINO_KAFKA_PRODUCER_QUEUE_FULL_POLL_TIMEOUT_SECONDS` | `0.1` | poll time used to service producer queue callbacks |

### Scheduling and concurrency

| Variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_PLAYSTORE_CRAWL_INTERVAL_MINUTES` | `60` | scheduler interval, minimum 1 |
| `SAHABINO_PLAYSTORE_MAX_CONCURRENT_APPS` | `3` | max application commands active concurrently; `1` is serial mode |
| `SAHABINO_PLAYSTORE_LANGUAGE_CODE` | `en` | 2-3 lowercase letter language code |
| `SAHABINO_PLAYSTORE_COUNTRY_CODE` | `us` | 2 lowercase letter country code |

### Request and retry

| Variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_PLAYSTORE_REQUEST_TIMEOUT_SECONDS` | `20` | controlled primary physical request timeout |
| `SAHABINO_PLAYSTORE_RETRY_MAX_ATTEMPTS` | `3` | total primary logical attempts |
| `SAHABINO_PLAYSTORE_RETRY_MAX_DELAY_SECONDS` | `30` | cap for local exponential+jitter backoff; server Retry-After may be longer |

### Global primary rate limiter

| Variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_PLAYSTORE_RATE_LIMIT_ENABLED` | `true` | choose token bucket vs no-op limiter |
| `SAHABINO_PLAYSTORE_RATE_LIMIT_REFILL_PER_SECOND` | `1.0` | token refill rate |
| `SAHABINO_PLAYSTORE_RATE_LIMIT_BURST_CAPACITY` | `2` | max stored tokens / initial burst |

The limiter's local `maximum_wait_seconds` is currently the class default of 60 seconds and has no dedicated environment setting in the current composition root.

### Proxy settings

| Variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_PLAYSTORE_PROXY_ENABLED` | `false` | use PoolProxyProvider instead of NoProxyProvider |
| `SAHABINO_PLAYSTORE_PROXY_URLS` | `[]` | JSON list of proxy URLs, stored as `SecretStr` at settings boundary |
| `SAHABINO_PLAYSTORE_PROXY_DIRECT_FALLBACK` | `true` | allow controlled primary to fall back to direct when no proxy is usable |
| `SAHABINO_PLAYSTORE_PROXY_FAILURE_THRESHOLD` | `2` | generic endpoint failures before cooldown |
| `SAHABINO_PLAYSTORE_PROXY_COOLDOWN_SECONDS` | `60` | cooldown duration |
| `SAHABINO_PLAYSTORE_PROXY_RATE_LIMIT_ROTATE_AFTER` | `2` | per-egress 429 count before proxied cooldown/rotation |

Example:

```bash
export SAHABINO_PLAYSTORE_PROXY_ENABLED=true
export SAHABINO_PLAYSTORE_PROXY_URLS='["http://user:password@proxy.example:8080"]'
```

Do not commit real credentials. HTTP proxies are supported by the current pass-through design; the transport passes the URL to `curl_cffi`. The project does not require SOCKS specifically.

Proxy-mode validation rejects a configuration where proxy mode is enabled, the list is empty, and direct fallback is also disabled.

### Circuit breaker

| Variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_ENABLED` | `true` | enable global Google Play circuit |
| `SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | consecutive relevant failed logical operations to OPEN |
| `SAHABINO_PLAYSTORE_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `60` | OPEN cooldown before one HALF_OPEN probe |

### Secondary adapter

| Variable | Default | Meaning |
| --- | ---: | --- |
| `SAHABINO_PLAYSTORE_SECONDARY_ADAPTER_ENABLED` | `true` | enable narrow parser/schema/adapter fallback |

Disabling secondary does not change retry, proxy, or circuit behavior. It only changes fallback eligibility after a primary adapter-specific failure.

## 6. Database provisioning and lifecycle inspection

### Migrations

```bash
uv run alembic upgrade head
```

Crawler migration creates:

- `crawl_runs`
- `crawl_tasks`

with constraints/indexes and foreign keys to `crawl_runs` and `applications`.

### Useful lifecycle interpretation

A task with:

```text
status = failed
attempt_count = 0
error_code = CIRCUIT_OPEN
```

is plausible: the circuit denied the logical operation before the first attempt hook.

A task with multiple attempts indicates primary retries and/or a secondary fallback. Secondary fallback counts as another logical attempt.

A run with zero active applications is finalized `succeeded` with zero tasks.

A Registry failure produces a failed run with no tasks because active application discovery happens before task creation.

## 7. Kafka provisioning and contracts

Provision topics explicitly:

```bash
uv run python -m sahabino.messaging.admin
```

Crawler topics:

```text
playstore.app-stats.v1
playstore.review-observed.v1
```

The shared project also defines `network.analysis-collected.v1`, but it is not produced by the Google Play crawler collection path documented here.

### App stats event

- event type: `playstore.app_stats.collected`
- schema version: `1`
- key: application UUID string
- one event per successful app-details task

### Review event

- event type: `playstore.review.observed`
- schema version: `1`
- key: application UUID string
- one event per review

### Delivery boundary

The crawler publisher calls the synchronous producer's batch publication/flush boundary. A producer/delivery failure is translated to `MessagingPublishFailure`, causing the corresponding crawler task to fail.

## 8. Failure behavior quick reference

| Symptom | Domain meaning | Retry? | Proxy action? | Secondary? | Circuit relevance? |
| --- | --- | --- | --- | --- | --- |
| Google 429 | `RateLimited` | yes | dedicated counter; rotate proxy after threshold | no | yes after logical failure |
| Proxied 403 | `AccessForbidden` | only with new egress | cooldown + bounded rotate | no | no (proxy-attributed) |
| Direct 403 | `AccessForbidden` | no | none | no | yes |
| Proxied 407 | `ProxyAuthenticationFailure` | with new egress | unhealthy + rotate | no | no |
| Timeout on proxy | `NetworkTimeout` or classified proxy failure depending boundary | yes | generic endpoint health | no | proxied timeout excluded |
| Direct timeout | `NetworkTimeout` | yes | none | no | yes after exhaustion |
| 502/504 | `GatewayFailure` | yes | generic endpoint health can rotate | no | yes after exhaustion |
| 503 | `UpstreamFailure` | yes; respect Retry-After | no generic proxy accounting | no | yes after exhaustion |
| 404 | `AppNotFound` | no | none | no | no |
| Parser/schema/known adapter failure | corresponding adapter error | no repeated primary retry | none | yes if enabled | no for primary adapter failure |
| Local token wait exceeded | `LocalRateLimitWaitExceeded` | no | none | no | no |
| Kafka delivery failure | `MessagingPublishFailure` | no Play Store retry | none | no | not a Google Play circuit signal |

## 9. Proxy operations

### Direct-only configuration

Default proxy disabled:

```bash
SAHABINO_PLAYSTORE_PROXY_ENABLED=false
```

`NoProxyProvider` still creates/reuses direct leases so the same application lifecycle code runs without proxy-specific branches.

### Pool with direct fallback

```bash
SAHABINO_PLAYSTORE_PROXY_ENABLED=true
SAHABINO_PLAYSTORE_PROXY_URLS='["http://proxy1:8080","http://proxy2:8080"]'
SAHABINO_PLAYSTORE_PROXY_DIRECT_FALLBACK=true
```

If all proxies are unusable, the primary controlled stack can use direct egress. This is **not** the secondary adapter; it remains the primary controlled adapter with no proxy argument.

### Unhealthy proxy behavior

A 407 marks an endpoint `UNHEALTHY`. Current v1 has no background recovery mechanism. Restart/rebuild or future management behavior is required to reintroduce an unhealthy endpoint.

## 10. External validation

### Real direct primary smoke

```bash
SAHABINO_RUN_EXTERNAL_PLAYSTORE_SMOKE=1 \
  uv run pytest tests/external/test_controlled_primary_smoke.py \
  -k controlled_primary_stack_against_one_public_package -v
```

### Real proxy smoke

```bash
SAHABINO_RUN_EXTERNAL_PLAYSTORE_SMOKE=1 \
SAHABINO_EXTERNAL_PROXY_URL='http://user:password@proxy.example:8080' \
  uv run pytest tests/external/test_controlled_primary_smoke.py \
  -k configured_real_proxy -v
```

These tests are intentionally opt-in because live Google/proxy availability should not determine normal CI health.

## 11. Test and health commands

```bash
uv run pytest tests/unit/crawler
uv run pytest tests/integration/crawler
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## 12. Operational troubleshooting

### Crawler creates a run but no tasks

Check:

1. Is the Registry reachable?
2. Does `/applications?active=true` return HTTP 200 and a list?
3. Was the run marked `failed`?
4. Are there actually active applications?

A Registry failure happens before tasks exist. Zero active applications is not an error; it produces a successful zero-task run.

### Task fails with Kafka publish error

Collection may have succeeded but delivery did not cross the required Kafka flush boundary. Check:

- broker reachability;
- topic provisioning;
- producer delivery errors;
- advertised listeners/host vs container bootstrap addresses.

The task is correctly considered failed because collected data was not confirmed delivered.

### Repeated proxy rotation

Inspect the failure type rather than treating all rotations the same:

- 403 -> endpoint cooldown immediately;
- 407 -> endpoint unhealthy;
- repeated 429 -> dedicated rate-limit threshold;
- repeated connection/gateway failures -> generic endpoint threshold.

Also check whether direct fallback is enabled.

### Circuit is open

A circuit-open operation sends no primary request. Investigate recent terminal logical failures such as 5xx, repeated rate limiting, or direct connectivity/access failures. Wait for configured cooldown or fix upstream conditions; the circuit permits one HALF_OPEN probe after cooldown.

### Primary parser suddenly fails after dependency change

Verify the exact package versions in `pyproject.toml`/lock:

```text
gplay-scraper == 1.0.6
google-play-scraper == 1.2.7
```

Primary runtime intentionally rejects a different `gplay-scraper` version because private integration points are used.

### Foreign key cannot resolve `applications`

Current crawler persistence models import Registry models specifically to register `applications` in shared SQLAlchemy metadata. The subprocess regression test `test_persistence_metadata.py` should remain green. If this error reappears, check model import/metadata registration rather than only migration state.

## 13. Logging/observability note

The current CLI still prints a manual run ID directly and contains a TODO for a logging system. Operational observability currently comes primarily from:

- persisted crawl run/task lifecycle;
- safe error code/message;
- process/pytest output;
- Kafka behavior.

A structured logging/metrics layer would be a future operational improvement, not something documented here as already implemented.
