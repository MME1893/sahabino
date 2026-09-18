# Sahabino Observability V1

Observability V1 adds four PostgreSQL-backed dashboards and structured operational events to the existing Grafana, Loki, and Alloy stack. It does not add a service, expose PostgreSQL, alter application tables, or make the PostgreSQL dashboard path mandatory.

## Architecture and activation

The existing application services continue to write JSON logs to Docker. Alloy collects containers carrying `com.sahabino.logs=true`, Loki stores the logs, and Grafana keeps using `sahabino-loki` as its default Data Source. The existing logging smoke dashboard is unchanged.

Grafana receives the repository provisioning tree as a read-only source. `infrastructure/observability/grafana/entrypoint.sh` copies it to a container-local temporary directory before Grafana starts. If `GRAFANA_POSTGRES_PASSWORD` is empty or absent, the script removes `postgres.yaml` from that runtime copy. Loki and file-dashboard provisioning remain present. If the password exists, Grafana provisions `Sahabino PostgreSQL` with UID `sahabino-postgres`. The small entrypoint remains necessary because native Grafana provisioning has no supported conditional-file mechanism; provisioning a Data Source with an absent credential would not be equivalent to omitting it.

The PostgreSQL Data Source connects only over the internal Compose network at `postgres:5432`, uses `sslmode=disable` because that network does not terminate TLS, and opens at most three connections. The reader role permits five connections: three for Grafana's bounded pool, one for Ansible credential verification, and one spare connection. PostgreSQL remains unpublished in the production override.

Grafana provisioning uses `$GRAFANA_POSTGRES_PASSWORD`, not `${GRAFANA_POSTGRES_PASSWORD}`. Grafana expands the braced form twice; the unbraced form preserves a `$` embedded in the environment value. The secret is stored under `secureJsonData` and is not present in dashboard JSON.

## Reader security contract

`grafana_reader` is created outside Alembic after migrations have reached the checked-out head. The task is repeatable and applies:

- `LOGIN`, `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`, `NOREPLICATION`, and `NOBYPASSRLS`;
- connection limit 5;
- read-only transactions, 30-second statement and idle-transaction limits, and a 5-second lock timeout;
- database `CONNECT` and `USAGE` on `public`;
- column-level `SELECT` on `applications`, `crawl_runs`, `crawl_tasks`, `reviews`, and `review_observations`.

The grants deliberately exclude `reviews.content`, `reviews.author_name`, `reviews.external_review_id`, `review_observations.content`, and `crawl_tasks.error_message`. Grafana Explore therefore cannot read those fields through this account. The setup rejects a pre-existing role with elevated attributes, memberships, object ownership, table-level access, write privileges, or an effective ability to create objects in `public`.

An existing role password is never changed by default. Set `sahabino_grafana_reader_rotate_password=true` for one reviewed deployment to perform an explicit rotation, then return it to `false`.

## Optional secret enrollment and password reuse

Existing Vault files remain valid without a new key. To enable the feature, edit the encrypted file in place:

```bash
cd /opt/sahabino/app/deploy/ansible
ansible-vault edit --vault-password-file /etc/sahabino/ansible-vault-password group_vars/production/vault.yml
```

Add this one optional reference; Ansible resolves it to the existing PostgreSQL
Vault value before validating it or rendering the optional environment variable:

```yaml
vault_sahabino_grafana_reader_password: "{{ vault_sahabino_postgres_password }}"
```

This is an explicit, one-time operator choice: the deployment assistant does not
generate the key, add the reference, or modify an encrypted Vault. It does not
reuse the Grafana administrator password, and it never uses the primary
PostgreSQL username in Grafana. The reader remains a separate, column-restricted
database role.

Password sharing increases the impact of disclosure because the same secret may
authenticate the primary PostgreSQL account, which can have broader privileges.
Only enroll this reference when that trade-off is approved. The effective primary
password must satisfy the existing primary PostgreSQL preflight contract:
20 or more characters from letters, digits, `.`, `_`, and `-`, without
`CHANGE_ME`. The reader-only password contract accepts additional characters,
but sharing a primary password does not bypass its stricter validation. An invalid effective value is
not rendered into `.env`; the optional feature is reported disabled while the
original deployment continues.

The reader verification connects to `postgres:5432`, not the database
container's `127.0.0.1`: a normal `initdb` cluster can permit loopback
`trust` authentication. Activation also requires that an intentionally wrong
password be rejected over the same connection route. A `trust`-configured
network rejects activation rather than pretending to verify a password.

An existing reader with unapproved column-level SELECT privileges is rejected
instead of silently retaining access to review text or other restricted data.

A normal reviewed deployment creates or verifies the reader after migrations,
verifies the credential over TCP, and reconciles Grafana. If the key is missing,
the task reports a skip. If setup or credential verification fails, Ansible
removes only the optional variable from the rendered `.env`, reports a warning,
and continues the existing deployment path. It never falls back to application
credentials.

If `grafana_reader` already has a different password, setup does not overwrite
it. Credential verification disables the optional Data Source and reports the
mismatch safely. For a reviewed one-time rotation, set
`sahabino_grafana_reader_rotate_password: true` in the production variable
source for one deployment, then restore `false`; this changes only the reader
role to the effective shared password.

Fresh installations remain disabled because the deployment assistant creates the original Vault key set only. No change to `sahabino-deploy.sh` is required.

## Local setup

Leave `GRAFANA_POSTGRES_PASSWORD=` empty in `.env` for the original Loki-only behavior. To enable locally, first initialize the reader against an isolated development database using the committed SQL and a password that meets the format above. Supply the secret through standard input rather than a command-line argument:

```bash
read -rsp 'Grafana reader password: ' GRAFANA_READER_PASSWORD; echo
{
  printf "\\set reader_password '%s'\n" "$GRAFANA_READER_PASSWORD"
  printf "\\set rotate_password 'false'\n"
  cat deploy/ansible/roles/sahabino_deploy/files/grafana_reader.sql
} | docker compose exec -T postgres sh -c \
  'exec psql --no-psqlrc -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
unset GRAFANA_READER_PASSWORD
```

Put the same value in the ignored local `.env`. Keep the value single-quoted so
Compose treats any `$` as literal, then start the existing profiles:

```dotenv
GRAFANA_POSTGRES_PASSWORD='REPLACE_WITH_THE_SAME_LOCAL_SECRET'
```

```bash
docker compose --profile observability up -d postgres grafana loki alloy
```

## Dashboard catalog and KPI definitions

- **Sahabino Overview**: all-time application, unique-review, and observation counts; selected-range crawl counts and failures; current task status and sentiment backlog; latest runs; daily unique-review collection growth.
- **Sahabino Crawler Operations**: run and task outcomes, current task states, completed-task duration, recorded attempts, bounded error codes, latest failures without error text, activity, and application breakdown.
- **Sahabino Reviews**: unique identities versus repeated observations, daily collection series, ratings, application counts, latest non-sensitive observation metadata, selected-range cumulative growth, and available quality indicators.
- **Sahabino Application Explorer**: a single-value database-backed application selector, application-specific reviews, observations, crawl history, failures, ratings, activity, last task, durations, actual task language/country, and sentiment state.

"All time" panels ignore the Grafana time range. "Selected range" panels use the named database timestamp. `reviews` counts unique review identities; `review_observations` counts collection observations. `first_observed_at`, `observed_at`, and database `created_at` are collection/persistence timestamps, not Play Store publication time. `attempt_count` is labeled recorded attempts, not retries. Store snapshot totals are not presented as collected-review totals.

## Structured event catalog

- `crawler.task.succeeded` and `crawler.task.failed`: now include monotonic `duration_seconds` when the task execution began.
- `crawler.run.completed` and `crawler.run.failed`: include monotonic run duration; completion also retains aggregate application count.
- `crawler.review.fetch_completed`: one INFO event per review task with count, locale, task/application context, and fetch duration.
- `crawler.review.batch_published`: one INFO event after Kafka acknowledges the full batch, with record count and topic.
- `ingestion.message.processed`, `ingestion.message.duplicate`, `ingestion.message.skipped`, and database-processing failures: include monotonic processing duration.

No event includes review content, author names, payloads, credentials, connection strings, or presigned URLs. The additions do not change retry, transaction, Kafka schema, scheduling, or concurrency behavior.

## Verification

The [production acceptance checklist](../operations/PRODUCTION_ACCEPTANCE.md)
separates HTTP readiness, Loki ingestion *after a real crawl*, optional
PostgreSQL Data Source enrollment, and every Grafana panel's actual output.
A healthy Grafana `/api/health` does not prove these paths. If
`GRAFANA_POSTGRES_PASSWORD` is absent, the four PostgreSQL-backed dashboards
may be provisioned but their Data Source is not enabled: record this as a
feature-gated/incomplete dashboard acceptance, not a complete PASS.

Static checks from the repository root:

```bash
uv run pytest tests/deployment/test_observability.py tests/unit/crawler/test_application.py tests/unit/crawler/test_orchestration.py tests/unit/ingestion/test_worker.py
docker compose --env-file .env --profile observability config --quiet
docker compose --env-file .env -f docker-compose.yml -f compose.prod.yml --profile observability --profile network config --quiet
```

On a feature-enabled host, verify the role without printing secrets:

```sql
SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolconnlimit
FROM pg_roles WHERE rolname = 'grafana_reader';
SELECT has_table_privilege('grafana_reader', 'reviews', 'INSERT,UPDATE,DELETE,TRUNCATE');
SELECT has_column_privilege('grafana_reader', 'reviews', 'content', 'SELECT');
SELECT has_column_privilege('grafana_reader', 'reviews', 'id', 'SELECT');
```

Expected booleans are false, false, and true for the three privilege queries. In Grafana, confirm the Data Source UID is `sahabino-postgres`, its user is `grafana_reader`, all four dashboards appear in the existing Sahabino folder, the application dropdown resolves, and the original logging smoke dashboard still queries `sahabino-loki`.

Runtime dashboard validation requires a running PostgreSQL and Grafana. Compare all-time panels with independent `count(id)` or `count(review_id)` queries and test an empty isolated database. Do not run write-denial probes against production; use an isolated container.

## Troubleshooting

- **Data Source absent**: confirm the optional Vault key is present and the deployment did not report `OPTIONAL FEATURE DISABLED`; do not add application credentials as a fallback.
- **Existing role rejected**: inspect role attributes, memberships, ownership, schema creation, and grants. Remove unsafe privileges deliberately or choose not to enable the feature.
- **Credential verification fails for an existing role**: the task intentionally did not overwrite its password. Review the role owner's records and use one explicit rotation deployment if appropriate.
- **Panels show no data**: confirm the Data Source UID, dashboard time range, migrations at head, and reader column grants. Empty tables legitimately produce empty series or zero count statistics.
- **Loki dashboard issue**: treat it separately; PostgreSQL activation does not alter the Loki provisioning file or UID.

## Non-destructive rollback

1. Remove `vault_sahabino_grafana_reader_password` from the encrypted Vault and run the normal reviewed deployment at an exact revision. The Grafana entrypoint omits the PostgreSQL provisioning file. Because that file uses `prune: true`, Grafana removes the Data Source it provisioned while leaving Loki, dashboards, and `grafana_data` intact.
2. For a Grafana-only reconciliation after the configuration is reviewed, an operator may run the command below from `/opt/sahabino/app`. It force-recreates only Grafana so the conditional entrypoint runs again, while preserving `grafana_data`. Do not run `down -v`.

   ```bash
   docker compose --project-name sahabino --env-file .env \
     --file docker-compose.yml --file compose.prod.yml \
     --profile observability --profile network \
     up -d --no-deps --no-build --pull never --force-recreate grafana
   ```

3. Leaving `grafana_reader` in place is harmless and preserves easy reactivation. If policy requires removal, first confirm the Data Source is absent and no sessions use the role, then revoke its explicit grants and drop only `grafana_reader` during a separate approved database change. Role removal is not automatic.
4. To roll back repository behavior, deploy the prior exact Git revision through the existing assistant. Do not edit the production checkout or provisioning files in place.

The PostgreSQL role and Grafana Data Source contain no application data, so disabling them does not require a database restore or volume deletion.
