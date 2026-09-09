# Google Play crawler architecture

The crawler is independently runnable and uses constructor injection at one
composition root. Domain DTOs and errors import no scraper, transport, Kafka,
HTTP, scheduler, Tenacity, or SQLAlchemy implementation. Application commands
depend on ports and policy objects. Infrastructure implements those ports.

## Ownership and flow

```text
Scheduler/manual command
  -> Registry HTTP port: GET /applications?active=true
  -> PostgreSQL: crawl_runs and crawl_tasks only
  -> bounded application command pool
  -> resilient primary/secondary adapter policy
  -> normalized DTOs
  -> Kafka delivery-confirmed operation batch
```

Crawler lifecycle state belongs in PostgreSQL. Collected Play Store data belongs
in Kafka. App snapshots, review identity/revisions/observations, and analytics
persistence belong to future ingestion. Partial Kafka publication is possible;
delivery is at-least-once and there is intentionally no cross-system transaction,
outbox, or Kafka exactly-once transaction.

## Lifecycle state

A run starts as `running`. Each application gets unique `app_details` and
`reviews` tasks in `pending`. A logical attempt transitions a task to `running`
and increments `attempt_count`; a retry passes through `retrying`. Terminal states
are `succeeded` or `failed`, with a safe error code/message on failure. A
secondary-adapter fallback is another logical attempt, while internal review-page
requests are not lifecycle attempts.

All succeeded tasks make the run `succeeded`; mixed terminal results make it
`partially_failed`; all failed tasks make it `failed`; zero active applications
make it `succeeded`. Registry exhaustion fails the run without creating tasks.
Every final run and terminal task gets `finished_at`.

## Independent controls

These controls are intentionally different and composable:

- Application concurrency limits in-flight application commands.
- The global Token Bucket paces every primary physical request, including retries
  and sequential review pages.
- Tenacity owns bounded retry attempts and exponential/jitter/`Retry-After` waits.
- Proxy health owns sticky leases, failure thresholds, cooldown, and rotation.
- Adapter fallback selects the direct-only secondary only for parser/schema/known
  implementation failures.
- The Circuit Breaker gates likely Google Play outages, not individual proxy health.

There is no per-proxy rate limiter in v1. The configured global rate is a local
defensive policy, not an official Google quota. Failure to obtain a local token
raises `LocalRateLimitWaitExceeded` before transmission and does not affect proxy
health, retries, fallback, or the upstream circuit.

## Networking and timestamps

Each application context owns its proxy lease, `NetworkContext`, `curl_cffi`
session, controlled gplay client, and primary adapter. Rotation closes that stack
and constructs a new one. The pinned gplay parser never gets access to its normal
multi-backend fallback path.

HTTP responses retain their network meaning. A proxied 403 cools the current
endpoint and uses a bounded retry on different egress; a direct 403 is terminal.
A 407 marks the proxy `UNHEALTHY` and rotates boundedly. The first 429 waits and
retries on the same egress; only repeated failures cross the configured proxy
threshold. Timeouts and 502/504 may similarly rotate after that threshold. A 503
honors `Retry-After`; 404, 410, 451, and other non-transient 4xx responses are
terminal. No HTTP/network response selects the secondary adapter.

Proxy states are `HEALTHY` (usable), `COOLDOWN` (temporarily ineligible),
`UNHEALTHY` (unavailable until explicit recovery), and `DISABLED`
(administratively unavailable). V1 has no background health checker, so an
unhealthy proxy remains unavailable for the life of the process.

App update precision is represented as `store_updated_on: date | None`. Numeric
timestamps first become UTC and then a date; date-only upstream values remain
dates. Review Unix timestamps become timezone-aware UTC datetimes directly from
their raw values. Machine-local timezone interpretation is never used.

## Kafka contracts

| Event | Topic | Payload | Key |
| --- | --- | --- | --- |
| `playstore.app_stats.collected` | `playstore.app-stats.v1` | `AppStatsCollectedV1` | application UUID as UTF-8 |
| `playstore.review.observed` | `playstore.review-observed.v1` | `ReviewObservedV1` | application UUID as UTF-8 |

Both use `EventEnvelope`, schema version 1. Reviews are published as individual
events. A task succeeds only after scraping, DTO validation, and the Kafka batch
flush/delivery boundary succeed.

Normal crawler startup only produces and does not require Kafka Admin privileges.
Provision topics explicitly before deployment with:

```bash
python -m sahabino.messaging.admin
```

The real controlled-primary smoke remains opt-in and is skipped by default:

```bash
SAHABINO_RUN_EXTERNAL_PLAYSTORE_SMOKE=1 uv run pytest tests/external/test_controlled_primary_smoke.py
```
