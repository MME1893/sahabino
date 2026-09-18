# Sahabino BI — Production Deployment and Operations Runbook

**Audience:** Production operator and database administrator (DBA)
**Scope:** Deploy, verify, operate, and recover the standalone Metabase BI service without changing the main Sahabino application's Compose files, migrations, or deployment procedure.

> **Execution status:** A historical, earlier-version Metabase run reported
> `APPLY: completed 34 action assessments; no unrelated content deleted`.
> That record **does not demonstrate that the current 42-question/7-dashboard
> manifest has been applied**. The final eligible-object `SKIP` plan,
> actual UI and reader-SQL checks, live health and restore drill still require
> current production evidence. This runbook is a procedure, not a server PASS.
>
> **Do not run initial provisioning on an existing installation without inspecting its databases, roles, secrets, and current state.** Do not replace `.env`, the encryption key, the API key, or `secrets/metabase_sync_state.json` during routine updates.

## 1. Deployment model and prerequisites

The BI service runs in its **own** `/opt/sahabino-bi` directory and Compose project. It connects to the **existing** Sahabino PostgreSQL container through the separate Docker network `sahabino-bi-db` and alias `sahabino-postgres-bi`. No additional PostgreSQL server is required. For an observation-first
verification of the **entire** system, use the
[production acceptance guide](../../docs/operations/PRODUCTION_ACCEPTANCE.md);
this document details BI-specific installation, ownership and recovery.

| Database | Purpose | Access |
|---|---|---|
| `sahabino` | Live application data: applications, store snapshots, reviews, observations, and crawl history | `sahabino_bi_reader`: restricted read-only access |
| `metabase_app` | Metabase accounts, connections, saved questions, dashboards, settings, and potentially cached results | `metabase_app`: own application database |

**Data freshness:** Opening or refreshing a question executes SQL against `sahabino` when there is no valid cached result. A valid Metabase cache may return an older result. A new crawler run does **not** require `metabase_sync.py apply`; `apply` updates dashboard/question *definitions*, not application data. Newly committed crawler data is visible on a new uncached query. Metabase schema synchronization is also separate from content synchronization.

**Prerequisites:** a running main Sahabino PostgreSQL container; Docker and Docker Compose v2; Python 3.10+; the **final, consolidated, reviewed BI release** already staged in `/opt/sahabino-bi`; DBA authorization for role/database changes; SSH access to the VPS. Do not deploy only an older foundation build or a synchronizer missing its two previously required API compatibility fixes.

### 1.1 Read-only environment inspection — always run first

Run on the **VPS** with an account permitted to operate Docker:

```bash
cd /opt/sahabino-bi
pwd
ls -ld . secrets backups 2>/dev/null || true
ls -l compose.yml .env.example provisioning/roles_and_grants.sql.example \
  scripts/network_attach.py scripts/preflight.py scripts/metabase_sync.py

docker compose version
docker ps --format '{{.Names}} | {{.Label "com.docker.compose.project"}} | {{.Label "com.docker.compose.service"}}'
docker network ls --format '{{.Name}}'
```

Stop if the installed BI release, existing secrets, ownership, or target PostgreSQL container cannot be identified unambiguously. Inspect existing state rather than copying a replacement release over it. The main Sahabino checkout and deployment assistant are out of scope.

## 2. Configure BI secrets — initial installation only

**Do not run this section to reset an existing service.** If `/opt/sahabino-bi/.env` already exists, inspect and preserve it instead.

```bash
cd /opt/sahabino-bi
umask 077
if [ -e .env ]; then
  printf '%s\n' 'Existing .env found: stop fresh-install secret creation and inspect it.'
else
  cp .env.example .env
  chmod 600 .env
fi
install -d -m 700 secrets backups
```

Edit `.env` using a private editor; the documented deployment expects these values (retain any other required fields already present in the release):

```dotenv
METABASE_PORT=3001
BI_DB_NETWORK=sahabino-bi-db
MB_DB_HOST=sahabino-postgres-bi
MB_DB_PORT=5432
MB_DB_DBNAME=metabase_app
MB_DB_USER=metabase_app
METABASE_APP_DB_PASSWORD_FILE=/opt/sahabino-bi/secrets/metabase_app_db_password
MB_ENCRYPTION_SECRET_KEY=<GENERATED_BASE64_ENCRYPTION_KEY>
MB_SESSION_SECRET_KEY=<DIFFERENT_GENERATED_RANDOM_SECRET>
BI_METADATA_BACKUP_DIR=/opt/sahabino-bi/backups
```

Generate **two distinct keys** in a private terminal; insert their values into the corresponding fields without publishing them:

```bash
openssl rand -base64 32    # encryption key; save securely
openssl rand -base64 32    # independent session key; save securely
nano .env
chmod 600 .env
```

Create password files **only if absent**, then enter independently generated passwords through a private editor. They must match the database roles configured in §4:

```bash
cd /opt/sahabino-bi
umask 077
[ -e secrets/metabase_app_db_password ] || install -m 600 /dev/null secrets/metabase_app_db_password
[ -e secrets/bi_reader_password ] || install -m 600 /dev/null secrets/bi_reader_password
nano secrets/metabase_app_db_password
nano secrets/bi_reader_password
chmod 700 secrets
chmod 600 .env secrets/metabase_app_db_password secrets/bi_reader_password
python3 scripts/validate.py
```

**Security constraints:** never put passwords in shell arguments, Git, chat, or published logs. Do not paste expanded `docker compose config` output; use `config --quiet`. Keep `.env`, `secrets/`, and `backups/` out of Git. The host-side `METABASE_APP_DB_PASSWORD_FILE` is distinct from the container-side `/run/secrets/metabase_app_db_password`. **Preserve the original encryption key** for every existing installation and backup.

## 3. Attach the existing PostgreSQL container to the BI network

Run on the VPS. This operation does **not** replace the main project's PostgreSQL or its Compose configuration.

```bash
cd /opt/sahabino-bi
export SAHABINO_COMPOSE_PROJECT=sahabino
python3 scripts/network_attach.py discover \
  --project "$SAHABINO_COMPOSE_PROJECT" --service postgres
```

From the discovery output, copy the actual **full 64-character PostgreSQL container ID** into the following command; do not leave the placeholder:

```bash
export PG_CONTAINER_ID='PASTE_VERIFIED_FULL_POSTGRES_CONTAINER_ID'
if [ "${#PG_CONTAINER_ID}" -ne 64 ]; then
  echo 'Invalid container ID; STOP'
  exit 1
fi
docker network inspect sahabino-bi-db >/dev/null 2>&1 || \
  docker network create --driver bridge --attachable sahabino-bi-db

python3 scripts/network_attach.py attach \
  --project "$SAHABINO_COMPOSE_PROJECT" --service postgres \
  --network sahabino-bi-db --container-id "$PG_CONTAINER_ID"
python3 scripts/network_attach.py check \
  --project "$SAHABINO_COMPOSE_PROJECT" --service postgres \
  --network sahabino-bi-db --container-id "$PG_CONTAINER_ID"
```

**Expected:** exactly one correctly labeled PostgreSQL container, matching full ID and `sahabino-postgres-bi` alias. If identity or alias differs, **stop**: do not disconnect/reconnect an existing endpoint blindly.

**After a main-project deployment recreates PostgreSQL:** rerun `discover`, replace `PG_CONTAINER_ID` with its **new** full ID, then rerun `attach` and `check`. This reattachment is a known ongoing operational requirement.

## 4. Provision the metadata database and restricted BI reader — initial installation / DBA change

### 4.1 Identify the actual PostgreSQL administrator

The recorded VPS uses the administrator `sahabino`; `postgres` did **not** exist there. Never assume an administrator username across environments.

```bash
cd /opt/sahabino-bi
# PG_CONTAINER_ID must be obtained and verified in §3.
docker exec "$PG_CONTAINER_ID" sh -c \
  'printf "POSTGRES_USER=%s\n" "$POSTGRES_USER"'
docker exec "$PG_CONTAINER_ID" sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
    -c "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user;"'
```

Review `provisioning/roles_and_grants.sql.example` and the target ACLs with the DBA. Its role/database creation is **not one global atomic transaction**; a previous failed run may already have created some objects. Never drop them as an automatic recovery step.

### 4.2 Inspect `PUBLIC` privileges before executing provisioning

Use a DBA connection to `postgres` and run the following read-only SQL:

```bash
docker exec -i "$PG_CONTAINER_ID" sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<'SQL'
SELECT d.datname AS database_name,
       pg_get_userbyid(d.datdba) AS owner,
       has_database_privilege('metabase_app', d.oid, 'CONNECT') AS mb_connect,
       has_database_privilege('metabase_app', d.oid, 'CREATE') AS mb_create,
       has_database_privilege('metabase_app', d.oid, 'TEMPORARY') AS mb_temp,
       EXISTS (
         SELECT 1
         FROM aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) a
         WHERE a.grantee = 0 AND a.privilege_type = 'TEMPORARY'
       ) AS public_temp
FROM pg_database d
WHERE d.datallowconn
ORDER BY d.datname;
SQL
```

**Note:** On a completely fresh install, the `metabase_app` role may not yet exist, so this query can fail. First inspect roles with the command below; if absent, skip the `metabase_app` privilege query until after the initial provisioning attempt:

```bash
docker exec -i "$PG_CONTAINER_ID" sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' <<'SQL'
SELECT rolname, rolcanlogin, rolsuper
FROM pg_roles
WHERE rolname IN ('metabase_app', 'sahabino_bi_reader');
SQL
```

### 4.3 Execute provisioning with the **actual** administrator

Run only after the SQL file and permissions have been reviewed by the DBA:

```bash
cd /opt/sahabino-bi
docker exec -i "$PG_CONTAINER_ID" sh -c \
  'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres' \
  < provisioning/roles_and_grants.sql.example
```

The SQL provisions roles `metabase_app` and `sahabino_bi_reader`, creates the `metabase_app` database, and applies restricted grants. If it stops with `Metadata role has CREATE/TEMP on another DB (possibly via PUBLIC)`, **do not** repeat it blindly. The recorded cause was inherited `PUBLIC TEMPORARY` on the existing `postgres` and `sahabino` databases. A cluster-wide `REVOKE ... FROM PUBLIC` affects other users and must only be performed following a DBA dependency review and approval. Do **not** embed this broad grant change into the deployment script. After an approved correction, rerun provisioning; never drop partially created roles or databases.

### 4.4 Set passwords interactively

```bash
docker exec -it "$PG_CONTAINER_ID" sh -c \
  'exec psql -X -U "$POSTGRES_USER" -d postgres'
```

At the `psql` prompt:

```sql
\password metabase_app
\password sahabino_bi_reader
\q
```

Enter the values from their matching private files. Do not place literal passwords into `ALTER ROLE` commands or shell history. For an existing role whose authentication already works, do not rotate it unnecessarily.

**Verify connectivity** with a password prompt, then verify the actual source database through the Metabase UI in §7. An in-container TCP test alone does **not** prove network authentication from Metabase, because `pg_hba.conf` may treat local connections differently.

```bash
docker exec -it "$PG_CONTAINER_ID" psql -X -h 127.0.0.1 -p 5432 \
  -U metabase_app -d metabase_app -W \
  -c 'SELECT current_user, current_database();'

docker exec -it "$PG_CONTAINER_ID" psql -X -h 127.0.0.1 -p 5432 \
  -U sahabino_bi_reader -d sahabino -W \
  -c 'SELECT current_user, current_database(), version_num FROM public.alembic_version;'
```

## 5. Run preflight before starting Metabase

```bash
cd /opt/sahabino-bi
python3 scripts/validate.py
python3 scripts/preflight.py \
  --project sahabino \
  --reader-password-file secrets/bi_reader_password \
  --metadata-password-file secrets/metabase_app_db_password
```

**Go/no-go:** all `BLOCKER` findings must be resolved. Review `WARN` findings before proceeding. Preflight checks file permissions, keys, Compose identity, network and alias, port exposure, backup destination, both DB connections, Alembic/schema, and role privileges. It does not create a database, repair ACLs, run migrations, or make a backup.

For permission diagnostics, do **not** print secret values:

```bash
cd /opt/sahabino-bi
stat -c '%a %U:%G %n' .env secrets \
  secrets/metabase_app_db_password secrets/bi_reader_password
```

Expected file modes: `.env` and password files `600`; secrets directory `700`, with appropriate owner. Repair permissions only for verified BI-owned paths. `BI_METADATA_BACKUP_DIR` being configured does not prove a backup exists.

## 6. Start and validate Metabase

Only after preflight passes:

```bash
cd /opt/sahabino-bi
docker compose --env-file .env -f compose.yml config --quiet
docker compose --env-file .env -f compose.yml pull
docker compose --env-file .env -f compose.yml up -d
docker compose --env-file .env -f compose.yml ps
curl -i --max-time 10 http://127.0.0.1:3001/api/health
docker compose --env-file .env -f compose.yml logs --tail=100 metabase
```

**Expected:** the image is the reviewed digest-pinned `metabase/metabase:v0.63.18@sha256:1160b570cb11c107bce00e71293552df8a8363e01a32c2c7a048cee002dc8a73`; published port remains **`127.0.0.1:3001`**, not `0.0.0.0:3001`; health eventually returns `{"status":"ok"}` and the container becomes healthy.

A brief connection reset, health `starting`, or HTTP 503 with `status=initializing` occurred during the recorded first boot and does not by itself prove permanent failure. If startup does not complete, inspect the following **without deleting volumes/databases**:

```bash
cd /opt/sahabino-bi
docker compose --env-file .env -f compose.yml ps
docker compose --env-file .env -f compose.yml logs --tail=200 metabase
curl -i --max-time 10 http://127.0.0.1:3001/api/health
docker stats --no-stream sahabino-bi-metabase-1
```

Check for explicit database authentication errors, connection refusal, OOM, or restarts. `up -d` starts Metabase; it does **not** create the 42 Saved Questions or seven dashboards defined by the current manifest.

## 7. Complete the one-time UI configuration securely

### 7.1 SSH port forwarding from Windows

Run on **Windows PowerShell**, not on the VPS; substitute the real SSH hostname. Keep the terminal open:

```powershell
ssh -N -L 13001:127.0.0.1:3001 sahabino@SERVER_IP
```

Open **http://127.0.0.1:13001** in the laptop browser. The local port is `13001`; server access terminates at its loopback-only `3001`. No public firewall opening or public Metabase port is needed. If `13001` is occupied, change only the **left-hand** port in `-L` and use the new browser port.

### 7.2 Admin account, application source and API key

On a fresh metadata database, complete Metabase's welcome wizard and create the administrator. On an existing database, **sign in with its existing account** instead of initiating a new first-run flow.

In the initial wizard or **Admin → Databases → Add Database**, configure the operational source:

| UI field | Value |
|---|---|
| Database type | PostgreSQL |
| Display name | **`Sahabino BI Source`** — synchronizer requires exact identity |
| Host | `sahabino-postgres-bi` |
| Port | `5432` |
| Database | `sahabino` |
| Username | `sahabino_bi_reader` |
| Password | The reader's private password; never publish it |
| SSL | Match the actual PostgreSQL/network policy; do not assume a universal setting |

Save and verify the connection from the **Metabase container**. Do **not** add `metabase_app` as the reporting source: that is Metabase's internal metadata DB, configured by Compose.

In Metabase administration, create an API key with the necessary scope for the `Sahabino BI Sync` operation. Store it privately on the VPS (create the file only if it does not exist):

```bash
cd /opt/sahabino-bi
umask 077
[ -e secrets/metabase_api_key ] || install -m 600 /dev/null secrets/metabase_api_key
nano secrets/metabase_api_key
chmod 600 secrets/metabase_api_key
```

Metabase's default sync endpoint is `http://127.0.0.1:3001` **on the VPS**, independent of the laptop SSH tunnel.

## 8. Deploy saved questions and dashboards — the essential content commands

After the UI source and API key have been configured:

```bash
cd /opt/sahabino-bi
python3 scripts/validate.py
python3 -m py_compile scripts/metabase_sync.py
python3 scripts/metabase_sync.py plan
```

Inspect the entire plan. The **current manifest** has three collections,
42 questions and seven dashboards (45 dashboard-card placements), but a fresh
installation may legitimately report `SKIP_CAPABILITY` for gated content.
Do **not** reuse an old expectation of 31 actions or assume every manifest
object can be created regardless of source schema, grants or validated evidence.
Stop for blockers, conflicting ownership, unexpected updates, a missing
source or unsafe SQL. Only after reviewing eligible actions and obtaining
operator approval:

```bash
cd /opt/sahabino-bi
python3 scripts/metabase_sync.py apply
python3 scripts/metabase_sync.py plan
```

**Historical record only:** an earlier `apply` logged 34 action assessments.
It is not proof of current coverage. **Required verification for this
revision:** every eligible, unchanged managed object should report `SKIP`;
unavailable capabilities may correctly report `SKIP_CAPABILITY`. Investigate
any unexpected `CREATE`, `UPDATE`, conflict, or blocker. Record counts and
compare deployed objects with the current manifest and enabled capabilities.

**Commands mean different things:**

| Command/action | Effect |
|---|---|
| `metabase_sync.py plan` | Validates/compares managed content without creating dashboards; may perform read queries |
| `metabase_sync.py apply` | Creates/updates versioned collections, questions and dashboards via the Metabase API |
| Open/refresh dashboard | Executes its queries or uses valid cached results |
| Crawler/ingestion runs | Updates data in the operational PostgreSQL DB; **does not** run `apply` |

The synchronizer records ownership and checkpoints in `secrets/metabase_sync_state.json`. **Never delete that file to bypass a conflict.** When changing a question's SQL or the dashboard manifest, review the planned `UPDATE`, run `apply` after approval, then rerun `plan`. No `apply` is needed simply because new reviews arrived.

## 9. Validate the BI deliverable, not only the API response

The manifest currently defines **three collections, 42 saved questions,
seven dashboards, and 45 dashboard-card placements**. Eligibility is
capability-dependent; record both the declared inventory and what was actually
provisioned and executed:

| Dashboard | Cards | Acceptance checks |
|---|---:|---|
| Executive Portfolio | 6 | Application coverage, freshness and totals; excluded app behavior; no fabricated risk score |
| Store Growth & Rating | 11 | Rating/count trends, valid date and locale comparisons, install thresholds rather than fabricated precise growth |
| Review Intelligence | 5 | Unique reviews versus observations, score changes, sampling and coverage |
| Sentiment Intelligence | 5 | Required schema and valid `done` labels; correct denominator; absent data shown as unavailable |
| Network Benchmark | 8 | Schema/grants, capture readiness and per-metric sample gates; do not interpret missing experiments as performance. |
| Application Experience — Store × Network × User Voice | 4 | Independently aggregated sources; historical locale, timestamps and unavailable coverage clearly shown. |
| Release Impact Explorer | 6 | Verified releases, exact periods and sample gates; no causal claim or fabricated missing evidence. |

In the UI at **http://127.0.0.1:13001**, open `Sahabino BI → Dashboards`.
Execute **every actually eligible/deployed card**, inspect empty/error states,
test available application/country/language/date/experiment/release filters,
and compare selected results with direct SQL as `sahabino_bi_reader`. A
successful `apply` is **not** acceptance of results or chart layout. Record
unavailable network/release evidence as an explicit capability limitation,
not an achieved benchmark.

On the VPS, record a clean content audit:

```bash
cd /opt/sahabino-bi
python3 scripts/metabase_sync.py plan
python3 scripts/validate.py
python3 -m pytest -q tests
```

If Docker-backed disposable PostgreSQL tests are skipped, report them as **NOT RUN**, not successful SQL validation. Production backup/restore and dashboard visual checks are also separate acceptance items.

### KPI interpretation guardrails

- A Store snapshot reflects **when it was collected**, not necessarily current Store state. Historical country/language comes from the associated crawl task.
- Store `ratings_count` and `reviews_count` are cumulative reported counts; changes can be negative and are **not** install growth. `min_installs` is a reported threshold, not precise installs.
- `reviews` counts unique review identities. `review_observations` tracks repeated observations/revisions, not additional independent reviewers. For historical ratings use eligible observations, not today's current `reviews.score`.
- Star shares must use the correct eligible unique-review denominator; no fabricated zero for missing denominator.
- Sentiment requires its real schema, grants and valid `done` labels. `pending`, `failed`, and `skipped` are not neutral. Sentiment is not proof of a network complaint.
- Missing baseline, missing snapshot and missing measurement remain unavailable/NULL, not zero. Treat filtered historical totals and global unique totals as distinct populations.

## 10. Daily operations: health, freshness, content changes

### 10.1 Routine checks

```bash
cd /opt/sahabino-bi
docker compose --env-file .env -f compose.yml ps
curl -i --max-time 10 http://127.0.0.1:3001/api/health
python3 scripts/network_attach.py discover --project sahabino --service postgres
# Obtain/verify the full current PostgreSQL ID and set PG_CONTAINER_ID as in §3.
python3 scripts/network_attach.py check --project sahabino --service postgres \
  --network sahabino-bi-db --container-id "$PG_CONTAINER_ID"
python3 scripts/metabase_sync.py plan
```

If the main PostgreSQL container was recreated, perform §3 **before** concluding the BI database is down. Monitor the underlying crawler/ingestion and worker separately: refreshing an old snapshot cannot create new application data.

### 10.2 Changed SQL/dashboard definitions

Change only reviewed, versioned BI SQL and manifest files; do not silently overwrite managed questions in the UI. After code review:

```bash
cd /opt/sahabino-bi
python3 scripts/validate.py
python3 -m pytest -q tests
python3 scripts/metabase_sync.py plan
# Confirm that ONLY the intended objects are CREATE/UPDATE.
python3 scripts/metabase_sync.py apply
python3 scripts/metabase_sync.py plan
```

Keep the Metabase image digest pinned. An upgrade needs separate API-compatibility and data/backup tests; the previously observed card-API MBQL format and dashboard assembly changes caused failures.

### 10.3 Data freshness troubleshooting

If a dashboard still shows old values after a crawler run, check in this order: source data committed; actual `collected_at`, `observed_at`, or `processed_at`; filters and the card's SQL; Metabase cache validity; and dashboard/card refresh. **Do not rerun `apply` as a data-refresh mechanism.** Auto-refresh can increase query load on the production PostgreSQL instance.

## 11. Incident response: known failures and exact safe actions

| Symptom | Diagnosis / operator action |
|---|---|
| `role "postgres" does not exist` | Inspect `POSTGRES_USER` and DBA role using §4.1. Use the actual administrator; do not invent a `postgres` role. |
| `Metadata role has CREATE/TEMP on another DB` | Inspect `PUBLIC` and effective grants under §4.2; obtain DBA approval before altering shared privileges. Provisioning may have partly completed; do not drop objects. |
| Missing/insecure secret | `stat` the paths as in §5. Correct only verified BI-owned permissions and file paths; never display the password. |
| Preflight database blocker | Check role, DB, authentication, grants, Alembic/schema, and network. Rerun the entire preflight after correction. |
| HTTP 503 / `initializing` | Inspect health and recent Metabase logs under §6; do not delete metadata or rotate the encryption key. |
| `Remote payload differs ... question:q01` | Recorded failure involved MBQL5 versus legacy card API. Verify the **final consolidated synchronizer** supports `legacy-mbql=true` and checkpoint repair. Do not delete Q01 or state. |
| HTTP 400 on `PUT /api/dashboard/2` | Recorded failure involved duplicate temporary dashboard-card ID `-1`. Confirm the final synchronizer generates distinct negative IDs for new cards and retains existing positive IDs. Do not delete the dashboard. |
| `Manual edit... refusing overwrite` | Inspect the managed object, state and manifest read-only; resolve ownership/conflict explicitly. No force overwrite. |
| Missing `Sahabino BI Source` | Recheck the exact UI display name, network alias, reader credentials, and source connectivity. Do not assume database ID 1. |
| Dashboard empty | Check actual data, query, filter, rights, caching, and sentiment prerequisites. Do not convert absent samples into zeros. |
| BI disconnect after main deployment | Main PostgreSQL container ID/network membership may have changed. Repeat §3 with the new verified ID. |
| Port 3001 occupied | Confirm which process owns it; never kill an unrelated service or expose Metabase publicly. |
| Laptop UI inaccessible but VPS health OK | Keep SSH tunnel open; inspect local port `13001`, host/SSH identity, and browser URL. |

**Partial-apply recovery:** Stop write operations; privately back up `secrets/metabase_sync_state.json`; inspect managed objects and the planned changes; correct the *verified* synchronizer defect in a reviewed release; run `py_compile`, tests and `plan`; only then approve `apply` and check the final `plan`. Do not erase checkpoint state or remote objects to force a clean run.

## 12. Back up, restore and roll back safely

### 12.1 Back up the Metabase metadata database and matching state

The metadata DB preserves users, connections, questions, dashboards and settings, and **may include cached query results**. BI state and encryption material are also essential. The operational `sahabino` database requires its own, separate backup strategy.

Run on the VPS, after checking disk space and confirming the **current** PostgreSQL container ID:

```bash
cd /opt/sahabino-bi
umask 077
df -h / /opt/sahabino-bi
BACKUP_TAG=$(date -u +%Y%m%dT%H%M%SZ)
# DBA in the recorded environment is sahabino; verify per §4.1 before using.
docker exec "$PG_CONTAINER_ID" pg_dump -U sahabino -Fc -d metabase_app > \
  "backups/metabase_app-${BACKUP_TAG}.dump"
DUMP_STATUS=$?
if [ "$DUMP_STATUS" -ne 0 ]; then
  echo 'Metadata backup FAILED; do not treat output file as a backup.'
else
  chmod 600 "backups/metabase_app-${BACKUP_TAG}.dump"
  pg_restore -l "backups/metabase_app-${BACKUP_TAG}.dump" >/dev/null || \
    echo 'Archive-list inspection FAILED: investigate backup.'
  cp -p secrets/metabase_sync_state.json \
    "backups/metabase_sync_state-${BACKUP_TAG}.json"
  chmod 600 "backups/metabase_sync_state-${BACKUP_TAG}.json"
fi
```

Replace `sahabino` in `pg_dump -U` only after verifying the actual DBA. `pg_restore -l` requires the PostgreSQL client utilities on the host; if unavailable, verify the archive in an approved container or DBA environment. **Listing archive entries is not a successful restore test.** Encrypt and keep backups off the VPS. Safeguard the *original* Metabase encryption key separately. Never publish dump contents or state.

### 12.2 Restore drill and incident recovery

Restoration was **not verified** in the recorded deployment. Before a production cutover, have the DBA restore a backup into a **new, empty database**, configure an **isolated** Metabase test instance with the matching original encryption key and state, and verify login, source connection and dashboard definitions. Only after that should a separately approved production recovery plan change `MB_DB_DBNAME`.

**Never:** run `pg_restore --clean` against live `sahabino` or `metabase_app`; remove Docker volumes; regenerate the encryption key; delete state; or overwrite existing BI secrets during a code rollback. Rolling back BI does not roll back Sahabino's application migrations.

## 13. Release acceptance checklist

- [ ] Final reviewed BI release and synchronizer include both previously required API fixes; existing metadata and secrets preserved.
- [ ] PostgreSQL container identity and BI network alias verified; reattachment procedure understood.
- [ ] DBA approved role/grant state; provisioning and both actual container-to-container connections validated.
- [ ] Preflight has **no BLOCKER**; warnings assessed; secrets and encryption key private.
- [ ] Compose config valid; image digest pinned; Metabase accessible only on loopback; health `ok` and Docker `healthy` recorded.
- [ ] Admin account, exact `Sahabino BI Source`, and private API key configured.
- [ ] Current manifest inventory compared with actual enabled capabilities; only approved `CREATE`/`UPDATE` applied; final `plan` is `SKIP` for eligible unchanged objects, with justified `SKIP_CAPABILITY` where applicable.
- [ ] Seven dashboards, 42 questions and 45 card placements declared; deployed/eligible subset inventoried; every relevant panel, filter and selected reader-SQL result verified.
- [ ] Data timestamps and cache behavior documented; missing values not misreported as zero.
- [ ] Metadata backup, matching sync state and encryption material protected; restore drill completed before claiming disaster-recovery readiness.

**Recorded versus unverified:** The historical `apply` record pertains to a
smaller earlier inventory. A current eligible-object `SKIP` plan, chart/SQL
results, health status and restore drill remain acceptance checks until their
actual output is captured for this revision. See the
[full production acceptance guide](../../docs/operations/PRODUCTION_ACCEPTANCE.md).
