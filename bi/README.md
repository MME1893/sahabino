# Sahabino BI — independent, operator-controlled module

**Status:** version-controlled definition and test harness; nothing here proves that production Metabase objects have been created. Only files under `bi/` belong to this delivery. This is deliberately separate from Sahabino's Compose, network, Alembic/release automation, analyzer and sentiment worker. It creates **no application tables or migrations**. PostgreSQL must already have the source schema. No application passwords or secret values belong in Git.

## What exists

- Official Metabase OSS `v0.63.18`, pinned by repository-supplied digest in `compose.yml` (retain the full `@sha256` reference); no custom Dockerfile. Host port bound to `127.0.0.1` only; intended access via SSH forwarding, never a public firewall rule.
- PostgreSQL source `sahabino`: minimal column-specific `sahabino_bi_reader`. Separate metadata database `metabase_app`, owned by writable `metabase_app`. An independent, manually created external BI-only Docker bridge connects to the **existing** source PostgreSQL container with alias `sahabino-postgres-bi`.
- 24 read-only PostgreSQL report queries, 3 collections, 24 saved-question definitions, 4 dashboard definitions and 27 cards (`manifest/content.json`); Q13–Q17 and Sentiment dashboard are conditional. Base dashboards are Executive Portfolio, Store Growth & Rating and Review Intelligence. Network and Release Impact definitions are **deferred contracts**, not fake dashboards.
- `scripts/network_attach.py` (explicit attach), `scripts/preflight.py` (read-only audit), `scripts/validate.py` (offline shape check), `scripts/metabase_sync.py plan|apply` (API content life-cycle). `docker compose up` **never runs** content synchronization.

**Acceptance boundary:** No Docker daemon, PostgreSQL instance or authorized Metabase/API key was available in the implementation environment. `pytest` mock tests ran; real SQL authorization tests are implemented but NOT RUN. Before production apply, run them on a host with Docker, then validate actual pinned-version API payloads on an isolated Metabase and resolve any fail-closed incompatibility. `plan` makes API GETs and `POST /api/dataset` read-only SQL SELECTs; it never issues collection/card/dashboard writes. `apply` is explicitly invoked, not startup-triggered.

## 1. Independent prerequisites and files

Use a deployment checkout that contains **only** the `bi/` directory or, within a project checkout, switch into `bi/`. Commands below assume `/opt/sahabino-bi` contains `.env.example`, `compose.yml`, `scripts/`, etc. Docker Compose v2, Python 3.10+, PostgreSQL server already running in the existing Sahabino Compose project, and a DBA are required. For the optional executable tests, Docker access and availability of `postgres:16-alpine` are required; no PostgreSQL port is published for the test. Ensure the pinned Metabase image can be pulled by Docker. `python3 -m pytest` requires pytest and is **not** needed at runtime.

```bash
cd /opt/sahabino-bi
cp .env.example .env
chmod 600 .env
install -d -m 700 secrets backups
# Edit .env using a private terminal/editor; replace CHANGE_ME with independent keys.
openssl rand -base64 32   # encryption key: copy privately to MB_ENCRYPTION_SECRET_KEY
openssl rand -base64 32   # DIFFERENT session key: copy privately to MB_SESSION_SECRET_KEY
# Create BOTH password files with a secure editor, avoid command-line literals/history.
install -m 600 /dev/null secrets/metabase_app_db_password
install -m 600 /dev/null secrets/bi_reader_password
# Set METABASE_APP_DB_PASSWORD_FILE to the ABSOLUTE path of the first file.
python3 scripts/validate.py
```

The outputs of `openssl` are secrets; don't put them in issue comments or shell history. The two generated keys are environment values, so protect `.env` (0600); Metabase v0.63.18 uses `MB_DB_PASS_FILE` for metadata DB password. All passwords, API key, ownership state and backups are ignored by `.gitignore`; never publish an expanded `docker compose config` that may contain environment secrets. Bind port to loopback even behind a reverse proxy; default endpoint `http://127.0.0.1:3001`.

## 2. DBA: metadata and restricted source reader

Inspect `provisioning/roles_and_grants.sql.example` before executing; it is a **manual admin action**. It creates missing `NOINHERIT` login roles and a separate UTF8 `metabase_app` DB if absent, refuses unsafe pre-existing roles/memberships/database ownership, adds only source-column SELECT grants, and conditionally grants the three sentiment columns when migration schema exists. It neither revokes PUBLIC permissions on the existing `sahabino` database nor touches existing application roles. **Isolation caveat:** on a stock PostgreSQL cluster, PUBLIC may have `TEMP` on other databases; the script deliberately BLOCKS if that makes `metabase_app` writable outside metadata DB. Only the DBA may approve/remediate this environment-specific ACL conflict, with an application-role impact assessment; do not use BI automation for broad PUBLIC revocations. The disposable test cluster revokes PUBLIC CONNECT/TEMP only inside its own isolated container. On an existing metadata database it refuses an unexpected owner or PUBLIC CONNECT; stop for DBA review rather than changing access blindly. Partial-failure recovery: inspect which roles/database exist, repair credentials and any unsafe ACLs manually, rerun script; never drop the existing metadata DB on retries.

```bash
# Discover real PostgreSQL container using exact Compose labels first (section 3).
# Run the SQL from this BI checkout, not Alembic. Set exact full PG container ID.
docker exec -i "$PG_CONTAINER_ID" psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres \
  < provisioning/roles_and_grants.sql.example
# DBA password entry, interactive: connect to postgres and run:
# \password metabase_app
# \password sahabino_bi_reader
# Set those separate generated passwords in the respective protected secret files.
```

**Roles:** `metabase_app` owns/writes only its own metadata DB; do not connect it to the Sahabino source. `sahabino_bi_reader` connects to source `sahabino` with SELECT on explicitly listed application/category/task/snapshot and *only* review identity, first-observed timestamp, observation score/time/task and (when present) status/label/language. `reviews.content`, `reviews.author_name`, `reviews.external_review_id`, `review_observations.content` and other sensitive data are **not granted**. Source role gets no table-wide SELECT, inherited role, schema CREATE, database CREATE or TEMP, and defaults to read-only with timeouts. Existing PUBLIC/inherited escalation is a **blocker**, not an invitation to broad production privilege revocations. Test privilege denial in disposable PostgreSQL using `tests/test_postgres_integration.py` and audit via preflight. If preflight detects a violation, the DBA must identify the underlying source privileges and approve a narrowly scoped repair; otherwise stop deployment.

**Important:** database `sahabino` and revision up to `20260916_0006` are sufficient for Store/Review; sentiment columns depend on independently deployed migrations `20260917_0007` and `20260917_0008`. This SQL does not apply those migrations, backfill sentiment or start a worker.

## 3. Attach the existing PostgreSQL to the independent network

Original bug: `docker ps -q` produces a *container ID*, while matching a network `Containers[*].Name` compares it to *container names* and can silently mis-detect membership. This script obtains a single running container using exact Compose project and service labels, verifies the complete selected container ID against Docker network **ID keys**, and checks the alias from container inspect. It never rewires an existing attachment to fix alias drift automatically.

```bash
# Replace this placeholder with the ACTUAL project label, not a guessed service name.
export SAHABINO_COMPOSE_PROJECT='THE_EXACT_COMPOSE_PROJECT_LABEL'
python3 scripts/network_attach.py discover --project "$SAHABINO_COMPOSE_PROJECT" --service postgres
# Copy the exact FULL ID from discover output; confirm with your operator/DBA.
export PG_CONTAINER_ID='PASTE_FULL_64_CHARACTER_CONTAINER_ID'
# Explicit one-time step ONLY if inspect says network does not yet exist:
docker network inspect sahabino-bi-db >/dev/null 2>&1 || \
  docker network create --driver bridge --attachable sahabino-bi-db
python3 scripts/network_attach.py attach --project "$SAHABINO_COMPOSE_PROJECT" \
  --service postgres --network sahabino-bi-db --container-id "$PG_CONTAINER_ID"
python3 scripts/network_attach.py check --project "$SAHABINO_COMPOSE_PROJECT" \
  --service postgres --network sahabino-bi-db --container-id "$PG_CONTAINER_ID"
```

The *operator* must select the full ID: no discovery heuristics pick between multiple matching containers. Repeated `attach` is an unchanged success when membership+alias already match. On missing container, ambiguous match, wrong selected ID, Docker unavailable, conflicting network IDs or missing/wrong alias, the script stops. **Never automatically disconnect/reconnect live PostgreSQL.** For missing alias on an existing network endpoint: schedule an approved maintenance window; inspect clients/network identity; only with explicit approval manually run `docker network disconnect sahabino-bi-db "$PG_CONTAINER_ID"` then `docker network connect --alias sahabino-postgres-bi sahabino-bi-db "$PG_CONTAINER_ID"` and verify via `check`. This may disrupt Metabase sessions; coordinate and restart only BI if necessary.

**Container recreation:** Sahabino Compose can replace its PostgreSQL container and lose the independent network endpoint. After each replacement, discover the **new full ID**, explicitly re-run `attach`/`check` before bringing BI back, and rerun preflight; do not modify the main Compose or attach an old ID. If PostgreSQL is absent stop here; do not create/start main Sahabino services from this module.

## 4. Non-destructive preflight

```bash
python3 scripts/preflight.py --project "$SAHABINO_COMPOSE_PROJECT" \
  --service postgres --network sahabino-bi-db \
  --reader-password-file secrets/bi_reader_password \
  --metadata-password-file secrets/metabase_app_db_password
```

Reports actual PostgreSQL identity/network/alias; loopback host port availability; reader and metadata DB connectivity (the probe inside the PG container uses the local socket, so it is **not** a proof of Docker cross-network password authentication); Alembic revision; each needed column, conditional sentiment schema/grants; reader privilege escalation; backup destination configuration. `OK` is a check passed, `WARN` needs operator review but does not fake readiness, `BLOCKER` makes exit code 2 and prohibits apply. An already running BI may legitimately occupy port 3001: confirm the owner is the **correct loopback-bound BI container**. Ensure BI `.env`, secret-file modes, keys and network alias are correct before starting. Preflight never creates containers, networks, roles or databases, and suppresses SQL diagnostics and secrets. It cannot substitute for independently confirming PG database TCP host routing from the Metabase container.

## 5. Start Metabase and connect source manually

```bash
docker compose --env-file .env -f compose.yml config --quiet
docker compose --env-file .env -f compose.yml pull
docker compose --env-file .env -f compose.yml up -d
docker compose --env-file .env -f compose.yml ps
# From your workstation; the web app remains loopback-only on the server:
ssh -N -L 3001:127.0.0.1:3001 OPERATOR@BI_SERVER
```

Authorized operator opens `http://127.0.0.1:3001` **through the tunnel**, performs Metabase first-run setup manually (no auto-admin), then creates a PostgreSQL data source named **exactly `Sahabino BI Source`**, with host `sahabino-postgres-bi`, port `5432`, DB `sahabino`, username `sahabino_bi_reader` and its own password. Set SSL consistent with the existing source PostgreSQL policy; do not use metadata role, DB ID 1, first match, or a published PostgreSQL port. Metabase syncs database *metadata*, not our `.sql` files. The dashboard sync uses the Metabase API separately. Compose healthcheck requires an exact `"status":"ok"` response; pinned image, localhost binding and native `/run/secrets` file require no custom Dockerfile.

## 6. API key and explicit two-step content sync

An authorized Metabase admin must manually create an **API key** with the minimum permissions sufficient for the target data source and managed collections. Restrict access to the collections and underlying source, and keep the API private. Write the key without shell echo or command-history literal into `secrets/metabase_api_key`; enforce `chmod 600 secrets/metabase_api_key`. The synchronizer reads it from file only, never creates users, keys or initial setup; API key is never echoed, sent through redirects or sent off loopback by default. `--allow-remote` allows an **explicit** HTTPS origin only; avoid it for this deployment.

```bash
python3 scripts/metabase_sync.py plan
# Inspect CREATE, UPDATE, SKIP, SKIP_CAPABILITY or BLOCKER; proceed only after review.
python3 scripts/metabase_sync.py apply
python3 scripts/metabase_sync.py plan   # normally SKIP for all unchanged managed objects
```

`plan` GETs API content, verifies `/api/health`, the *exact pinned version* reported by `/api/session/properties`, required routes in the **live** `/api/docs/openapi.json` schema, exactly one `Sahabino BI Source` database, reader identity/permissions, schema, and attempts a read-only `LIMIT 0` execution of every eligible SQL. The API is not versioned: on any Metabase image/digest upgrade review official API docs, actual live OpenAPI and request/response shape on a **disposable instance**, update contract tests, and only then change this module. No OSS Enterprise Serialization assumption; no direct metadata DB writes. All actions are operator controlled.

`apply` creates or updates only objects with our stable `[sahabino-bi:<kind>:<key>]` description marker **and** a private local `secrets/metabase_sync_state.json` mapping logical IDs to remote numeric IDs and normalized remote hashes. This state is as essential as the metadata DB backup: back up both together. An untracked same-name object, duplicate, deleted marker, manual edit, unknown remote drift or unsupported API payload is a **hard conflict**; it is not automatically adopted, overwritten, archived or deleted. Writes checkpoint each verified object. Dashboard cards use per-card IDs and parameter-to-template-tag mappings; no duplicate cards are created on unchanged runs. If SQL is changed in a tracked question file, plan shows `UPDATE question:qNN`, apply updates that saved question only; live dashboard **data** refresh is independent (Metabase executing queries, its cache/schedule and underlying crawls). Changes do not sync during Compose startup.

**Partial failure:** stop; capture stdout and private state (redact API key); inspect Metabase managed collection/cards/dashboard and last successfully checkpointed state; restore consistent metadata+state backup when needed, or reconcile marker/remote hash under an authorized manual recovery process. Do not blindly delete existing cards or delete `secrets/metabase_sync_state.json` and retry against a nonempty server. No transactional, atomic cross-object guarantee is claimed. A server-modified response different from expected is checkpointed then rejected, not represented as success. A previously enabled sentiment dashboard is never deleted automatically on schema rollback; restrict/hide it manually until migrations are restored.

**Filter mapping:** application uses exact package name; category uses current primary assignment, not historical; country/language always actual crawl task; dates are UTC. Native SQL uses optional, typed text/date template tags and variable mappings, not misleading Field Filters for computed aggregate/CTE keys. `start_date` applies **after** LAG for Store count/score change questions and Review transitions, preserving prior baseline. Q03 category peers are selected before focal app filter. No date filter attached to cards without mathematically defensible date semantics (Q01/Q03/Q06/Q07 and latest-state cards). In cohort questions, `start_date` means review's *first encounter* and `end_date` means last eligible observation cutoff; latest observation inside a selected locale may differ from global latest. Check intended mappings and visualizations against the real pinned instance before use.

## 7. Sentiment enablement without breaking Review

At a source revision without `sentiment_status`, `sentiment_label` and `sentiment_language`, or if all classified records are absent, `plan` prints `SKIP_CAPABILITY` for Q13–Q17 and Sentiment dashboard. Executive/Store/Review remain provisionable. Once application operations separately deploy migrations `20260917_0007` and `20260917_0008`, start worker through its **own** deployment, rerun the DBA SQL to grant **only** the three columns, run preflight then `plan`/`apply`. Existing historical observations intentionally marked `skipped` remain unclassified. The BI module does not modify that data, infer missing labels, or assume model/version provenance.

## 8. Metadata backup, restore, rotation, rollback

The metadata DB is `metabase_app` in **existing PostgreSQL**, not a new DB managed by Compose. Configure `BI_METADATA_BACKUP_DIR` and an actual operator-managed encrypted/off-host retention schedule. Use an admin account with backups permissions through an interactive/authenticated secure channel (no password in command line):

```bash
# Example one-time backup; choose existing secured destination and date/tag.
docker exec "$PG_CONTAINER_ID" pg_dump -U postgres -Fc -d metabase_app > \
  backups/metabase_app-YYYYMMDD.dump
chmod 600 backups/metabase_app-YYYYMMDD.dump
# Also save privately: secrets/metabase_sync_state.json AND the correct .env key material.
# Restore: STOP Metabase first and use a NEW, EMPTY, DBA-approved target DB.
docker compose --env-file .env -f compose.yml stop metabase
# DBA creates that new target metadata DB with correct owner/ACL using approved workflow.
# Example; REPLACE NEW_EMPTY_METADATA_DB (do NOT point at existing metabase_app):
docker exec -i "$PG_CONTAINER_ID" pg_restore -U postgres -d NEW_EMPTY_METADATA_DB \
  --no-owner --no-acl < backups/metabase_app-YYYYMMDD.dump
# Verify restored object owner/ACL, correct original encryption key, matching state snapshot,
# switch MB_DB_DBNAME via approved .env edit only when verified, then restart Metabase.
```

Do a regular *actual restore drill*. Never `pg_restore --clean` against live source/metadata; no automated overwrite. Rotate PostgreSQL passwords interactively with `\password`, update private files, then restart the affected BI connection. Rotate operator Metabase API key in admin UI, edit 0600 key file and revoke old key; repeat `plan`. **Do not casually rotate `MB_ENCRYPTION_SECRET_KEY`:** encrypted Metabase stored credentials depend on it; follow supported Metabase re-encryption/backup procedure first. Rotating `MB_SESSION_SECRET_KEY` invalidates sessions; schedule downtime. Roll back code using version control within `bi/`, stop Metabase if incompatible, restore corresponding metadata/state/key snapshot when needed. BI rollback never rolls back Sahabino application migrations. Keep backups and secrets outside distributed artifacts.

## 9. Verification and deferred boundaries

Run `python3 -m pytest -q tests` on a disposable Docker-capable host; real test uses `postgres:16-alpine`, network `none`, no host ports and synthetic schema/fixture. If Docker unavailable it explicitly SKIPs and **does not count as SQL success**. Then use a disposable Metabase v0.63.18 instance and API key to verify creation, idempotency, SQL update, exact saved-question names/descriptions/collections, rows vs direct psql, dashboard card layout/filter mappings/axes and separate missing-sentiment behavior. On production, compare Q01/Q06 totals, Q02 observed days and raw score, Q03 peer cohort, Q04/Q19/Q20 negative corrections, Q05/Q21 install *thresholds*, Q10/Q11 unique review sample, Q12 actual transitions, and Q13–Q17 coverage/classifications against independent DBA SELECTs. Verify missing fields remain `NULL`, not fabricated 0, and that fixture still exists in source but is excluded in BI. See [report reference and KPI contract](reports/README.md) and [test details](tests/README.md).

**Still deferred (no fabricated outputs):** Network Benchmark requires PCAP/analyzer metric schema, explicit unit, capture cohort and comparable traffic definition; Baham-vs-Pinno and Telegram-vs-WhatsApp network comparisons require matched real captures and baseline; Store × Network requires application/time/locale join and comparable sampling; Release Impact Explorer requires independently verified real release timestamps, observation windows and confounder controls. None are activated by this manifest until their real data and validations exist. Store-only Baham vs Pinno Q18 is an explicitly matched *store* comparison, not a network claim. Sentiment is *not* topic classification or evidence of a network complaint.

Official API and environment reference: https://www.metabase.com/learn/metabase-basics/administration/administration-and-operation/metabase-api ; https://www.metabase.com/docs/latest/people-and-groups/api-keys ; https://www.metabase.com/docs/latest/configuring-metabase/environment-variables ; https://www.metabase.com/docs/latest/installation-and-operation/running-metabase-on-docker . Confirm the **pinned** version's own live API docs before applying, as `/docs/latest/` may differ from v0.63.18.
