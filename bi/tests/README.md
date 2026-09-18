# BI executable tests and their limits

From the root of `bi/` run `python3 -m pytest -q tests` (pytest required). `scripts/validate.py` only checks offline structure/privacy strings; it is **not** an SQL execution test.

`test_postgres_integration.py` starts a disposable `postgres:16-alpine` container with `--network none`, no host ports, and only synthetic schema/fixtures. It applies the grant script twice, executes every eligible Q01-Q42 as `sahabino_bi_reader`, then adds mock optional sentiment columns. Assertions cover legacy Store/Review semantics plus exact Network condition grains, metric-specific eligibility, Upload/Download separation, pair identity/cohort rejection, release periods with only one eligible capture, manifest-only release evidence, historical review cutoffs and denial of raw review/network columns and writes. Docker/image absence is explicit SKIP/NOT RUN.

`test_sync.py` uses deterministic mock HTTP API **not a running Metabase**. It covers read-only plan, empty apply, second apply idempotency, changed SQL scoped update, unrelated content preservation, missing DB, schema/data-gated optional sentiment, unauthorized key, version mismatch, ownership conflict/manual archival, partial checkpoint, exact card IDs, filter mappings and secret handling. The actual v0.63.18 API OpenAPI schema and request/response normalization MUST be checked against a **disposable Metabase instance** with authorized operator key before production apply. No end-to-end Metabase test ran in this environment.

`test_network.py` mocks Docker label/inspect/attach calls for first/repeated/correct/wrong/missing attachments, wrong full IDs, ambiguous/absent container, missing alias/network and unavailable Docker. Never exercises a production Docker network.

No test modifies Sahabino's application database, release scripts, migrations, application tests or Compose. If production rejects a SQL column or API payload, treat it as a blocker, do not broaden source grants or silently change app migrations.

## Extended network/release validation

`run_postgres_validation.py` is the stdlib-only path when pytest is unavailable. It starts a uniquely named `postgres:16-alpine` container with `--network none`, publishes no ports, loads only synthetic fixtures, applies the reader grants twice, executes Q01-Q42 as the restricted role, asserts network medians/paired/release values, and verifies raw review/network columns plus writes are denied. It always removes only the container it created.

`test_metadata.py` adds more than 30 manifest cases: UUIDs, timestamps/order, required provenance, action/phase, SHA/size, device/profile/cohort catalogs, duplicates, cross-experiment pair rejection, pair cardinality/conditions, DB attribution/size/existence, release precision/windows/verification and private-field exclusion from generated SQL. Offline validation explicitly records DB checks as NOT_RUN.

`test_sync.py` now covers all 42 questions and 7 dashboards, immutable Q01-Q24 SQL/dashboard-definition hashes, readiness-only Network behavior when schema/grants are absent, private-field exclusion, optional sentiment, unchanged second apply/no duplicates, dynamic manifest card counts, layout/filter mapping, conflicts, partial checkpoints and secrets. It remains a mock contract test, not proof against a live Metabase. A disposable **digest-pinned `metabase/metabase:v0.63.18@sha256:1160b570cb11c107bce00e71293552df8a8363e01a32c2c7a048cee002dc8a73`** API smoke test is still required before any approved production apply; if Docker/image/API setup is unavailable, report it as NOT RUN and do not claim deployment readiness.

## Production acceptance boundary

These tests belong on a disposable Docker-capable developer/CI host. The SQL
harness deliberately creates and destroys its own isolated test container, but
its successful result still does not prove the live source schema/grants,
Metabase connections, actual card results, or dashboard layouts. On a live VPS,
use the read-only checks in
[`docs/operations/PRODUCTION_ACCEPTANCE.md`](../../docs/operations/PRODUCTION_ACCEPTANCE.md)
and record skipped integration cases as **NOT RUN**. Do not point any test database
URL, test fixture or synthetic capture at production.
