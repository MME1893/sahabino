# Full Runtime Smoke Test

Sahabino includes a runtime smoke test that validates the deployed local Docker
Compose pipeline as one connected system:

```text
Application Registry
        ↓
real crawler
        ↓
Google Play
        ↓
crawler lifecycle persistence
        ↓
Kafka
        ↓
ingestion worker
        ↓
PostgreSQL analytical persistence
        ↓
structured Docker logs
        ↓
Grafana Alloy
        ↓
Loki
        ↓
Grafana provisioning
```

The script is intended for local acceptance testing, demos, and pre-merge or
pre-delivery verification. It complements the automated unit and integration
test suites; it does not replace them.

The script lives at:

```text
scripts/smoke-full-pipeline.sh
```

Run it from the repository root with:

```bash
bash scripts/smoke-full-pipeline.sh
```

On Unix-like systems the file may also be marked executable:

```bash
chmod +x scripts/smoke-full-pipeline.sh
./scripts/smoke-full-pipeline.sh
```

Using `bash ...` is the most portable option for the project's current Windows
Git Bash development workflow.

---

## Purpose and test boundaries

The smoke test exercises the real Docker Compose runtime. It intentionally uses:

- the real PostgreSQL container and persistent volume;
- the real Kafka broker and persistent volume;
- the real FastAPI Application Registry service;
- the real crawler process;
- the real Google Play adapters and network path;
- the real crawler lifecycle repository;
- the real crawler Kafka publisher;
- the real ingestion worker;
- the real ingestion PostgreSQL persistence;
- the real Alloy → Loki logging path;
- the provisioned Grafana Loki datasource and smoke dashboard files.

The smoke test does **not** replace deterministic pytest coverage.

The automated integration suite should remain responsible for deterministic
component-level behavior such as:

```text
Fake Google Play
→ real CrawlerService
→ real PostgreSQL
→ real Kafka
→ real IngestionWorker
→ real PostgreSQL
```

The runtime smoke test goes further by exercising Docker Compose and the real
external Google Play boundary.

Because it contacts Google Play, do not run this script as part of ordinary CI
or every local edit cycle.

---

## Prerequisites

Before running the script, the repository should contain:

- a valid `.env`;
- Docker Desktop / Docker Engine running;
- Docker Compose available as `docker compose`;
- the Sahabino project images already built, unless `--build` is used;
- the current Alembic migrations;
- the current Kafka topic administration command;
- the centralized logging stack files under
  `infrastructure/observability/`.

The script also expects the host to provide common shell tools:

```text
bash
curl
grep
awk
sed
```

The script checks Docker availability and validates both normal and
`observability` Compose configurations before starting the flow.

---

## Recommended first run

If the project images already contain the current source code:

```bash
bash scripts/smoke-full-pipeline.sh
```

If Python source, dependencies, or the Dockerfile changed after the last image
build:

```bash
bash scripts/smoke-full-pipeline.sh --build
```

The `--build` path uses the repository's Docker/uv caching strategy. Normal
runtime startup uses `--no-build` so a smoke test does not rebuild images or
redownload Python dependencies unnecessarily.

If the Registry has no active applications, use:

```bash
bash scripts/smoke-full-pipeline.sh --seed
```

This seeds Telegram and WhatsApp only when their package names do not already
exist.

---

## Command-line options

### Default

```bash
bash scripts/smoke-full-pipeline.sh
```

Runs the full runtime smoke test using existing Docker images and includes
observability verification.

### `--build`

```bash
bash scripts/smoke-full-pipeline.sh --build
```

Builds the API, crawler, and ingestion images before starting the smoke test.

Use this after source or dependency changes.

The Dockerfile is structured so third-party uv dependencies stay cached while
normal source-code changes only invalidate the inexpensive project layers.

### `--seed`

```bash
bash scripts/smoke-full-pipeline.sh --seed
```

Ensures the following applications exist and are active:

```text
org.telegram.messenger
com.whatsapp
```

If either package already exists and is active, it is left unchanged.

If the package exists but is inactive, the script fails instead of mutating
state silently because the current Registry API does not expose a dedicated
reactivation endpoint.

### `--skip-observability`

```bash
bash scripts/smoke-full-pipeline.sh --skip-observability
```

Runs the core business/data flow only:

```text
Registry
→ crawler
→ Kafka
→ ingestion
→ PostgreSQL
```

Loki, Alloy, and Grafana checks are skipped.

This is useful when validating the ingestion pipeline without bringing up the
observability profile.

### `--down-after`

```bash
bash scripts/smoke-full-pipeline.sh --down-after
```

Stops Compose containers after a successful smoke run.

It uses normal Compose shutdown behavior and **does not remove named volumes**.

It intentionally does not run:

```bash
docker compose down -v
```

Therefore PostgreSQL, Kafka, Loki, Grafana, and Alloy volume data are preserved.

Options may be combined, for example:

```bash
bash scripts/smoke-full-pipeline.sh --build --seed --down-after
```

---

## Environment overrides

The following optional environment variables control smoke-test behavior.

### Timeout

Default:

```text
SMOKE_TIMEOUT_SECONDS=180
```

Override example:

```bash
SMOKE_TIMEOUT_SECONDS=300 \
  bash scripts/smoke-full-pipeline.sh
```

Increase this on a slow machine, slow disk, slow Kafka startup, or slow external
Google Play connection.

### Require review events

Default:

```text
SMOKE_REQUIRE_REVIEWS=1
```

With the default, a successful smoke crawl must produce at least one
`ReviewObserved` Kafka record.

For a deliberately chosen application with no available reviews:

```bash
SMOKE_REQUIRE_REVIEWS=0 \
  bash scripts/smoke-full-pipeline.sh
```

Do not disable this merely to hide a crawler regression.

### Temporary crawler log discovery window

Default:

```text
SMOKE_CRAWLER_LOG_DISCOVERY_SECONDS=12
```

If the one-shot crawler container exits before Alloy discovers it, the script
emits one dedicated structured crawler smoke record and keeps that temporary
container alive for this many seconds.

Override example:

```bash
SMOKE_CRAWLER_LOG_DISCOVERY_SECONDS=20 \
  bash scripts/smoke-full-pipeline.sh
```

---

# What the script verifies

## 1. Compose and Docker preflight

The script verifies:

```text
Docker daemon available
docker compose available
docker compose config valid
docker compose --profile observability config valid
```

Failure at this stage means the runtime environment or Compose configuration is
not ready.

---

## 2. PostgreSQL and Kafka startup

The script starts:

```text
postgres
kafka
```

without rebuilding images.

It waits for PostgreSQL using `pg_isready` and verifies the Kafka broker through
Kafka's broker API tooling.

This is stronger than merely checking whether a container is in the `Up` state.

---

## 3. Crawler scheduler isolation

If the long-running crawler scheduler is already running, the script temporarily
stops it.

This prevents an unrelated scheduled crawl from modifying Kafka offsets or
database counts while the smoke test is measuring its baseline.

The scheduler is restored to its original running state when the smoke flow
finishes.

If the smoke test is interrupted after the script stopped a previously running
scheduler, cleanup also attempts to restore it.

---

## 4. Alembic schema verification

The smoke test explicitly executes:

```bash
uv run --no-sync alembic upgrade head
```

inside the project image.

It then compares:

```text
Alembic code head
vs.
alembic_version in PostgreSQL
```

The two revisions must match.

It also verifies the existence of the required production tables:

```text
applications
crawl_runs
crawl_tasks
ingested_events
playstore_app_snapshots
reviews
review_observations
```

The API, crawler, and ingestion processes do not run migrations implicitly; the
smoke script preserves that explicit migration boundary.

---

## 5. Kafka topic provisioning

The smoke test invokes the existing administration command:

```bash
uv run --no-sync python -m sahabino.messaging.admin
```

The following topics must exist:

```text
playstore.app-stats.v1
playstore.review-observed.v1
network.analysis-collected.v1
```

Kafka automatic topic creation remains disabled.

The network topic is verified as part of the platform topology even though the
current ingestion worker intentionally does not consume it yet.

---

## 6. API and ingestion startup

The script starts:

```text
api
ingestion
```

and, unless observability is skipped:

```text
loki
alloy
grafana
```

It verifies the API through its HTTP documentation endpoint and reads the actual
ingestion consumer-group configuration from the running ingestion container.

The normal default is:

```text
sahabino-ingestion-v1
```

but the smoke test does not hardcode that value when verifying runtime behavior.

---

## 7. Observability provisioning regression checks

When observability is enabled, the script verifies:

```text
Loki /ready
Grafana /api/health
Alloy container running
```

It also guards two previously discovered logging regressions.

### Docker log envelope parsing

The Alloy configuration must contain:

```alloy
stage.docker {}
```

Docker writes application stdout inside the Docker log envelope. Without this
stage, application JSON fields such as `service`, `environment`, and `level`
would not be parsed correctly before Loki labels are extracted.

### Loki-safe dashboard "All" variables

The Grafana smoke dashboard must not contain:

```json
"allValue": ".*"
```

for Loki label selectors.

Loki rejects a selector when all matchers can match an empty value. The dashboard
uses the non-empty-compatible value:

```json
"allValue": ".+"
```

instead.

The script also verifies that the Loki datasource uses the expected UID:

```text
sahabino-loki
```

and that the logging smoke dashboard file is present.

---

## 8. Application Registry verification

The script verifies the real API endpoints:

```text
GET /applications?active=true
GET /categories
```

At least one active application is required for a real crawler run.

Without `--seed`, an empty active application set causes a clear failure.

With `--seed`, Telegram and WhatsApp are created only when absent.

---

## 9. Drain any old ingestion backlog

Before measuring the smoke test, the ingestion worker is allowed to consume any
records left from previous runs.

The script waits until the configured ingestion consumer group is caught up.

This establishes a stable starting point and avoids incorrectly attributing old
Kafka records to the new smoke crawl.

---

## 10. Stable Kafka and database baseline

After old backlog is drained, the script stops the ingestion worker.

It records Kafka topic end offsets for:

```text
playstore.app-stats.v1
playstore.review-observed.v1
```

and database counts for:

```text
ingested_events
playstore_app_snapshots
reviews
review_observations
```

Stopping ingestion before the baseline is important.

The crawler can now produce a measurable Kafka backlog without the consumer
racing to remove it before the test observes the delta.

---

## 11. Controlled real crawler run

The script executes one manual crawler cycle:

```bash
uv run --no-sync python -m sahabino.crawler crawl-once
```

This uses the project's real runtime crawler and real Google Play network path.

The crawler prints its `crawl_run_id`; the script extracts that UUID and uses it
for run-specific database assertions later.

The long-running scheduler is not used for the smoke crawl.

---

## 12. Crawler lifecycle persistence

For the extracted `crawl_run_id`, the script requires:

```text
crawl_run.status = succeeded
```

For every active application it expects exactly:

```text
1 app_details task
1 reviews task
```

and every task must be:

```text
succeeded
```

Therefore, for `N` active applications:

```text
expected tasks = N × 2
```

This confirms the real crawler lifecycle path before ingestion is allowed to
consume anything.

---

## 13. Crawler → Kafka verification

With ingestion still stopped, the script reads Kafka end offsets again.

Expected semantics:

```text
new AppStats records
    = number of active applications
```

and, by default:

```text
new ReviewObserved records
    > 0
```

The review count is intentionally not hardcoded to 100. It depends on the
external data returned by Google Play and the crawler's configured limit.

The total Kafka delta is:

```text
AppStats delta + ReviewObserved delta
```

The script also checks consumer-group lag when Kafka exposes a committed offset
for the affected partitions.

For newly used partitions Kafka may temporarily report no prior committed
offset. In that case, the script treats the topic end-offset delta as the
authoritative proof that the crawler produced records instead of falsely
failing the smoke test.

---

## 14. Kafka → ingestion → PostgreSQL verification

The ingestion worker is restarted.

The script waits until database totals reach the exact targets implied by the
new Kafka delta.

It requires:

```text
ingested_events delta
    = total new Kafka records
```

```text
playstore_app_snapshots delta
    = AppStats Kafka delta
```

```text
review_observations delta
    = ReviewObserved Kafka delta
```

It then waits until Kafka consumer lag returns to:

```text
0
```

This proves the successful runtime sequence:

```text
crawler publishes
→ Kafka stores
→ ingestion consumes
→ PostgreSQL commits
→ Kafka offset commits
```

---

## 15. Why `reviews` row count is different

The script deliberately does **not** require:

```text
reviews delta = ReviewObserved event count
```

The current model is:

```text
reviews
→ latest/current state

review_observations
→ per-crawl historical observation
```

A review that was already known can be observed again during a later crawl.

That produces:

```text
new review_observations row
```

while the stable current row in:

```text
reviews
```

is updated/upserted rather than duplicated.

Therefore the script only requires the current-review row delta to be
non-negative and no greater than the number of new ReviewObserved events.

This matches the intended business model and the manual runtime behavior.

---

## 16. Exact crawl-run database verification

Global database counts are not enough.

The smoke test joins the newly persisted analytical rows back to the crawler
lifecycle task IDs for the exact `crawl_run_id`.

It requires:

```text
run-specific snapshot count
    = new AppStats Kafka event count
```

and:

```text
run-specific review observation count
    = new ReviewObserved Kafka event count
```

This prevents unrelated historical database rows from making the smoke test pass.

---

## 17. Alloy → Loki runtime verification

The script does not stop at checking that Loki is "ready".

It queries Loki for structured logs from:

```text
sahabino-api
sahabino-ingestion
sahabino-crawler
```

This verifies that Docker labels, Alloy discovery, Docker envelope decoding,
structured JSON parsing, Loki labeling, and Loki ingestion work together.

The equivalent useful manual Explore queries are:

```logql
{service="sahabino-api"}
```

```logql
{service="sahabino-ingestion"}
```

```logql
{service="sahabino-crawler"}
```

For ingestion processing logs:

```logql
{service="sahabino-ingestion"}
| json
| event="ingestion.message.processed"
```

---

## 18. Temporary crawler-container logging behavior

A one-shot crawler is launched through:

```bash
docker compose run --rm crawler ...
```

That container can exit and be deleted before Alloy's Docker discovery loop sees
it.

This is a runtime timing characteristic, not necessarily a logging failure.

If no crawler log is visible in Loki after the crawl, the smoke script launches
one short-lived crawler container that:

1. configures the existing centralized logging foundation;
2. emits a structured event:

```text
event = crawler.smoke
```

3. includes the smoke crawl's `crawl_run_id`;
4. stays alive briefly so Alloy can discover and tail it.

The script then requires the `sahabino-crawler` Loki stream to exist.

No smoke-only code is added to the application source.

---

# Safety and side effects

## Named volumes are preserved

The script never intentionally runs:

```bash
docker compose down -v
```

It does not erase the PostgreSQL, Kafka, Loki, Grafana, or Alloy named volumes.

## Existing data is preserved

The smoke test is **not destructive**, but it is stateful.

A successful run adds normal runtime state, including:

```text
crawl_runs rows
crawl_tasks rows
Kafka records
ingested_events rows
playstore_app_snapshots rows
review/review_observation updates
Loki log data
```

This data is intentionally left in place.

Run the smoke test against a local development environment, not a production
database.

## `--seed` changes Registry state

When requested, `--seed` may create Telegram and WhatsApp registry records.

Without `--seed`, no application is created by the smoke script.

## Real Google Play traffic

The crawler phase performs real network requests to Google Play for every active
application.

If many applications are active, the smoke test can produce substantially more
network traffic and runtime data than a two-application demo.

For a compact acceptance run, keep the active Registry set intentionally small.

---

# Docker and uv cache behavior

The smoke test is cache-friendly by default.

Normal service startup uses:

```text
--no-build
```

so simply running the smoke script does not rebuild project images.

Use:

```bash
bash scripts/smoke-full-pipeline.sh --build
```

only when the project image must reflect new source or dependency changes.

The project Dockerfile isolates:

```text
pyproject.toml
uv.lock
```

from normal source-code copies and uses persistent BuildKit cache mounts for uv.

As a result, changing ordinary Python source should not force third-party
dependencies to be redownloaded.

Avoid unnecessary cache-destroying commands such as:

```bash
docker compose build --no-cache
docker compose pull
docker image prune -a
docker builder prune
docker compose down --rmi all
```

during normal development unless their behavior is explicitly desired.

---

# Expected successful summary

A successful run ends with a summary similar to:

```text
crawl_run_id:            <uuid>
active applications:     2

Kafka new app events:     2
Kafka new review events:  200
Kafka final group lag:    0

DB new ingested_events:   202
DB new snapshots:         2
DB new observations:      200

API:                      PASS
PostgreSQL/Alembic:       PASS
Kafka topics:             PASS
Crawler lifecycle:        PASS
Crawler -> Kafka:         PASS
Kafka -> Ingestion:       PASS
Kafka offset commits:     PASS
Ingestion persistence:    PASS
Alloy -> Loki:            PASS
Grafana provisioning:     PASS

FULL SAHABINO SMOKE TEST: PASS
```

Exact review counts depend on real Google Play responses and should not be
assumed to be 100 per application.

---

# Troubleshooting

## Project images do not contain recent source changes

Symptom:

- behavior does not match the current checkout;
- a recently added module cannot be imported;
- old logging or ingestion behavior appears.

Run:

```bash
bash scripts/smoke-full-pipeline.sh --build
```

Do not use `--no-cache` unless diagnosing an actual Docker cache problem.

---

## No active applications

Symptom:

```text
No active applications
```

Either create applications through the Registry API or run:

```bash
bash scripts/smoke-full-pipeline.sh --seed
```

---

## Seed package exists but is inactive

The current Registry delete operation soft-deactivates applications and the
current API does not expose a dedicated reactivation endpoint.

If the seed package exists but is inactive, the script fails instead of silently
creating a duplicate or mutating the database directly.

Reactivate the application through an appropriate project-supported path or use
another active application before rerunning the smoke test.

---

## Kafka CLI paths are rewritten by Git Bash

Running a command such as:

```bash
docker compose exec kafka /opt/kafka/bin/kafka-topics.sh ...
```

directly from Git Bash may produce a host-path rewrite similar to:

```text
C:/Program Files/Git/opt/kafka/...
```

Use the container shell form:

```bash
docker compose exec kafka sh -lc \
  '/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:19092 --list'
```

The smoke script already uses this safe form internally.

---

## Ingestion does not catch up

Inspect:

```bash
docker compose logs --tail=100 ingestion
```

and:

```bash
docker compose exec kafka sh -lc \
  '/opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:19092 \
    --describe \
    --group sahabino-ingestion-v1'
```

A valid runtime should converge to:

```text
LAG = 0
```

for all assigned Play Store topic partitions.

If ingestion exits because a valid message caused a database/business failure,
do not reset offsets merely to make the smoke test pass. Fix the underlying
consistency failure.

---

## No ReviewObserved events were produced

By default this is a failure because the smoke test is intended to validate both
Play Store event types.

Inspect crawler logs and selected applications first.

Only when zero reviews are a legitimate property of the selected test
application, run:

```bash
SMOKE_REQUIRE_REVIEWS=0 \
  bash scripts/smoke-full-pipeline.sh
```

---

## Smoke test times out

Increase the bounded deadline:

```bash
SMOKE_TIMEOUT_SECONDS=300 \
  bash scripts/smoke-full-pipeline.sh
```

Before increasing it repeatedly, inspect Docker health and service logs to rule
out an actual startup or processing failure.

---

## Ingestion logs are visible but crawler logs are absent in Loki

This can happen because the crawler smoke run uses an ephemeral `--rm`
container.

The script automatically emits a dedicated crawler smoke log and keeps the
container alive briefly for Alloy discovery.

If the environment needs more time:

```bash
SMOKE_CRAWLER_LOG_DISCOVERY_SECONDS=20 \
  bash scripts/smoke-full-pipeline.sh
```

Useful manual Loki query:

```logql
{service="sahabino-crawler"}
| json
| event="crawler.smoke"
```

---

## Loki query returns no application services

Check:

```bash
curl http://localhost:3100/ready
docker compose logs --tail=100 alloy
```

Then verify the Alloy config still contains:

```alloy
stage.docker {}
```

and application services still carry:

```yaml
labels:
  com.sahabino.logs: "true"
```

Useful label check:

```bash
curl http://localhost:3100/loki/api/v1/label/service/values
```

Expected services include:

```text
sahabino-api
sahabino-crawler
sahabino-ingestion
```

---

## Grafana dashboard fails with an empty-compatible Loki matcher

The smoke dashboard variables must use:

```json
"allValue": ".+"
```

not:

```json
"allValue": ".*"
```

A selector made entirely of empty-compatible matchers is rejected by Loki.

---

# Relationship to pytest

Sahabino uses three complementary verification layers.

## Unit tests

```bash
uv run pytest tests/unit
```

Purpose:

```text
logic
failure semantics
validation
small deterministic behavior
```

## Integration tests

```bash
uv run pytest tests/integration
```

Purpose:

```text
production components
+ real PostgreSQL/Testcontainers
+ real Kafka/Testcontainers
+ fake external Google Play
```

The crawler-to-ingestion full-flow integration test lives at:

```text
tests/integration/ingestion/test_crawler_to_ingestion_flow.py
```

It proves:

```text
Fake Google Play
→ real crawler
→ real lifecycle PostgreSQL
→ real Kafka
→ real ingestion
→ real ingestion PostgreSQL
```

without contacting Google Play or depending on Grafana.

## Full runtime smoke test

```bash
bash scripts/smoke-full-pipeline.sh
```

Purpose:

```text
real Docker Compose
+ real Google Play crawler
+ real Kafka
+ real ingestion
+ real PostgreSQL
+ real Alloy/Loki/Grafana
```

These layers deliberately overlap at important boundaries but solve different
problems.

A pytest integration failure should remain deterministic and reproducible
without external Google Play availability.

A runtime smoke failure may legitimately reveal deployment, network, Docker,
Google Play, or observability integration problems that pytest intentionally
does not exercise.

---

# Recommended verification workflow

For normal development:

```bash
uv run pytest tests/unit
```

For crawler/ingestion changes:

```bash
uv run pytest tests/integration/crawler
uv run pytest tests/integration/ingestion
```

Before merging a substantial pipeline change:

```bash
uv run pytest tests/integration
bash scripts/smoke-full-pipeline.sh --build
```

Before a demo or delivery:

```bash
uv run pytest
bash scripts/smoke-full-pipeline.sh
```

Do not enable the real external crawler smoke as a required CI gate unless the
project explicitly accepts an external Google Play dependency in CI.

---

# What a PASS means

A complete PASS demonstrates, for the current local runtime and current external
Google Play availability, that:

- Compose configuration is valid;
- PostgreSQL and Kafka start successfully;
- the database schema is at Alembic head;
- the required Kafka topics exist;
- the Registry API is reachable;
- active applications can be crawled successfully;
- crawler lifecycle persistence succeeds;
- crawler events reach Kafka;
- ingestion processes those Kafka records;
- expected ingestion database effects occur;
- Kafka offsets are committed and group lag returns to zero;
- structured API, crawler, and ingestion logs reach Loki;
- the current Grafana logging provisioning files are present;
- the two known logging configuration regressions remain fixed.

A PASS does **not** claim:

- production high availability;
- Kafka exactly-once semantics;
- external Google Play long-term availability;
- load/performance capacity;
- multi-broker failure tolerance;
- a production deployment topology.

The ingestion delivery contract remains:

> at-least-once Kafka delivery with idempotent database processing.
