# Sahabino — Production Acceptance & End-to-End Verification

**Review date:** 2026-09-18. Based on the supplied source archive and GitHub `MME1893/sahabino` (`main` inspected at `c154daf`). **This is a procedure, not evidence that any VPS check has passed.** Run on the real server and record each result as PASS / FAIL / NOT RUN with timestamp, deployed SHA and error details (redact credentials, review text, tokens and presigned URLs).

## Safety / scope

- Sections 1–9 are observation-oriented: no service startup, restart, migration, topic creation, network attachment or database writes. `metabase_sync.py plan` is definition-write-free but can execute SELECTs
against the live DB; it can consume resources. Existing GET APIs can also
update access logs. "Read-only" below means **no intended application/Compose
state mutation**, not zero side effects anywhere.
- Section 10 is a deliberately **state-changing** manual crawl. It temporarily stops/resumes the scheduler, contacts Google Play for **all active applications**, produces Kafka messages and persists application/review data. Obtain operator approval and take/verify the appropriate backup first. Do not use it to test an isolated app unless the implementation supports such scope.
- Section 11 is a deliberately **state-changing** sentiment drain of real pending observations. Its CPU model/cache, DB network, dedicated env file and absence of another worker must be confirmed first.
- Do **not** run `scripts/smoke-full-pipeline.sh` or `scripts/smoke-network-pipeline.sh` against production: they assume development Compose and can perform migrations, service changes, seeding or synthetic uploads. Run their tests on a disposable, production-like environment instead.
- Never run `docker compose down -v`, `docker volume prune`, `docker system prune`, Kafka offset resets, or a production `alembic upgrade` merely as a health check. Never display `.env`, secrets, presigned URLs or expanded Compose config.
- The deploy assistant `--verify` does not deploy a release, but it **can normalize checkout file permissions**; treat it as an approved maintenance step rather than strictly read-only.

## 1. Main VPS: enter the correct checkout; verify deployed revision and Compose model

```bash
sudo -iu sahabino
cd /opt/sahabino/app
scompose() {
  docker compose --project-name sahabino --env-file .env \
    --file docker-compose.yml --file compose.prod.yml \
    --profile observability --profile network "$@"
}
spsql() {
  scompose exec -T postgres sh -c \
    'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off "$@"' sh "$@"
}
pwd
git rev-parse HEAD
git status --short
sudo cat /opt/sahabino/DEPLOYED_REVISION
scompose config --quiet
scompose ps -a
```

**PASS:** deployed SHA agrees with the checkout HEAD and intended GitHub revision; no unexplained worktree changes; `config --quiet` succeeds; all ten expected main services are present and running: `postgres kafka api crawler ingestion seaweedfs network-analyzer loki alloy grafana`. Note that `sentiment` and `metabase` are **independent** and should not be searched for in this Compose project. The `DEPLOYED_REVISION` marker records last fully verified release, not necessarily the currently healthy state.

## 2. Host and Docker runtime (read only)

```bash
docker info --format 'Docker server={{.ServerVersion}} root={{.DockerRootDir}}'
docker ps -a --filter label=com.docker.compose.project=sahabino \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
docker ps -q --filter label=com.docker.compose.project=sahabino |
  xargs -r docker inspect --format '{{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}} restarts={{.RestartCount}} OOM={{.State.OOMKilled}}'
df -h / /var/lib/docker 2>/dev/null || df -h /
free -h
docker system df
mapfile -t IDS < <(scompose ps -q)
((${#IDS[@]})) && docker stats --no-stream "${IDS[@]}"
sudo ss -lntp | grep -E ':(8000|3000|3001|3100|12345|8333)\b' || true
```

**PASS:** no unexpected `Exited`/`Restarting`, OOM or growing restarts; sufficient free resources; bind addresses match intended access. Main ports `3000/3100/12345` and private SeaweedFS `8333` should bind loopback; API exposure is an explicit deployment choice. PostgreSQL and Kafka must not publish ports in production. `no-healthcheck` is not a failure for services without a Docker healthcheck; inspect logs and end-to-end behavior instead. `3001` is the optional, independent Metabase port.

## 3. Base readiness, schema and API

```bash
scompose exec -T postgres sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
scompose exec -T kafka /opt/kafka/bin/kafka-broker-api-versions.sh \
  --bootstrap-server localhost:19092 | head -n 12
scompose exec -T api uv run --no-sync alembic current
scompose exec -T api uv run --no-sync alembic heads
curl -fsS -o /dev/null -w 'API HTTP %{http_code}\n' http://127.0.0.1:8000/docs
curl -fsS 'http://127.0.0.1:8000/applications?active=true' | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/categories | python3 -m json.tool
```

**PASS:** PostgreSQL accepts connections; Kafka responds; `alembic current` equals the sole `heads` revision (in this source, `20260917_0008`); API HTTP 200; expected active packages and categories appear. `alembic current/heads` are inspections, **not** migration commands. If an API response might contain customer identifiers, keep output private.

## 4. PostgreSQL — volume, freshness and real crawler output

```bash
spsql <<'SQL'
SELECT version_num FROM alembic_version;
SELECT 'applications' AS metric, count(*) AS row_count FROM applications UNION ALL
SELECT 'crawl_runs',count(*) FROM crawl_runs UNION ALL
SELECT 'crawl_tasks',count(*) FROM crawl_tasks UNION ALL
SELECT 'ingested_events',count(*) FROM ingested_events UNION ALL
SELECT 'playstore_app_snapshots',count(*) FROM playstore_app_snapshots UNION ALL
SELECT 'reviews',count(*) FROM reviews UNION ALL
SELECT 'review_observations',count(*) FROM review_observations UNION ALL
SELECT 'network_captures',count(*) FROM network_captures UNION ALL
SELECT 'network_analysis_results',count(*) FROM network_analysis_results;
SELECT id, trigger_type, status, started_at, finished_at, created_at
FROM crawl_runs ORDER BY created_at DESC LIMIT 10;
SELECT a.package_name, ct.task_type, ct.status, ct.language_code, ct.country_code,
       ct.attempt_count, ct.error_code, ct.finished_at
FROM crawl_tasks ct JOIN applications a ON a.id=ct.application_id
ORDER BY ct.created_at DESC LIMIT 30;
SELECT a.package_name, ct.country_code, ct.language_code,
       s.score, s.ratings_count, s.reviews_count, s.min_installs,
       s.source_adapter, s.collected_at
FROM playstore_app_snapshots s
JOIN crawl_tasks ct ON ct.id=s.crawl_task_id
JOIN applications a ON a.id=s.application_id
ORDER BY s.collected_at DESC LIMIT 20;
SELECT event_type, count(*) AS total, max(processed_at) AS last_processed
FROM ingested_events GROUP BY event_type ORDER BY event_type;
SELECT count(*) AS observations, max(observed_at) AS last_observed
FROM review_observations;
SQL
```

**PASS:** expected tables exist; recent successful `app_details` tasks have matching snapshots and realistic metadata; review tasks and observations correspond to the sampled review pipeline; timestamps are fresh relative to the actual crawl schedule. A `partially_failed` run requires inspecting *which* app failed; totals alone do not prove the latest crawl succeeded. Zero review rows may be legitimate for an empty package or source restriction; distinguish it from a successful fetch with positive review count. Historic locale is recorded per `crawl_tasks`, while an app can override default locale.

## 5. Kafka — topics, live consumers and backlog

```bash
scompose exec -T kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:19092 --list
scompose exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:19092 --describe --group sahabino-ingestion-v1
scompose exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:19092 --describe --state --group sahabino-network-analyzer-v1
scompose exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:19092 --describe --group sahabino-network-analyzer-v1
scompose logs --since=30m --tail=150 kafka ingestion network-analyzer
```

**PASS:** expected application/review and network topics exist; ingestion eventually reaches zero lag for active assigned partitions; analyzer group is stable with a live member. `LAG=0` without any recent input is only an idle-state observation, not a successful crawl/ingestion proof. Initial uncommitted offsets can display `-`; investigate in context rather than interpreting as zero. If production group IDs differ from defaults, read their **names only** from the approved private settings, without printing their containing secrets.

## 6. S3 / network analyzer — read-only state and analytical outputs

```bash
curl -fsS http://127.0.0.1:8333/status
scompose exec -T network-analyzer tshark --version | head -n 3
# API CaptureRead exposes object_key, filename, hashes and error_message.
# Do not print or save its unredacted JSON in ordinary test logs.
curl -fsS -o /dev/null -w 'Network API HTTP %{http_code}\n' \
  'http://127.0.0.1:8000/network-captures'
spsql <<'SQL'
SELECT status, scenario, count(*) AS captures,
       max(created_at) AS newest_capture, max(analysis_finished_at) AS newest_analysis
FROM network_captures GROUP BY status,scenario ORDER BY status,scenario;
SELECT nc.id, nc.scenario, nc.status, nc.analysis_attempt_count,
       nc.created_at, nc.analysis_finished_at,
       (nar.capture_id IS NOT NULL) AS result_persisted,
       nar.packet_count, nar.comparison_ready,
       nar.effective_file_throughput_mbps
FROM network_captures nc
LEFT JOIN network_analysis_results nar ON nar.capture_id=nc.id
ORDER BY nc.created_at DESC LIMIT 15;
SQL
```

**PASS:** SeaweedFS responds; TShark installed; existing `analyzed` captures have corresponding result rows and plausible non-null metric fields **where the capture format/evidence supports them**. `comparison_ready=false`/NULL can legitimately represent inadequate or non-comparable packet evidence. Do not expose `object_key`, filenames, hashes, free-text error messages,
credentials, download URLs or raw PCAP; the capture-list API currently returns
some of those fields, so check only its HTTP status and use restricted SQL for
routine diagnostics. A complete synthetic upload→analyzer→ingestion check belongs in a disposable environment, not the routine production check.

## 7. Observability — Grafana, Loki, Alloy, dashboard **data**

```bash
curl -fsS http://127.0.0.1:3100/ready && echo
curl -fsS http://127.0.0.1:3000/api/health | python3 -m json.tool
curl -fsS http://127.0.0.1:12345/-/ready || true
curl -fsSG 'http://127.0.0.1:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={service="sahabino-crawler"}' \
  --data-urlencode 'limit=2' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("Loki status:",d.get("status"),"streams:",len(d.get("data",{}).get("result",[])),"entries:",sum(len(x.get("values",[])) for x in d.get("data",{}).get("result",[])))'
scompose logs --tail=100 alloy loki grafana
scompose exec -T grafana sh -c \
  'if [ -n "$GRAFANA_POSTGRES_PASSWORD" ]; then echo "PostgreSQL datasource credential enabled"; else echo "PostgreSQL datasource disabled"; fi'
```

**PASS:** Loki ready; Grafana health succeeds; the Loki query returns *recent* crawler logs after a real crawl (adjust Grafana Explore time range to last 24h). If Alloy endpoint `/-/ready` does not exist in the pinned version, do not fail solely on that optional HTTP check; confirm Alloy container and Loki ingestion. Grafana UI: check provisioning of `sahabino-logging-smoke` plus the four PostgreSQL-backed dashboards (`overview`, `crawler`, `reviews`, `application-explorer`); the latter require the optional `grafana_reader` datasource/password to be enabled. Check datasource UID `sahabino-postgres`, user `grafana_reader`, Loki UID `sahabino-loki`. Open each dashboard, change app/time filters, verify panels produce current results, compare a count with the SQL in section 4. A healthy `/api/health` **does not** prove dashboards, datasource authentication, log shipping or query correctness.

SSH tunnel **from your own workstation** (replace `SERVER_IP`; keep the session running):

```bash
ssh -N -L 3000:127.0.0.1:3000 -L 3100:127.0.0.1:3100 \
  -L 12345:127.0.0.1:12345 sahabino@SERVER_IP
```

Visit `http://127.0.0.1:3000`. Keep these services loopback-only on the VPS.

## 8. Standalone Metabase — health, definition drift, actual dashboard results

```bash
# On VPS, in a separate shell with Docker access:
if [ -d /opt/sahabino-bi ]; then
  cd /opt/sahabino-bi
  docker compose --env-file .env -f compose.yml config --quiet
  docker compose --env-file .env -f compose.yml ps -a
  curl -fsS http://127.0.0.1:3001/api/health | python3 -m json.tool
  python3 scripts/validate.py
  python3 scripts/network_attach.py discover --project sahabino --service postgres
  python3 scripts/preflight.py --project sahabino --service postgres \
    --network sahabino-bi-db \
    --reader-password-file secrets/bi_reader_password \
    --metadata-password-file secrets/metabase_app_db_password
  python3 scripts/metabase_sync.py plan
else
  echo 'NOT RUN: independent BI checkout /opt/sahabino-bi not found; locate actual BI checkout'
fi
```

**PASS:** Metabase is `healthy` with `{"status":"ok"}`; main PostgreSQL container remains on `sahabino-bi-db` with alias `sahabino-postgres-bi` (main PostgreSQL recreation can break this attachment); preflight has no BLOCKER. `plan` should show SKIP for already-deployed unchanged eligible objects. `CREATE`/`UPDATE` needs operator review; `SKIP_CAPABILITY` can be legitimate when sentiment/network/release evidence or grants are missing. **Do not run `apply` simply to refresh data**; it writes Metabase content definitions.

Current manifest defines **3 collections, 42 questions and 7 dashboards, 45 total card placements**. Actual deployed count may be smaller due to capabilities, configuration or earlier sync: verify the actual provisioned items in the UI, not merely the manifest. On your workstation:

```bash
ssh -N -L 13001:127.0.0.1:3001 sahabino@SERVER_IP
```

Visit `http://127.0.0.1:13001`, open `Sahabino BI → Dashboards`, execute all eligible cards, test application/locale/date filters, inspect empty states and SQL errors, and compare at least one dashboard result with a direct SQL query run through the restricted `sahabino_bi_reader`. For network/release interpretation, require actual capture and experiment/release provenance; an empty readiness panel is not a benchmark result.

## 9. Sentiment — installed vs running, processing coverage, labels, freshness

```bash
# From /opt/sahabino/app with scompose/spsql defined:
docker ps -a --filter name=sahabino-sentiment \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker image inspect sahabino-sentiment:local --format '{{.Id}} {{.Created}}' 2>/dev/null \
  || echo 'NOT RUN: local sentiment image not installed (check deployment location)'
test -d /srv/sahabino-models/hf-hub && echo 'model cache directory exists' \
  || echo 'model cache directory missing'
spsql <<'SQL'
SELECT sentiment_status, count(*) AS observations,
       max(sentiment_processed_at) AS last_processed,
       max(sentiment_attempt_count) AS max_attempts
FROM review_observations GROUP BY sentiment_status ORDER BY sentiment_status;
SELECT sentiment_language, sentiment_label, count(*) AS observations,
       max(sentiment_processed_at) AS last_processed
FROM review_observations
WHERE sentiment_status='done'
GROUP BY sentiment_language,sentiment_label
ORDER BY sentiment_language,sentiment_label;
SELECT count(*) FILTER (WHERE sentiment_status='pending') AS pending,
       count(*) FILTER (WHERE sentiment_status='failed') AS failed,
       count(*) FILTER (WHERE sentiment_status='done' AND
         (sentiment_language NOT IN ('fa','en') OR
          sentiment_label NOT IN ('positive','neutral','negative') OR
          sentiment_label IS NULL OR sentiment_language IS NULL)) AS invalid_done
FROM review_observations;
SQL
```

**PASS:** required migration 0007/0008 present, exact pinned model cache available if Persian inference will run, `done` has only valid fa/en × positive/neutral/negative, no unexpected persistent failures, and recent new observations are eventually processed **when the worker's manually scheduled execution has run**. Historical observations may remain `skipped`, and an on-demand `--once` container is expected to be `Exited`/absent between runs. Current architecture does **not** include an always-running sentiment service in main Compose. Do not inspect raw review contents to check status.

## 10. Approved write test — ONE real manual crawl on production

**Only with an approved maintenance window and verified backup.** The live
scheduler could already be in an active crawl when stopped: first inspect
`crawl_runs` for running runs, wait for normal completion, then make sure no
other operator or automation will start an overlapping crawl. Backups and
maintenance authorization cannot be automated by this block. Requires the section 1 `scompose` and `spsql` functions in the *same Bash shell*. First capture baseline counts, most recent run, and Kafka lag with sections 4 and 5. The following temporarily stops the scheduler if it was running, guarantees an attempt to restore its former running state on normal script failure/exit, and creates real application/review records. Do not run it concurrently with the scheduled crawler.

```bash
(
  set -Eeuo pipefail
  umask 077
  WAS_RUNNING=0
  if scompose ps --services --filter status=running | grep -qx crawler; then
    WAS_RUNNING=1
  fi
  restore_scheduler() {
    if [ "$WAS_RUNNING" -eq 1 ]; then
      scompose start crawler || echo 'ALERT: crawler scheduler restore failed' >&2
    fi
  }
  trap restore_scheduler EXIT
  if [ "$WAS_RUNNING" -eq 1 ]; then scompose stop crawler; fi
  LOG="$HOME/sahabino-manual-crawl-$(date -u +%Y%m%dT%H%M%SZ).log"
  scompose run --rm --no-deps crawler \
    uv run --no-sync python -m sahabino.crawler crawl-once 2>&1 | tee "$LOG"
  RUN_ID="$(grep -E '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$' "$LOG" | tail -n 1)"
  test -n "$RUN_ID" || { echo 'FAIL: crawl run ID not found'; exit 1; }
  printf 'MANUAL_CRAWL_RUN_ID=%s\n' "$RUN_ID"
  spsql -v run_id="$RUN_ID" <<'SQL'
SELECT id,status,started_at,finished_at FROM crawl_runs WHERE id=:'run_id'::uuid;
SELECT a.package_name,ct.task_type,ct.status,ct.country_code,ct.language_code,
       ct.attempt_count,ct.error_code,ct.finished_at
FROM crawl_tasks ct JOIN applications a ON a.id=ct.application_id
WHERE ct.crawl_run_id=:'run_id'::uuid ORDER BY a.package_name,ct.task_type;
SELECT count(*) AS snapshots_in_run FROM playstore_app_snapshots s
JOIN crawl_tasks ct ON ct.id=s.crawl_task_id
WHERE ct.crawl_run_id=:'run_id'::uuid;
SELECT count(*) AS review_observations_in_run FROM review_observations o
JOIN crawl_tasks ct ON ct.id=o.crawl_task_id
WHERE ct.crawl_run_id=:'run_id'::uuid;
SQL
)
```

After completion, **recheck** scheduler `scompose ps crawler`, recent snapshots/observations, and the Kafka ingestion group until lag settles. PASS only if the specific `RUN_ID` and its tasks have expected outcomes, Kafka carries corresponding events, ingestion commits them, and DB results are present. `partially_failed` must be explained per package. An empty review observation count is not automatically a bug without evidence that reviews were actually returned.

## 11. Approved write test — process existing sentiment backlog once

Only if sentiment was deliberately installed, the model cache exists at the **pinned revision**, there is pending work to process, a dedicated private sentiment env file exists, and no running `sahabino-sentiment-watch` / `sahabino-sentiment-once` worker is active. The worker uses a PostgreSQL advisory lock; do not launch a second worker. Select the real PostgreSQL-reachable network by inspecting, not guessing:

```bash
cd /opt/sahabino/app
docker inspect "$(scompose ps -q postgres)" \
  --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}'
# Fill these with VERIFIED values from operator configuration, never paste credentials:
# NET='VERIFIED_POSTGRES_DOCKER_NETWORK'
# SENTIMENT_ENV='/secure/path/sentiment.env'
# Inspect status and model revision first (see sentiment/README.md).
# docker run --rm --name sahabino-sentiment-once \
#   --network "$NET" --env-file "$SENTIMENT_ENV" \
#   --mount type=bind,src=/srv/sahabino-models/hf-hub,dst=/models/hf-hub,readonly \
#   sahabino-sentiment:local --once
```

Compare `sentiment_status` counts and `max(sentiment_processed_at)` from section 9 **before/after**; inspect the worker's `run_complete` JSON and any failure counts. `done` + `skipped` growth with pending drain can indicate progress; `skipped` is **not neutral**. This action changes production data and should never be advertised as a read-only diagnostic.

## 12. Separate tests on a disposable dev/staging host (NOT against production)

Tests may create and destroy disposable Docker resources and must not inherit
production `TEST_DATABASE_URL`, `TEST_KAFKA_BOOTSTRAP_SERVERS`, `.env`, API
base URLs, or secret mounts. Verify the active environment is isolated first.

```bash
# From the checked-out repository root with development dependencies installed:
uv run --no-sync pytest -q tests/unit tests/deployment
uv run --no-sync pytest -q tests/integration
uv run --no-sync pytest -q sentiment/tests/unit
# Only if TShark and test prerequisites are installed:
uv run --no-sync pytest -q tests/system/network
# BI tests may start a disposable Docker PostgreSQL instance; no production DB:
cd bi
python3 scripts/validate.py
python3 -m pytest -q tests
python3 tests/run_postgres_validation.py
```

Use `scripts/smoke-full-pipeline.sh` and `scripts/smoke-network-pipeline.sh` **only on an isolated, disposable development Compose project**, after reading each script's stateful side effects and ensuring its `.env` points to development resources. Real ParsBERT model smoke is opt-in and requires offline model cache; a default skip is NOT executed coverage (`sentiment/README.md`). Record any unavailable dependency, skipped Docker integration or missing external Google Play access as NOT RUN, not PASS.

## 13. Final acceptance record

For backups, first inspect the existing backup timer, last backup timestamp,
archive presence and an approved restore-drill record (see
`docs/operations/README.md`); `pg_restore --list` checks archive structure,
**not** restorability. Separate Metabase metadata/state/key and SeaweedFS
object backups from the application PostgreSQL dump. Do not claim an E2E PASS
if an optional subsystem required by the presentation is missing or its
real output was not checked.

Record: UTC timestamp, deployed Git SHA vs intended GitHub SHA, all main containers' actual state / restart count, Postgres/Kafka health, schema head, last crawler run ID with success/failure per app, Kafka ingestion/analyzer lag, snapshot and review observation deltas for the exact run, network analyzed/result consistency, sentiment counts before/after a scheduled worker cycle, Loki post-run log query, Grafana datasource/panel outputs, Metabase preflight/plan and card execution, backup freshness and restore-drill date. Classify each check PASS / FAIL / NOT RUN. A full PASS requires proof of data flow and dashboard outputs, not just healthy HTTP status.

## 14. Suggested evidence matrix (fill in on the VPS)

| Check | Required evidence | Result (PASS / FAIL / NOT RUN / NOT APPLICABLE) |
|---|---|---|
| Release/Compose | intended SHA, actual SHA, config quiet, 10 service states | |
| Backup | dated archive + protected matching state + separate restore drill | |
| API/PostgreSQL | HTTP, current/head, real newest crawl/task/snapshot rows | |
| Kafka | four expected topics, live group members, lag after an actual event | |
| Crawler→ingestion | one approved run ID, task status, resulting rows | |
| Network | S3/TShark, analyzed rows/result pairing; approved real capture if in scope | |
| Sentiment | queue/status baseline + real worker cycle and valid labels if in scope | |
| Loki/Grafana | post-run logs, available datasources, every required panel | |
| Metabase | preflight, plan, actual available cards/filters and SQL parity | |

Document limitations and expected empty data instead of converting an
unexecuted or ineligible check into a PASS. This guide supplies commands;
it does not certify the server without captured output.
