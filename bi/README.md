# Standalone Sahabino BI

This directory is an independently runnable Compose project for Metabase OSS and the Phase 1
Store/Executive questions. Copy `bi/` to `/opt/sahabino-bi` only after the existing Sahabino stack
and PostgreSQL are healthy. It does not participate in the Sahabino Compose project and has no
dependency on the API, Kafka, workers, SeaweedFS, Grafana, or crawler services.

The module creates no warehouse, ETL, views, materialized views, or source tables. Compose does
not provision PostgreSQL. All database administration below is a separate, manual, authorized
operator action.

## Security and data boundaries

```text
operator browser --SSH tunnel--> 127.0.0.1:3001 -> Metabase container
                                                       |
                                    external BI-only Docker network
                                                       |
                                             existing Postgres container
                                               |                 |
                               metabase_app DB/role      sahabino DB
                               writable metadata         sahabino_bi_reader
                                                        column-limited/read-only
```

- Metabase binds only `127.0.0.1:${METABASE_PORT:-3001}`. Grafana remains on loopback port 3000.
- PostgreSQL is reached through the external Docker network and is never published by this module.
- `metabase_app` owns only Metabase's application/metadata database. It is not the analytics
  source credential.
- `sahabino_bi_reader` connects to `sahabino` with column-level `SELECT` grants for Phase 1 only.
- Raw review content/author data, review observations, Kafka state, credentials, and secret tables
  are not granted.
- Metabase passwords and encryption/session keys are not tracked. The source-reader password is
  entered manually in the Metabase UI and stored in the encrypted metadata database.

## Image verification and pinning

The selected image is the official `metabase/metabase:v0.63.18` release, pinned to the immutable
multi-platform manifest digest:

```text
metabase/metabase:v0.63.18@sha256:1160b570cb11c107bce00e71293552df8a8363e01a32c2c7a048cee002dc8a73
```

Evidence rechecked on 2026-09-17:

- The official [GitHub v0.63.18 release](https://github.com/metabase/metabase/releases/tag/v0.63.18)
  reports tag `v0.63.18` (published 2026-09-16).
- `docker manifest inspect metabase/metabase:v0.63.18` returned linux/amd64 and linux/arm64
  manifests.
- A Docker Registry v2 `HEAD` request for that exact tag returned
  `Docker-Content-Digest: sha256:1160b570...8a73`.
- The official v0.63.18
  [`run_metabase.sh`](https://github.com/metabase/metabase/blob/v0.63.18/bin/docker/run_metabase.sh)
  explicitly maps `MB_DB_PASS_FILE` to `MB_DB_PASS` before startup.

Registry tags are mutable pointers, including exact patch tags. The digest suffix is what makes
the checked-in image reference immutable. Do not replace it with `latest`, `v0.63.x`,
`v0.63.18.x`, or a newly resolved digest without the upgrade review below.

## 1. Copy and prepare local secrets

Run on the BI host after copying this directory:

```sh
cd /opt/sahabino-bi
cp .env.example .env
chmod 600 .env
install -d -m 700 secrets backups
install -m 600 /dev/null secrets/metabase_app_db_password
```

Use a password manager or protected editor to:

1. place only the `metabase_app` role password in
   `secrets/metabase_app_db_password` with no extra logging;
2. replace both `CHANGE_ME` values in `.env` with independent high-entropy values of at least
   32 random bytes;
3. confirm the paths, metadata DB name, username, and external network name.

Keep `METABASE_PORT=3001`; never set it to 3000 on this host because production Grafana already
owns `127.0.0.1:3000`.

Metabase v0.63.18 supports `_FILE` for its application-database password, so Compose mounts that
password as a secret. Its encryption and session key settings do not have `_FILE` handling in the
v0.63.18 entrypoint; they stay in the ignored mode-0600 `.env`. Back up the encryption key in the
approved secret manager. Losing or changing it can make encrypted source credentials unusable;
changing the session key signs out existing sessions.

Never run or share `docker compose config` output against the real `.env`, because interpolation
can render the encryption/session keys. The validation command later uses synthetic placeholders
and `--quiet` only.

## 2. Discover and attach the existing PostgreSQL container

Do not infer the container name and do not inspect its environment, which could print the
application database password. Discover candidates from non-secret Compose labels:

```sh
docker ps \
  --filter label=com.docker.compose.service=postgres \
  --format '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Label "com.docker.compose.project"}}'
```

Verify the selected row belongs to the already-running Sahabino project. Set operator-local
variables (replace the container value with the exact result):

```sh
SAHABINO_POSTGRES_CONTAINER='<exact-postgres-container>'
SAHABINO_BI_NETWORK='sahabino-bi-db'
```

Create the external network if absent, then attach only that PostgreSQL container from the
existing stack with a stable alias:

```sh
docker network inspect "$SAHABINO_BI_NETWORK" >/dev/null 2>&1 \
  || docker network create --driver bridge --attachable "$SAHABINO_BI_NETWORK"

if ! docker network inspect "$SAHABINO_BI_NETWORK" \
  --format '{{range .Containers}}{{println .Name}}{{end}}' \
  | grep -Fxq "$SAHABINO_POSTGRES_CONTAINER"; then
  docker network connect \
    --alias sahabino-postgres-bi \
    "$SAHABINO_BI_NETWORK" \
    "$SAHABINO_POSTGRES_CONTAINER"
fi
```

Check membership and the alias without reading container environment variables:

```sh
docker network inspect "$SAHABINO_BI_NETWORK" \
  --format '{{range .Containers}}{{println .Name}}{{end}}'
docker inspect "$SAHABINO_POSTGRES_CONTAINER" \
  --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name $network.Aliases}}{{end}}'
```

Before Metabase starts, the external network should contain only PostgreSQL. After start, it
should contain PostgreSQL and this module's Metabase container—no API, Kafka, worker, SeaweedFS,
or Grafana containers.

If PostgreSQL is already attached but lacks `sahabino-postgres-bi`, repair only this BI network:

```sh
docker network disconnect "$SAHABINO_BI_NETWORK" "$SAHABINO_POSTGRES_CONTAINER"
docker network connect \
  --alias sahabino-postgres-bi \
  "$SAHABINO_BI_NETWORK" \
  "$SAHABINO_POSTGRES_CONTAINER"
```

### Reconnect after PostgreSQL recreation

The external attachment belongs to the container, not its persistent volume. A recreated
PostgreSQL container receives a new identity and loses this attachment. Repeat the discovery,
create/inspect, conditional `network connect`, membership, and alias checks above. They are safe
to repeat. Then recheck Metabase health; if it does not reconnect on its own:

```sh
cd /opt/sahabino-bi
docker compose --env-file .env -f compose.yml restart metabase
```

Do not change the Sahabino deployment files to make this attachment persistent.

## 3. Manually provision the two roles and metadata database

Review [`provisioning/roles_and_grants.sql.example`](provisioning/roles_and_grants.sql.example)
with the DBA. It creates no source tables and grants only the columns used by Q01–Q05. It also
sets the reader's transactions read-only with a 30-second statement timeout.

The script assumes the existing source database is named `sahabino`, as in this repository. An
authorized operator can apply the reviewed file once using the existing PostgreSQL admin role:

```sh
docker exec -i "$SAHABINO_POSTGRES_CONTAINER" \
  psql --username '<existing-admin-role>' --dbname postgres \
  < provisioning/roles_and_grants.sql.example
```

Assign both passwords interactively so neither appears in shell history or the tracked SQL:

```sh
docker exec -it "$SAHABINO_POSTGRES_CONTAINER" \
  psql --username '<existing-admin-role>' --dbname postgres
```

At the `psql` prompt:

```text
\password metabase_app
\password sahabino_bi_reader
\q
```

Put the first password in the Compose secret file. Keep the second in the approved secret manager
for one-time entry into the Metabase source connection.

The final provisioning query must show all four booleans as `false`. In particular, a direct
`REVOKE CREATE` or `REVOKE TEMPORARY` cannot override a grant inherited through PostgreSQL's
`PUBLIC` pseudo-role. If a CREATE/TEMPORARY check is true, stop: the DBA must review/harden the
existing database or schema PUBLIC ACL and confirm impact on application roles before BI starts.
Do not grant `pg_read_all_data`, table-wide source access, schema or temporary-object creation,
SUPERUSER, `CREATEDB`, or `CREATEROLE` to the reader.

### Non-mutating read-only denial test

After setting the reader password, connect as that role and verify its settings:

```sh
docker exec -it "$SAHABINO_POSTGRES_CONTAINER" \
  psql --username sahabino_bi_reader --dbname sahabino
```

Run:

```sql
SHOW default_transaction_read_only;
SHOW statement_timeout;
SELECT COUNT(*) FROM public.applications;
BEGIN;
UPDATE public.applications SET name = name WHERE FALSE;
ROLLBACK;
```

The settings must show `on` and `30s`; the `SELECT` must work; the zero-row `UPDATE` must be
denied by read-only mode and/or privileges. If it were unexpectedly permitted, `WHERE FALSE` and
the rollback/connection close prevent row changes, but the operator must stop and fix privileges.

## 4. Validate and start independently

Static/offline validation:

```sh
python3 scripts/validate.py
```

Compose parsing with synthetic values and no secret output:

```sh
MB_DB_DBNAME=synthetic_metadata \
MB_DB_USER=synthetic_user \
MB_ENCRYPTION_SECRET_KEY=synthetic-validation-key-32-bytes \
MB_SESSION_SECRET_KEY=synthetic-validation-session-32-bytes \
METABASE_APP_DB_PASSWORD_FILE=/dev/null \
docker compose --env-file /dev/null -f compose.yml config --quiet
```

The `config` command does not start or pull anything. Start only after provisioning and network
checks are approved:

```sh
docker compose --env-file .env -f compose.yml pull
docker compose --env-file .env -f compose.yml up -d
docker compose --env-file .env -f compose.yml ps
curl --fail --silent --show-error http://127.0.0.1:3001/api/health
```

Expected health JSON contains `"status":"ok"`. Inspect startup failures without printing Compose
configuration:

```sh
docker compose --env-file .env -f compose.yml logs --tail=200 metabase
```

Stop and start independently:

```sh
docker compose --env-file .env -f compose.yml stop
docker compose --env-file .env -f compose.yml start
```

`docker compose down` removes only this Compose project's container/network endpoint; the named
network is external and PostgreSQL remains running. Do not add `--volumes` and do not operate the
Sahabino Compose project from this directory.

## 5. Private access and initial UI setup

Keep the host port closed publicly. From an operator workstation:

```sh
ssh -N -L 3001:127.0.0.1:3001 '<operator>@<bi-host>'
```

Open `http://127.0.0.1:3001` locally, create the initial Metabase admin, then add the source under
**Admin settings → Databases → Add a database**:

- Type: PostgreSQL
- Host: `sahabino-postgres-bi`
- Port: `5432`
- Database: `sahabino`
- Username: `sahabino_bi_reader`
- Password: enter from the secret manager; do not place it in `.env`
- SSL: follow the existing PostgreSQL policy; the database remains on the private Docker network

Limit scanning before broad use:

1. keep only the `public` schema enabled; grants make only the five approved tables/columns visible;
2. schedule schema sync off-peak and infrequently enough for this small, stable schema;
3. disable periodic field-value scanning/refingerprinting where the UI permits it, or schedule it
   off-peak; do not enable aggressive hourly scans;
4. never enable model persistence, uploads, database actions/writeback, or a writable connection;
5. turn off public sharing and embedding unless a later security review explicitly approves them;
6. grant general users access to curated collections/dashboards, not unrestricted native SQL.

The database role is the final write barrier even if a UI option is misconfigured. Re-run the
read-only denial test after connection or privilege changes.

## 6. Create saved questions and dashboards manually

No running authorized Metabase exists in this checkout, so no dashboard objects are claimed or
serialized. API-based provisioning is intentionally out of scope.

For each file in `questions/`:

1. open **New → SQL query** and select the read-only Sahabino database;
2. paste the SQL unchanged and run it;
3. compare the returned grain and coverage fields with the contract in
   [`reports/README.md`](reports/README.md);
4. choose the recommended visualization, save with the report title, and add dashboard filters
   only to output fields documented for that report;
5. add Q01 to **Executive Portfolio** and Q02–Q05 to **Store Growth & Rating**;
6. show sample size, locale, UTC date, and freshness fields in tooltips/tables; never hide a
   missing/insufficient status behind zero.

Create navigation placeholders for **Review Intelligence**, **Network/Application Comparison**,
and **Release Impact**, but do not add queries until the prerequisites in the report catalog are
met. Do not use dummy production data, screenshots, or causal annotations.

## 7. Metadata backup and restore boundary

Metabase metadata is operational state: users, encrypted source credentials, saved questions,
dashboards, permissions, and settings. It lives only in `metabase_app`, not in H2 and not in the
Sahabino application database. Give it a distinct backup job, destination, name, retention, and
restore exercise. Never append it silently to Sahabino's existing application-data backup.

Manual backup example after setting the exact container/admin placeholders:

```sh
umask 077
METABASE_BACKUP_FILE="backups/metabase_app_$(date -u +%Y%m%dT%H%M%SZ).dump"
docker exec "$SAHABINO_POSTGRES_CONTAINER" \
  pg_dump --username '<existing-admin-role>' --dbname metabase_app \
  --format=custom --no-owner --no-acl \
  > "$METABASE_BACKUP_FILE"
sha256sum "$METABASE_BACKUP_FILE"
pg_restore --list "$METABASE_BACKUP_FILE" >/dev/null
unset METABASE_BACKUP_FILE
```

The operator must send the backup and checksum to approved encrypted storage; `bi/backups/` is
ignored but is not a durable backup destination.

Non-mutating restore-readiness checklist:

- identify the exact metadata backup timestamp, checksum, Metabase image digest, PostgreSQL major
  version, encryption key, and session key;
- verify `pg_restore --list` succeeds without connecting to a database;
- select a separate authorized restore target—never `sahabino` and never an existing development
  or production database for a rehearsal;
- confirm the target will be an empty database owned by `metabase_app` and that no source-table
  grants are part of the archive;
- obtain a separate change approval before any database drop/create or `pg_restore` execution;
- after restore, start only the matching Metabase image, test login/questions/permissions through
  the SSH tunnel, and repeat the source read-only denial test.

This repository does not execute backup or restore commands.

## 8. Upgrade and rollback

Treat Metabase application-DB migrations as potentially one-way.

1. Read release and security notes for an exact patch release.
2. Verify the official tag, resolve its registry digest, and pin `tag@sha256:digest` in
   `compose.yml`.
3. Take and verify a separate `metabase_app` backup and record the current image digest and keys.
4. Pull and start the new image during an approved window; check health, logs, login, questions,
   permissions, and source read-only behavior.
5. Never “roll back” by starting an older image on a metadata database already migrated by a newer
   version. A true rollback restores the pre-upgrade metadata backup into a clean metadata
   database, then starts the recorded old digest.

Do not modify, migrate, restart, or deploy the Sahabino stack as part of a BI upgrade.

## Scope limitations

- Phase 1 includes only the five Store/Executive SQL questions.
- No production, existing development database, or live Metabase was contacted by development of
  this module.
- Sentiment/review, release-impact, and network SQL are deliberately deferred; see the report
  catalog for prerequisites. Migrations `20260917_0007` and `20260917_0008` exist in the source
  checkout, but the last observed production revision was `20260916_0006`; this module does not
  assume the later observation/sentiment columns are deployed.
- Q03 refuses a median for a one-peer sample and labels two-peer samples as limited. Current small
  categories may therefore have no usable benchmark on some dates/locales.
- Q05 is a threshold history. It cannot measure exact installs between Google Play milestones.
