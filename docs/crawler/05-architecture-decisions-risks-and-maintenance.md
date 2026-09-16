# 05 - Architecture Decisions, Risks, and Maintenance Notes

## 1. Purpose

This document records decisions and trade-offs that are easy to lose when reading individual files. It is not a formal ADR log with historical dates/approvers; it is a **current decision register** grounded in the supplied implementation.

When a decision becomes contentious or changes materially, promote it to a dedicated ADR in the repository.

## 2. Decision: use a crawler-local Hexagonal Architecture

### Current choice

Domain DTOs/errors remain independent from HTTP, scraper libraries, Kafka, SQLAlchemy, APScheduler, Tenacity, and proxy implementations. Application code uses ports and policy objects; infrastructure implements external capabilities; `build_container` wires concrete objects.

### Why it matters

- Third-party scraper shapes cannot leak through the application contract.
- Policies can be tested without real I/O.
- Network behavior is not hidden inside scraper libraries.
- Persistence and publication can evolve without changing normalized DTOs.

### Cost

More small files/protocols and an explicit composition root.

## 3. Decision: split lifecycle persistence from collected-data transport

### Current choice

PostgreSQL records crawler execution state only. Collected app/review data is emitted to Kafka.

### Consequences

- Crawler run/task observability survives collection failure.
- Downstream ingestion can evolve separately.
- There is no atomic transaction covering both PostgreSQL lifecycle changes and Kafka delivery.
- Partial Kafka publication is possible and delivery must be treated as at-least-once.

### Maintenance rule

Do not add analytical snapshot/review-history writes into the crawler repository accidentally. If architecture changes, make the ownership change explicit.

## 4. Decision: application is the concurrency boundary

### Current choice

One `ApplicationCrawlCommand` is submitted per active application. Details and reviews are sequential inside it.

### Rationale

- keeps per-application network context sticky;
- avoids same-application details/reviews racing each other;
- makes concurrency tunable with one bounded setting;
- allows expected task-level partial failure.

### Consequence

The `context_id` model effectively assumes one owner for an application's open network context at a time. Proxy providers return the same active lease for the same context ID.

## 5. Decision: primary network policy is controlled by Sahabino

### Current choice

The primary adapter uses `gplay-scraper==1.0.6` for parser/scraper logic but bypasses its normal constructor/decorated network behaviors using `object.__new__`, injected `ControlledGPlayHttpClient`, and `inspect.unwrap`.

### Benefits

- every primary physical request uses the Sahabino token bucket;
- timeout/proxy/status semantics are centralized;
- retry remains at application layer;
- rotation rebuilds a clean stack;
- hidden backend fallback is prevented.

### Risk

This is intentionally coupled to private/internal library behavior. The runtime version guard and compatibility tests are mandatory defenses, not optional cleanup.

### Maintenance rule

A dependency upgrade should be treated as an integration change requiring:

1. private API inspection;
2. controlled-transport compatibility tests;
3. real opt-in smoke test;
4. normalization parity checks.

## 6. Decision: secondary adapter is a narrow direct fallback

### Current choice

`google-play-scraper==1.2.7` is used once after parser/schema/known adapter failures, when enabled.

### Explicit non-choice

It is not used after 429, timeout, proxy failure, 5xx, 404, 403/407, or local rate-limit wait errors.

### Rationale

Changing parser implementation can help when an undocumented upstream shape breaks one library. It does not solve an unhealthy network identity or upstream outage.

### Risks

- secondary also uses private library review APIs (`_fetch_review_items`, `ElementSpecs`);
- the direct secondary path bypasses Sahabino's controlled primary transport and global token bucket;
- the wrapper is stateless, but the repository cannot prove all internals of the third-party library are safe under every concurrent live workload.

## 7. Decision: preserve error semantics

### Current choice

HTTP and raw library errors are normalized into a detailed `CrawlerError` hierarchy.

### Rationale

Policies need meaning, not generic failures. For example:

- 429 should retry but never select secondary;
- proxied 403 should rotate but not pollute global Google circuit health;
- parser failure can select secondary without repeated primary retry;
- local rate-limit failure must not look like Google rate limiting.

### Risk: heuristic raw exception classification

Both transport and classifier use some string/type-name heuristics for third-party exceptions. Library error-message changes can alter mapping.

There is a characterized taxonomy difference for text such as `"proxy timeout"`:

- controlled transport checks `timeout` first and returns `NetworkTimeout`;
- generic `ErrorClassifier` checks `proxy` first and returns `ProxyConnectionFailure`.

Current tests intentionally preserve this behavior. Unifying it should be a deliberate policy change, not a drive-by refactor.

## 8. Decision: retry, network health, fallback, and circuit are separate policies

This is one of the most important design choices.

### RetryPolicy

Answers:

- is another primary attempt allowed?
- how long should it wait?

### NetworkPolicy

Answers:

- what should happen to this egress?
- should the proxy be cooled/unhealthy?
- should a replacement lease be used?

### AdapterFallbackPolicy

Answers:

- after primary has failed, does a different scraper implementation make sense?

### CircuitBreaker

Answers:

- should new logical operations be admitted at all based on global Google Play health?

Mixing these concerns tends to create accidental behaviors such as "429 means use another library" or "proxy timeout opens the Google-wide circuit". The current structure explicitly prevents those shortcuts.

## 9. Decision: rate limiting is global for controlled primary physical requests

### Current choice

One token bucket is created in the composition root and shared by all primary transports, including transports rebuilt after proxy rotation.

### Benefits

- application concurrency cannot bypass request pacing;
- new proxy/network stacks do not get fresh quota;
- each retry pays for a new physical request;
- failed transmitted requests do not refund capacity.

### Scope limitation

The secondary direct path is not controlled by this token bucket. Therefore "global" means process-wide across the controlled primary path, not all possible third-party HTTP calls made by the process.

If the architectural requirement later becomes "every Google Play outbound request must be globally paced," the secondary integration must be redesigned rather than merely renamed.

## 10. Decision: proxy health and circuit health are different state machines

### Proxy health

Per endpoint:

```text
HEALTHY / COOLDOWN / UNHEALTHY / DISABLED
```

### Circuit health

Process-wide Google Play state:

```text
CLOSED / OPEN / HALF_OPEN
```

### Rationale

A broken proxy is not the same as a broken upstream. The circuit therefore excludes proxied 403, proxied timeout, and proxied temporary-connection evidence while NetworkPolicy/ProxyPool handle them.

## 11. Decision: unhealthy proxy does not auto-recover in v1

A 407 marks the endpoint `UNHEALTHY`. Only cooldown endpoints lazily recover with time. There is no background health-check or explicit enable/recover path in current runtime wiring.

### Operational consequence

An endpoint incorrectly marked unhealthy remains unavailable for the life of the pool/process.

### Future option

Add an explicit health-check/recovery subsystem only if operational evidence justifies the added complexity. Do not silently make `record_success` revive unhealthy endpoints because that would change the meaning of 407 handling.

## 12. Decision: proxy leases are sticky but not exclusive

Multiple contexts may point to the same `ProxyEndpoint`; a lease is a context-to-egress binding.

Same-context concurrent acquisition returns the same active lease object. Releasing that lease invalidates both references.

### Architectural assumption

One active owner per context ID is the safe usage model.

### If this assumption changes

Choose explicitly between:

- preventing overlapping same-context application commands;
- using unique context IDs per concurrent ownership instance;
- implementing reference-counted/ownership-aware leases.

Do not add reference counting speculatively without a real concurrency requirement.

## 13. Decision: circuit result generation tokens prevent stale races

### Problem

A call admitted while circuit generation N is CLOSED may finish after another call has opened generation N+1. Without call identity, the stale success could close the new circuit or a stale failure could extend the new cooldown.

### Current solution

- `before_call` returns generation token;
- `_open` increments generation;
- `record_success`/`record_failure` ignore mismatched tokens;
- results arriving while already OPEN are ignored.

### Maintenance rule

Any alternate circuit implementation used through `CircuitBreakerPort` must preserve equivalent stale-result safety or consciously change concurrency semantics.

## 14. Decision: strict normalized DTOs form the internal data contract

Third-party libraries return dictionaries/nested lists using their own names/shapes. Adapters must convert these into frozen, validated Sahabino models.

Important data semantics:

- app update is a calendar date, not a fabricated datetime;
- naive source datetimes are rejected instead of interpreted in machine-local time;
- review timestamps become aware UTC datetimes;
- review position is bounded 1..1000;
- source adapter provenance is retained.

### Characterized permissive choices

- unknown optional update-date strings become `None`;
- missing `adSupported` becomes `False` through `bool(raw.get(...))`.

These are current semantics and should not be changed casually because downstream event meaning can change.

## 15. Decision: Registry access is HTTP, not direct crawler business-query access

Crawler orchestration calls the Registry port, implemented by HTTP. The lifecycle database still contains an FK to `applications.id`, but application discovery is via API.

This boundary prevents crawler use cases from depending on Registry ORM/query implementation.

Registry HTTP has its own small bounded retry loop, independent of Google Play retry policy.

## 16. Decision: normal startup has no provisioning side effects

The composition root can provision Kafka topics only with explicit `provision_topics=True`, used for tests/tools. Normal startup does not.

Alembic is never run by crawler startup.

### Rationale

Deployment permissions and lifecycle remain explicit; the crawler does not require Kafka Admin or migration privileges merely to collect data.

## 17. Risk register

### R1 - Private scraper APIs

**Area:** primary and secondary adapters
**Risk:** upstream library changes can break private methods/data indexes.
**Mitigation:** exact version pins, primary runtime guard, compatibility tests, opt-in real smoke tests.
**Severity:** high if dependency is upgraded without validation.

### R2 - Secondary bypasses controlled rate limiter

**Area:** fallback network behavior
**Risk:** secondary request is outside process-wide controlled primary pacing/proxy path.
**Mitigation:** fallback eligibility is narrow and only one secondary logical attempt is made.
**Action if requirement changes:** redesign secondary transport integration.

### R3 - String-based exception translation

**Area:** `CurlCffiTransport` / `ErrorClassifier`
**Risk:** dependency message/type-name changes can alter error taxonomy.
**Mitigation:** characterization matrix tests.
**Possible improvement:** prefer stable typed exception APIs where available without coupling application layers to the dependency.

### R4 - Same-context lease ownership

**Area:** Proxy providers
**Risk:** two independent consumers of the same context share a lease; one release invalidates the other.
**Mitigation:** current application architecture processes one command per application/context.
**Action:** enforce or redesign if overlapping same-context ownership becomes valid.

### R5 - Mutable endpoint objects exposed by `ProxyPool.endpoints`

**Area:** proxy encapsulation
**Risk:** callers can mutate live endpoint state outside pool lock.
**Current use:** tests/inspection.
**Possible improvement:** expose read-only snapshots or dedicated administrative methods if external mutation becomes production behavior.

### R6 - Concrete lease coupling behind Protocols

**Area:** `PrimaryAdapterFactory`, provider `_concrete`, `NetworkContext`
**Risk:** an alternate `ProxyLeasePort` implementation is rejected at infrastructure boundaries.
**Interpretation:** application boundary is abstract; infrastructure is not fully pluggable.
**Action:** widen only when a real alternate lease implementation is required.

### R7 - No unhealthy-proxy recovery

**Area:** proxy state machine
**Risk:** transient 407-like condition can permanently remove endpoint until process rebuild.
**Mitigation:** intended for credential/auth failures; restart/rebuild restores configured pool.
**Possible improvement:** explicit health-check/recovery policy.

### R8 - No DB/Kafka atomicity

**Area:** lifecycle + publication
**Risk:** failure between side effects can produce partial system state.
**Mitigation:** lifecycle reflects delivery failure; producer is reliability-configured; downstream should tolerate at-least-once semantics.
**Future architecture option:** outbox/transactional boundary if consistency requirements justify it.

### R9 - In-process scheduler locking only

**Area:** scheduling
**Risk:** two separate crawler processes can each run the scheduled job.
**Mitigation:** single crawler service instance in intended deployment.
**Action:** distributed/database lock if horizontal scheduler replicas are introduced.

### R10 - Limited runtime observability

**Area:** operations
**Risk:** CLI currently prints run ID and has a TODO for logging; no crawler-specific metrics layer is shown in current source.
**Mitigation:** persisted lifecycle and error codes provide operational evidence.
**Possible improvement:** structured logs and metrics around run duration, attempts, rate limiting, circuit/proxy state, and Kafka failures.

### R11 - Rotation cleanup edge case

**Area:** `ApplicationPlayStoreClient` resource lifecycle
**Current code sequence:** NetworkPolicy/provider can acquire a replacement lease, then client closes the old adapter, then stores the new lease.
**Risk:** if old adapter `close()` raises after provider rotation has already created/stored a replacement lease, the client may still hold the released old lease and cleanup may not release the replacement lease.
**Status:** not represented as fixed in the supplied source; a targeted regression test is a worthwhile hardening task.
**Suggested direction:** make ownership transfer/cleanup exception-safe without changing normal rotation semantics.

## 18. Safe maintenance checklist

Before changing crawler behavior, answer:

1. Is this a logical-operation concern or physical-request concern?
2. Which policy owns the decision?
3. Does the change alter direct vs proxied semantics?
4. Does it change retry count or persisted attempt count?
5. Does it change whether secondary is eligible?
6. Does it change global circuit relevance?
7. Does it change proxy state/rotation?
8. Does it bypass the shared primary rate limiter?
9. Does it change DTO/event semantics?
10. Does it add a new external side effect requiring integration testing?
11. Does it affect resource cleanup when exceptions occur?
12. Does it require an ADR because it changes system ownership rather than implementation detail?

## 19. Recommended ADR candidates

If the project begins maintaining a formal ADR directory, these current decisions are good candidates for dedicated records:

- lifecycle state in PostgreSQL vs collected data in Kafka;
- controlled primary integration with pinned `gplay-scraper` internals;
- direct-only secondary fallback scope;
- separation of Retry/Network/Fallback/Circuit policy ownership;
- application-level concurrency boundary;
- proxy lease/state model and direct fallback;
- process-wide primary token bucket;
- global Google Play circuit semantics;
- no normal-startup provisioning/migrations;
- at-least-once publication semantics without an outbox.
