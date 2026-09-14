# Sahabino production operations runbook

This runbook is for an already-provisioned Sahabino production VPS. Initial
bootstrap and releases should use
[`deploy/ansible/sahabino-deploy.sh`](../../deploy/ansible/README.md).

Routine runtime operations should normally be performed as the `sahabino`
deployment user. Commands that inspect root-only deployment state, systemd, or
protected backups use `sudo` explicitly.

## Enter the production checkout

```bash
sudo -iu sahabino
cd /opt/sahabino/app
```

Do **not** use `sudo cd ...`; `cd` is a shell builtin. If you need a root shell
for a compound operation, use `sudo bash -lc 'cd /opt/sahabino/app && ...'`.

All production Compose examples use the same model:

```text
project:  sahabino
base:     docker-compose.yml
override: compose.prod.yml
profile:  observability
env:      .env
```

For an interactive maintenance shell you can define a temporary helper:

```bash
scompose() {
  docker compose \
    --project-name sahabino \
    --env-file .env \
    --file docker-compose.yml \
    --file compose.prod.yml \
    --profile observability \
    "$@"
}
```

The function exists only in the current shell. All commands below can also be
expanded to the full `docker compose ...` form.

## Service inventory

The default production profile should contain:

```text
postgres
kafka
api
crawler
ingestion
loki
alloy
grafana
```

`seaweedfs` and `network-analyzer` belong to the optional `network` profile and
are not part of the default production deployment.

Show running services:

```bash
scompose ps
```

Show running + exited/restarting containers:

```bash
scompose ps -a
```

Show only service names currently running:

```bash
scompose ps --services --filter status=running
```

## Stop and start the stack

### Temporary stop

`stop` keeps the containers and named volumes:

```bash
scompose stop
```

Start the same containers again:

```bash
scompose start
```

Using `up -d` is also safe and is generally preferred because Compose can
reconcile configuration at the same time:

```bash
scompose up -d
```

### Cold container/network recreation

Remove project containers and its Compose network while preserving named
volumes:

```bash
scompose down
```

Then recreate/start:

```bash
scompose up -d
```

> **Never use `scompose down -v` in production unless the explicit goal is data
> destruction.** `-v` removes named volumes and can destroy PostgreSQL, Kafka,
> Grafana, Loki, and optional SeaweedFS state.

For source/configuration releases, migrations, or new images, use the deployment
assistant instead of manually assembling a release sequence.

## Restart/recreate one service

Normal restart:

```bash
scompose restart api
scompose restart crawler
scompose restart ingestion
```

Recreate one service from the current Compose model/image:

```bash
scompose up -d --force-recreate loki
```

Be careful with PostgreSQL/Kafka restarts during active writes. Prefer a planned
maintenance window when restarting stateful infrastructure.

## Production verification shortcut

The deployment assistant already contains the canonical verification path:

```bash
sudo /opt/sahabino/app/deploy/ansible/sahabino-deploy.sh --verify
```

It validates permissions, running services, Kafka health, API/Loki/Grafana
readiness, database counts, crawler status, consumer lag, log markers, and
project container resources.

## HTTP/readiness checks

API Swagger target:

```bash
curl -fsS -o /dev/null -w 'API: %{http_code}\n' \
  http://127.0.0.1:8000/docs
```

Loki readiness:

```bash
curl -fsS http://127.0.0.1:3100/ready && echo
```

Grafana health:

```bash
curl -fsS http://127.0.0.1:3000/api/health && echo
```

Loki can intentionally return HTTP 503 for a short ingester warm-up window after
startup. Retry for roughly 15–30 seconds before treating the first 503 as a
failure.

Quick combined check:

```bash
set -e
curl -fsS -o /dev/null http://127.0.0.1:8000/docs
curl -fsS http://127.0.0.1:3100/ready
curl -fsS http://127.0.0.1:3000/api/health
printf '\ncore HTTP readiness: OK\n'
```

## API smoke queries

List active applications from the VPS:

```bash
curl -fsS 'http://127.0.0.1:8000/applications?active=true' \
  | python3 -m json.tool
```

List categories:

```bash
curl -fsS http://127.0.0.1:8000/categories \
  | python3 -m json.tool
```

The production API may currently be publicly bound on port 8000. These loopback
commands deliberately avoid relying on external routing/firewall state.

## Follow service logs

Crawler:

```bash
scompose logs -f --tail=100 crawler
```

Ingestion:

```bash
scompose logs -f --tail=100 ingestion
```

API:

```bash
scompose logs -f --tail=100 api
```

Loki:

```bash
scompose logs -f --tail=100 loki
```

Recent crawler lifecycle markers:

```bash
scompose logs --since=30m crawler \
  | grep -E 'crawler\.scheduler\.started|crawler\.run\.(started|completed)|crawler\.task\.(succeeded|failed)' \
  | tail -n 100
```

Recent ingestion activity/errors:

```bash
scompose logs --since=30m ingestion \
  | grep -E 'ingestion\.message\.processed|ERROR|CRITICAL|Traceback' \
  | tail -n 100
```

A healthy idle ingestion worker does not need to emit messages continuously;
consumer lag and service state are stronger health signals.

## PostgreSQL pipeline counts

Use PostgreSQL credentials already present inside the container; there is no
need to print the production password:

```bash
scompose exec -T postgres sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off <<SQL
SELECT '\''applications'\'' AS metric, COUNT(*) AS count FROM applications
UNION ALL
SELECT '\''crawl_runs'\'', COUNT(*) FROM crawl_runs
UNION ALL
SELECT '\''crawl_tasks'\'', COUNT(*) FROM crawl_tasks
UNION ALL
SELECT '\''ingested_events'\'', COUNT(*) FROM ingested_events
UNION ALL
SELECT '\''playstore_app_snapshots'\'', COUNT(*) FROM playstore_app_snapshots
UNION ALL
SELECT '\''reviews'\'', COUNT(*) FROM reviews
UNION ALL
SELECT '\''review_observations'\'', COUNT(*) FROM review_observations;
SQL
'
```

The snapshot table is `playstore_app_snapshots` (not `app_snapshots`).

## Crawler task status

Counts by status/type:

```bash
scompose exec -T postgres sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT
    status,
    task_type,
    COUNT(*) AS count
FROM crawl_tasks
GROUP BY status, task_type
ORDER BY status, task_type;
"
'
```

Recent failed tasks with retry/error information:

```bash
scompose exec -T postgres sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT
    a.package_name,
    ct.task_type,
    ct.status,
    ct.attempt_count,
    ct.error_code,
    ct.finished_at
FROM crawl_tasks ct
JOIN applications a ON a.id = ct.application_id
WHERE ct.status = '\''failed'\''
ORDER BY ct.finished_at DESC NULLS LAST
LIMIT 30;
"
'
```

`APP_NOT_FOUND` for a package that is genuinely unavailable is a domain failure,
not evidence that the crawler scheduler or Kafka pipeline is down.

## Recent crawl runs

```bash
scompose exec -T postgres sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
SELECT
    id,
    trigger_type,
    status,
    scheduled_for,
    started_at,
    finished_at,
    crawler_version,
    created_at
FROM crawl_runs
ORDER BY created_at DESC
LIMIT 10;
"
'
```

With the default production configuration the scheduler runs roughly hourly.
A `partially_failed` run can still represent an otherwise successful batch when
one application has an expected domain-level failure.

## Kafka broker and consumer lag

Broker/container health:

```bash
scompose ps kafka
```

Broker API check:

```bash
scompose exec -T kafka \
  /opt/kafka/bin/kafka-broker-api-versions.sh \
  --bootstrap-server localhost:19092
```

Ingestion consumer-group lag:

```bash
scompose exec -T kafka \
  /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:19092 \
  --describe \
  --group sahabino-ingestion-v1
```

For active partitions, `LAG=0` means ingestion has caught up. A `-` offset on a
topic/partition with no committed records is not equivalent to positive lag.

List Sahabino topics:

```bash
scompose exec -T kafka \
  /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:19092 \
  --list
```

## Observability through SSH forwarding

Grafana, Loki, and Alloy are intentionally loopback-only on the VPS. Keep the
SSH session open on your workstation:

```bash
ssh -N \
  -L 3000:127.0.0.1:3000 \
  -L 3100:127.0.0.1:3100 \
  -L 12345:127.0.0.1:12345 \
  sahabino@SERVER_IP
```

Open locally:

```text
Grafana: http://127.0.0.1:3000
Loki:    http://127.0.0.1:3100
Alloy:   http://127.0.0.1:12345
```

If SSH login is only available through a different administrator account, use
that account for the tunnel; the important property is that the forwarded
remote endpoints remain `127.0.0.1`.

For the optional SeaweedFS network profile, add:

```text
-L 8333:127.0.0.1:8333
```

The provisioned Grafana logging dashboard reads Loki labels extracted by Alloy:

```text
service
environment
level
```

Useful Grafana Explore LogQL examples:

```logql
{service="sahabino-api"} | json
```

```logql
{service="sahabino-crawler", level="ERROR"} | json
```

```logql
{service="sahabino-ingestion"} | json
```

Do not expose Grafana/Loki/Alloy publicly merely to access them remotely.

## Current deployed revision

```bash
sudo cat /opt/sahabino/DEPLOYED_REVISION
```

Compare with the production checkout:

```bash
git rev-parse HEAD
git status --short
```

A production checkout should remain clean. Do not use destructive Git commands
to suppress unexpected server-side changes.

## Disk and resource checks

Filesystem space:

```bash
df -h /
```

Docker disk usage:

```bash
docker system df
```

Project container resource snapshot:

```bash
mapfile -t ids < <(scompose ps -q)
((${#ids[@]})) && docker stats --no-stream "${ids[@]}"
```

Host memory/swap:

```bash
free -h
```

Reserved/listening ports:

```bash
sudo ss -lntp \
  | grep -E ':(8000|3000|3100|12345|8333)\b' \
  || true
```

Low disk space should be addressed before it becomes an outage. Inspect Docker
images/build cache, PostgreSQL backups, Kafka/Loki retention, and application
logs before deleting anything. Do not remove named volumes as a generic cleanup
step.

## Deployment logs

The deployment assistant writes protected logs under:

```text
/var/log/sahabino-deploy-assistant/
```

Recent files:

```bash
sudo ls -lht /var/log/sahabino-deploy-assistant/ | head -n 20
```

Tail the newest wrapper log:

```bash
sudo tail -n 200 "$(sudo ls -1t /var/log/sahabino-deploy-assistant/run-*.log | head -n1)"
```

## PostgreSQL backups

Backup timer:

```bash
sudo systemctl status sahabino-postgres-backup.timer
sudo systemctl list-timers sahabino-postgres-backup.timer
```

Recent backup service logs:

```bash
sudo journalctl -u sahabino-postgres-backup.service --since today
```

List backups:

```bash
sudo find /var/backups/sahabino/postgres \
  -maxdepth 1 \
  -type f \
  -name '*.dump' \
  -printf '%TY-%Tm-%Td %TH:%TM %10s %p\n' \
  | sort
```

Validate one archive without restoring it:

```bash
sudo pg_restore --list /var/backups/sahabino/postgres/NAME.dump >/dev/null
```

The deployment playbook creates/verifies a pre-deployment backup before
migrations. Restore is intentionally guarded; use the deployment documentation
rather than improvising a `pg_restore` into production.

## Common interpretations

### Loki returns 503 immediately after startup

`/ready` may temporarily return:

```text
Ingester not ready: waiting for 15s after being ready
```

Retry. A persistent 503 or restart loop needs Loki logs.

### A service is restarting

```bash
scompose ps -a SERVICE
scompose logs --tail=200 SERVICE
```

Do not immediately recreate every container; diagnose the failed service first.

### Crawler shows `APP_NOT_FOUND`

Check whether the package is genuinely unavailable for the configured store
locale. Other applications continuing to succeed and the scheduler completing
its run means the overall crawler process is still functioning.

### Kafka publish/ingestion concern

Check all three signals together:

```bash
scompose ps kafka ingestion
scompose logs --since=15m ingestion | tail -n 100
scompose exec -T kafka \
  /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:19092 \
  --describe \
  --group sahabino-ingestion-v1
```

A one-off historical task error is different from a current non-zero consumer
lag or a repeatedly restarting worker.

### Bind-mounted config permission failure

Do not `chmod -R 777` the checkout. Run the canonical permission/health repair
through the assistant:

```bash
sudo /opt/sahabino/app/deploy/ansible/sahabino-deploy.sh --verify
```

The assistant rebuilds tracked file modes from the Git index, protects secrets,
validates relative Compose bind sources, and refuses symlink escapes.

## Recommended operational habits

- Deploy source/config/schema changes with the deployment assistant.
- Use `stop`/`up -d` for routine temporary shutdown/startup.
- Use `down`/`up -d` only when you intentionally want container/network
  recreation.
- Never use `down -v` as a normal restart command.
- Keep Grafana/Loki/Alloy private and access them through SSH forwarding.
- Treat `LAG=0`, successful crawl completions, increasing DB counts, and HTTP
  readiness together as end-to-end evidence.
- Investigate disk growth before the filesystem is nearly full.
- Keep production Git state clean; make source changes locally, commit/push, and
  deploy a revision rather than editing tracked files on the VPS.
