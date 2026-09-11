#!/usr/bin/env bash
set -Eeuo pipefail

# Sahabino full runtime smoke test
#
# Verifies:
#   PostgreSQL + Alembic
#   Kafka + required topics
#   Registry API
#   controlled one-shot crawler run
#   Kafka production/backlog
#   ingestion catch-up + offset commits
#   ingestion database effects
#   Alloy -> Loki structured logs
#   Grafana/Loki provisioning
#
# This is a runtime/system smoke test. It does NOT replace pytest integration tests.
#
# Usage:
#   ./scripts/smoke-full-pipeline.sh
#   ./scripts/smoke-full-pipeline.sh --build
#   ./scripts/smoke-full-pipeline.sh --seed
#   ./scripts/smoke-full-pipeline.sh --skip-observability
#   ./scripts/smoke-full-pipeline.sh --down-after
#
# Environment overrides:
#   SMOKE_TIMEOUT_SECONDS=180
#   SMOKE_REQUIRE_REVIEWS=1
#   SMOKE_CRAWLER_LOG_DISCOVERY_SECONDS=12
#
# Notes:
# - The default path is cache-friendly: it uses --no-build for compose startup.
# - Pass --build only when project images are missing or code changed.
# - No volumes are removed.
# - Existing active registry applications are used.
# - --seed creates Telegram + WhatsApp only when those packages are absent.
# - The real crawler contacts Google Play.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TIMEOUT_SECONDS="${SMOKE_TIMEOUT_SECONDS:-180}"
REQUIRE_REVIEWS="${SMOKE_REQUIRE_REVIEWS:-1}"
CRAWLER_LOG_DISCOVERY_SECONDS="${SMOKE_CRAWLER_LOG_DISCOVERY_SECONDS:-12}"

BUILD=0
SEED=0
SKIP_OBSERVABILITY=0
DOWN_AFTER=0

while (($#)); do
  case "$1" in
    --build)
      BUILD=1
      ;;
    --seed)
      SEED=1
      ;;
    --skip-observability)
      SKIP_OBSERVABILITY=1
      ;;
    --down-after)
      DOWN_AFTER=1
      ;;
    -h|--help)
      sed -n '3,34p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

TMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t sahabino-smoke)"
CRAWL_LOG="$TMP_DIR/crawl.log"

CRAWLER_WAS_RUNNING=0
CRAWLER_STOPPED_BY_US=0
INGESTION_STOPPED_BY_US=0
RESTORE_ON_EXIT=1

APP_STATS_TOPIC="playstore.app-stats.v1"
REVIEWS_TOPIC="playstore.review-observed.v1"
NETWORK_TOPIC="network.analysis-collected.v1"

log() {
  printf '\n==> %s\n' "$*"
}

ok() {
  printf '    [OK] %s\n' "$*"
}

warn() {
  printf '    [WARN] %s\n' "$*" >&2
}

die() {
  printf '    [FAIL] %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

service_running() {
  [[ -n "$(docker compose ps --status running -q "$1" 2>/dev/null || true)" ]]
}

cleanup() {
  local exit_code=$?

  rm -rf "$TMP_DIR" >/dev/null 2>&1 || true

  if ((RESTORE_ON_EXIT)); then
    if ((INGESTION_STOPPED_BY_US)) && ! service_running ingestion; then
      warn "Restoring ingestion service after interrupted smoke run"
      docker compose up -d --no-build ingestion >/dev/null 2>&1 || true
    fi

    if ((CRAWLER_STOPPED_BY_US)) && ! service_running crawler; then
      warn "Restoring crawler scheduler to its original running state"
      docker compose start crawler >/dev/null 2>&1 || true
    fi
  fi

  if ((exit_code != 0)); then
    printf '\nSmoke test FAILED (exit %s).\n' "$exit_code" >&2
  fi

  exit "$exit_code"
}
trap cleanup EXIT
trap 'printf "\nError on line %s while running: %s\n" "$LINENO" "$BASH_COMMAND" >&2' ERR

wait_for() {
  local description="$1"
  local timeout="$2"
  shift 2

  local started=$SECONDS
  while ! "$@"; do
    if ((SECONDS - started >= timeout)); then
      die "Timed out waiting for: $description"
    fi
    sleep 2
  done
  ok "$description"
}

http_ok() {
  curl -fsS --max-time 3 "$1" >/dev/null
}

psql_scalar() {
  local sql="$1"
  printf '%s\n' "$sql" \
    | docker compose exec -T postgres sh -lc \
      'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq' \
    | tr -d '\r'
}

db_counts() {
  psql_scalar "
SELECT
    (SELECT count(*) FROM ingested_events)::text || ' ' ||
    (SELECT count(*) FROM playstore_app_snapshots)::text || ' ' ||
    (SELECT count(*) FROM reviews)::text || ' ' ||
    (SELECT count(*) FROM review_observations)::text;
"
}

topic_end_offset() {
  local topic="$1"
  docker compose exec -T kafka sh -lc \
    "/opt/kafka/bin/kafka-get-offsets.sh --bootstrap-server localhost:19092 --topic '$topic'" \
    | tr -d '\r' \
    | awk -F: '$3 ~ /^[0-9]+$/ { total += $3 } END { print total + 0 }'
}

group_lag() {
  docker compose exec -T kafka sh -lc \
    "/opt/kafka/bin/kafka-consumer-groups.sh \
      --bootstrap-server localhost:19092 \
      --describe \
      --group '$GROUP_ID'" 2>/dev/null \
    | tr -d '\r' \
    | awk '
        $2 == "playstore.app-stats.v1" || $2 == "playstore.review-observed.v1" {
          found = 1
          if ($6 ~ /^[0-9]+$/) {
            lag += $6
          } else if ($4 == "-" && $5 ~ /^[0-9]+$/) {
            lag += $5
          }
        }
        END {
          if (found) print lag + 0
          else print -1
        }
      '
}

ingestion_caught_up() {
  local lag
  lag="$(group_lag)"

  if [[ "$lag" =~ ^[0-9]+$ ]] && ((lag == 0)); then
    return 0
  fi

  if [[ "$lag" == "-1" ]]; then
    local total
    total=$(( $(topic_end_offset "$APP_STATS_TOPIC") + $(topic_end_offset "$REVIEWS_TOPIC") ))
    ((total == 0)) && return 0
  fi

  return 1
}

topic_exists() {
  local topic="$1"
  docker compose exec -T kafka sh -lc \
    '/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:19092 --list' \
    | tr -d '\r' \
    | grep -Fxq "$topic"
}

seed_application_if_missing() {
  local name="$1"
  local package_name="$2"

  local all_json active_json
  all_json="$(curl -fsS 'http://localhost:8000/applications')"
  active_json="$(curl -fsS 'http://localhost:8000/applications?active=true')"

  if printf '%s' "$active_json" \
    | grep -Eq "\"package_name\"[[:space:]]*:[[:space:]]*\"${package_name}\""; then
    ok "$package_name already active"
    return 0
  fi

  if printf '%s' "$all_json" \
    | grep -Eq "\"package_name\"[[:space:]]*:[[:space:]]*\"${package_name}\""; then
    die "$package_name exists but is inactive; current Registry API has no reactivation endpoint"
  fi

  curl -fsS -X POST 'http://localhost:8000/applications' \
    -H 'Content-Type: application/json' \
    -d "{
      \"name\": \"${name}\",
      \"package_name\": \"${package_name}\",
      \"category_codes\": [\"messaging\", \"social_network\"],
      \"primary_category_code\": \"messaging\"
    }" >/dev/null

  ok "created active application $package_name"
}

db_reached_targets() {
  local current_ingested current_snapshots current_reviews current_observations
  read -r current_ingested current_snapshots current_reviews current_observations <<<"$(db_counts)"

  ((current_ingested == EXPECTED_INGESTED_TOTAL)) \
    && ((current_snapshots == EXPECTED_SNAPSHOT_TOTAL)) \
    && ((current_observations == EXPECTED_OBSERVATION_TOTAL))
}

loki_has_service_logs() {
  local service="$1"
  local now start response

  now="$(date +%s)"
  start=$((now - 3600))

  local start_ns end_ns
  start_ns=$((start * 1000000000))
  end_ns=$((now * 1000000000))

  response="$(
    curl -fsSG --max-time 5 'http://localhost:3100/loki/api/v1/query_range' \
      --data-urlencode "query={service=\"${service}\"}" \
      --data-urlencode "start=${start_ns}" \
      --data-urlencode "end=${end_ns}" \
      --data-urlencode 'limit=20'
  )" || return 1

  printf '%s' "$response" \
    | grep -Eq "\"service\"[[:space:]]*:[[:space:]]*\"${service}\""
}

preflight() {
  log "Preflight"

  require_command docker
  require_command curl
  require_command grep
  require_command awk
  require_command sed

  [[ -f docker-compose.yml ]] || die "docker-compose.yml not found at repository root"
  [[ -f Dockerfile ]] || die "Dockerfile not found at repository root"

  docker info >/dev/null 2>&1 || die "Docker daemon is not available"
  docker compose version >/dev/null 2>&1 || die "docker compose is not available"
  docker compose config >/dev/null
  docker compose --profile observability config >/dev/null

  ok "Docker daemon and Compose configuration"
}

build_images_if_requested() {
  if ((BUILD)); then
    log "Build project images using Docker/uv cache"
    docker compose build api crawler ingestion
    ok "project images built"
  else
    log "Build step skipped"
    printf '    Using existing project images (--no-build). Use --build after source/dependency changes.\n'
  fi
}

start_foundation() {
  log "Start PostgreSQL and Kafka"

  docker compose up -d --no-build postgres kafka

  wait_for "PostgreSQL healthy" "$TIMEOUT_SECONDS" \
    docker compose exec -T postgres sh -lc \
      'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null'

  wait_for "Kafka broker ready" "$TIMEOUT_SECONDS" \
    docker compose exec -T kafka sh -lc \
      '/opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:19092 >/dev/null 2>&1'
}

stop_scheduler_if_needed() {
  if service_running crawler; then
    CRAWLER_WAS_RUNNING=1
    CRAWLER_STOPPED_BY_US=1
    log "Temporarily stop crawler scheduler"
    docker compose stop crawler >/dev/null
    ok "crawler scheduler stopped"
  fi
}

migrate_database() {
  log "Apply Alembic migrations"

  docker compose run --rm --no-deps api \
    uv run --no-sync alembic upgrade head

  local code_head db_head
  code_head="$(
    docker compose run --rm --no-deps api \
      uv run --no-sync alembic heads 2>/dev/null \
      | tr -d '\r' \
      | awk '/\(head\)/ { print $1; exit }'
  )"
  db_head="$(psql_scalar 'SELECT version_num FROM alembic_version;')"

  [[ -n "$code_head" ]] || die "Could not determine Alembic code head"
  [[ "$db_head" == "$code_head" ]] \
    || die "Database revision ($db_head) does not match code head ($code_head)"

  ok "Alembic database head = $db_head"

  local required_table
  for required_table in \
    applications crawl_runs crawl_tasks \
    ingested_events playstore_app_snapshots reviews review_observations; do
    [[ "$(psql_scalar "SELECT to_regclass('public.${required_table}') IS NOT NULL;")" == "t" ]] \
      || die "Required table missing: $required_table"
  done

  ok "required database tables exist"
}

ensure_topics() {
  log "Provision and verify Kafka topics"

  docker compose run --rm --no-deps api \
    uv run --no-sync python -m sahabino.messaging.admin

  topic_exists "$APP_STATS_TOPIC" || die "Missing Kafka topic: $APP_STATS_TOPIC"
  topic_exists "$REVIEWS_TOPIC" || die "Missing Kafka topic: $REVIEWS_TOPIC"
  topic_exists "$NETWORK_TOPIC" || die "Missing Kafka topic: $NETWORK_TOPIC"

  ok "required Kafka topics exist"
}

start_runtime_services() {
  log "Start API, ingestion and observability"

  docker compose up -d --no-build api ingestion

  wait_for "API ready" "$TIMEOUT_SECONDS" \
    http_ok 'http://localhost:8000/docs'

  wait_for "ingestion service running" "$TIMEOUT_SECONDS" \
    service_running ingestion

  GROUP_ID="$(
    docker compose exec -T ingestion sh -lc \
      'printf "%s" "$SAHABINO_INGESTION_CONSUMER_GROUP_ID"' \
    | tr -d '\r'
  )"
  [[ -n "$GROUP_ID" ]] || die "Could not read ingestion consumer group id"

  ok "ingestion consumer group = $GROUP_ID"

  if ((SKIP_OBSERVABILITY == 0)); then
    docker compose --profile observability up -d --no-build loki alloy grafana

    wait_for "Loki ready" "$TIMEOUT_SECONDS" \
      http_ok 'http://localhost:3100/ready'

    wait_for "Grafana ready" "$TIMEOUT_SECONDS" \
      http_ok 'http://localhost:3000/api/health'

    wait_for "Alloy container running" "$TIMEOUT_SECONDS" \
      service_running alloy

    grep -q 'stage\.docker' infrastructure/observability/alloy/config.alloy \
      || die "Alloy config is missing stage.docker {}; Docker JSON envelope would not be unwrapped"

    grep -Eq 'uid:[[:space:]]*sahabino-loki' \
      infrastructure/observability/grafana/provisioning/datasources/loki.yaml \
      || die "Grafana Loki datasource UID sahabino-loki is not provisioned"

    [[ -f infrastructure/observability/grafana/dashboards/sahabino-logging-smoke.json ]] \
      || die "Grafana smoke dashboard file is missing"

    if grep -Eq '"allValue"[[:space:]]*:[[:space:]]*"\.\*"' \
      infrastructure/observability/grafana/dashboards/sahabino-logging-smoke.json; then
      die 'Dashboard still contains allValue=".*"; Loki requires a non-empty-compatible matcher such as ".+"'
    fi

    ok "observability provisioning regression checks"
  fi
}

ensure_registry_apps() {
  log "Verify Application Registry"

  curl -fsS 'http://localhost:8000/applications?active=true' >/dev/null
  curl -fsS 'http://localhost:8000/categories' >/dev/null

  if ((SEED)); then
    seed_application_if_missing "Telegram" "org.telegram.messenger"
    seed_application_if_missing "WhatsApp" "com.whatsapp"
  fi

  ACTIVE_APPS="$(psql_scalar 'SELECT count(*) FROM applications WHERE is_active IS TRUE;')"
  [[ "$ACTIVE_APPS" =~ ^[0-9]+$ ]] || die "Could not determine active application count"
  ((ACTIVE_APPS > 0)) \
    || die "No active applications. Create one through the Registry API or run with --seed."

  ok "active applications = $ACTIVE_APPS"
}

drain_old_backlog() {
  log "Drain any pre-existing ingestion backlog before taking the smoke baseline"

  wait_for "ingestion consumer caught up before smoke crawl" "$TIMEOUT_SECONDS" \
    ingestion_caught_up

  ok "pre-existing Kafka backlog drained"
}

stop_ingestion_and_capture_baseline() {
  log "Stop ingestion and capture stable baseline"

  docker compose stop ingestion >/dev/null
  INGESTION_STOPPED_BY_US=1

  BASE_APP_END="$(topic_end_offset "$APP_STATS_TOPIC")"
  BASE_REVIEW_END="$(topic_end_offset "$REVIEWS_TOPIC")"

  read -r \
    BASE_INGESTED \
    BASE_SNAPSHOTS \
    BASE_REVIEWS \
    BASE_OBSERVATIONS <<<"$(db_counts)"

  printf '    Kafka baseline: app-stats=%s reviews=%s\n' \
    "$BASE_APP_END" "$BASE_REVIEW_END"
  printf '    DB baseline: ingested=%s snapshots=%s reviews=%s observations=%s\n' \
    "$BASE_INGESTED" "$BASE_SNAPSHOTS" "$BASE_REVIEWS" "$BASE_OBSERVATIONS"

  ok "stable baseline captured"
}

run_controlled_crawl() {
  log "Run one real crawler cycle while ingestion is stopped"

  set +e
  docker compose run --rm crawler \
    uv run --no-sync python -m sahabino.crawler crawl-once \
    2>&1 | tee "$CRAWL_LOG"
  local crawl_exit=${PIPESTATUS[0]}
  set -e

  ((crawl_exit == 0)) || die "crawler crawl-once failed with exit code $crawl_exit"

  RUN_ID="$(
    tr -d '\r' < "$CRAWL_LOG" \
      | grep -E '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' \
      | tail -1
  )"

  [[ -n "$RUN_ID" ]] || die "Could not extract crawl_run_id from crawler output"

  ok "crawl_run_id = $RUN_ID"
}

verify_crawl_lifecycle_and_kafka() {
  log "Verify crawler lifecycle and Kafka production"

  local run_status task_total task_succeeded app_tasks review_tasks expected_tasks

  run_status="$(psql_scalar "SELECT status FROM crawl_runs WHERE id = '${RUN_ID}'::uuid;")"
  [[ "$run_status" == "succeeded" ]] || die "crawl run status is '$run_status', expected succeeded"

  expected_tasks=$((ACTIVE_APPS * 2))
  task_total="$(psql_scalar "SELECT count(*) FROM crawl_tasks WHERE crawl_run_id = '${RUN_ID}'::uuid;")"
  task_succeeded="$(psql_scalar "SELECT count(*) FROM crawl_tasks WHERE crawl_run_id = '${RUN_ID}'::uuid AND status = 'succeeded';")"
  app_tasks="$(psql_scalar "SELECT count(*) FROM crawl_tasks WHERE crawl_run_id = '${RUN_ID}'::uuid AND task_type = 'app_details' AND status = 'succeeded';")"
  review_tasks="$(psql_scalar "SELECT count(*) FROM crawl_tasks WHERE crawl_run_id = '${RUN_ID}'::uuid AND task_type = 'reviews' AND status = 'succeeded';")"

  ((task_total == expected_tasks)) \
    || die "crawl task total=$task_total, expected=$expected_tasks"
  ((task_succeeded == expected_tasks)) \
    || die "succeeded task total=$task_succeeded, expected=$expected_tasks"
  ((app_tasks == ACTIVE_APPS)) \
    || die "succeeded app_details tasks=$app_tasks, expected=$ACTIVE_APPS"
  ((review_tasks == ACTIVE_APPS)) \
    || die "succeeded reviews tasks=$review_tasks, expected=$ACTIVE_APPS"

  NEW_APP_END="$(topic_end_offset "$APP_STATS_TOPIC")"
  NEW_REVIEW_END="$(topic_end_offset "$REVIEWS_TOPIC")"

  APP_EVENT_DELTA=$((NEW_APP_END - BASE_APP_END))
  REVIEW_EVENT_DELTA=$((NEW_REVIEW_END - BASE_REVIEW_END))
  TOTAL_EVENT_DELTA=$((APP_EVENT_DELTA + REVIEW_EVENT_DELTA))

  ((APP_EVENT_DELTA == ACTIVE_APPS)) \
    || die "AppStats Kafka delta=$APP_EVENT_DELTA, expected one event per active app ($ACTIVE_APPS)"

  if ((REQUIRE_REVIEWS)); then
    ((REVIEW_EVENT_DELTA > 0)) \
      || die "No ReviewObserved events were produced; set SMOKE_REQUIRE_REVIEWS=0 only for apps that legitimately have no reviews"
  fi

  ((TOTAL_EVENT_DELTA > 0)) || die "Crawler produced no Kafka records"

  printf '    New Kafka records: app-stats=%s reviews=%s total=%s\n' \
    "$APP_EVENT_DELTA" "$REVIEW_EVENT_DELTA" "$TOTAL_EVENT_DELTA"

  local observed_lag
  observed_lag="$(group_lag)"
  if [[ "$observed_lag" =~ ^[0-9]+$ ]] && ((observed_lag > 0)); then
    ok "Kafka backlog visible while ingestion is stopped (lag=$observed_lag)"
  else
    warn "Consumer-group lag is not fully observable yet (new partitions may have no committed offset); topic end-offset deltas prove production"
  fi

  ok "crawler lifecycle and Kafka production"
}

start_ingestion_and_verify_persistence() {
  log "Start ingestion and verify PostgreSQL effects + Kafka commits"

  EXPECTED_INGESTED_TOTAL=$((BASE_INGESTED + TOTAL_EVENT_DELTA))
  EXPECTED_SNAPSHOT_TOTAL=$((BASE_SNAPSHOTS + APP_EVENT_DELTA))
  EXPECTED_OBSERVATION_TOTAL=$((BASE_OBSERVATIONS + REVIEW_EVENT_DELTA))

  docker compose up -d --no-build ingestion
  INGESTION_STOPPED_BY_US=0

  wait_for "ingestion database targets reached" "$TIMEOUT_SECONDS" \
    db_reached_targets

  wait_for "Kafka consumer lag returned to zero" "$TIMEOUT_SECONDS" \
    ingestion_caught_up

  local final_ingested final_snapshots final_reviews final_observations
  read -r final_ingested final_snapshots final_reviews final_observations <<<"$(db_counts)"

  local review_row_delta=$((final_reviews - BASE_REVIEWS))

  ((final_ingested - BASE_INGESTED == TOTAL_EVENT_DELTA)) \
    || die "ingested_events delta mismatch"
  ((final_snapshots - BASE_SNAPSHOTS == APP_EVENT_DELTA)) \
    || die "playstore_app_snapshots delta mismatch"
  ((final_observations - BASE_OBSERVATIONS == REVIEW_EVENT_DELTA)) \
    || die "review_observations delta mismatch"

  ((review_row_delta >= 0)) || die "reviews row count regressed unexpectedly"
  ((review_row_delta <= REVIEW_EVENT_DELTA)) \
    || die "reviews current-state delta exceeds ReviewObserved event delta"

  printf '    DB delta: ingested=%s snapshots=%s current_reviews=%s observations=%s\n' \
    "$((final_ingested - BASE_INGESTED))" \
    "$((final_snapshots - BASE_SNAPSHOTS))" \
    "$review_row_delta" \
    "$((final_observations - BASE_OBSERVATIONS))"

  ok "Kafka -> ingestion -> PostgreSQL"
}

verify_run_specific_database_rows() {
  log "Verify DB rows belong to this exact crawl run"

  local run_snapshots run_observations

  run_snapshots="$(psql_scalar "
SELECT count(*)
FROM playstore_app_snapshots s
JOIN crawl_tasks ct ON ct.id = s.crawl_task_id
WHERE ct.crawl_run_id = '${RUN_ID}'::uuid;
")"

  run_observations="$(psql_scalar "
SELECT count(*)
FROM review_observations ro
JOIN crawl_tasks ct ON ct.id = ro.crawl_task_id
WHERE ct.crawl_run_id = '${RUN_ID}'::uuid;
")"

  ((run_snapshots == APP_EVENT_DELTA)) \
    || die "This crawl run has $run_snapshots snapshots, expected $APP_EVENT_DELTA"

  ((run_observations == REVIEW_EVENT_DELTA)) \
    || die "This crawl run has $run_observations observations, expected $REVIEW_EVENT_DELTA"

  ok "run-specific snapshots=$run_snapshots observations=$run_observations"
}

verify_observability() {
  ((SKIP_OBSERVABILITY)) && return 0

  log "Verify Alloy -> Loki -> Grafana smoke path"

  wait_for "ingestion logs visible in Loki" "$TIMEOUT_SECONDS" \
    loki_has_service_logs "sahabino-ingestion"

  wait_for "API logs visible in Loki" "$TIMEOUT_SECONDS" \
    loki_has_service_logs "sahabino-api"

  if ! loki_has_service_logs "sahabino-crawler"; then
    warn "Crawler one-shot logs were not discovered before the temporary container exited"
    printf '    Emitting a dedicated crawler structured-log smoke record and keeping the container alive briefly...\n'

    docker compose run --rm crawler \
      uv run --no-sync python -c \
      "import logging,time; from sahabino.common.observability.logging import configure_logging; configure_logging(service_name='sahabino-crawler', level='INFO', log_format='json', environment='development'); logging.getLogger('sahabino.crawler.smoke').info('crawler logging smoke', extra={'event':'crawler.smoke','crawl_run_id':'${RUN_ID}'}); time.sleep(${CRAWLER_LOG_DISCOVERY_SECONDS})"
  fi

  wait_for "crawler logs visible in Loki" "$TIMEOUT_SECONDS" \
    loki_has_service_logs "sahabino-crawler"

  local services
  services="$(curl -fsS --max-time 5 'http://localhost:3100/loki/api/v1/label/service/values')"

  printf '%s' "$services" | grep -q 'sahabino-ingestion' \
    || die "Loki service labels do not contain sahabino-ingestion"
  printf '%s' "$services" | grep -q 'sahabino-api' \
    || die "Loki service labels do not contain sahabino-api"
  printf '%s' "$services" | grep -q 'sahabino-crawler' \
    || die "Loki service labels do not contain sahabino-crawler"

  [[ -f infrastructure/observability/grafana/dashboards/sahabino-logging-smoke.json ]] \
    || die "Grafana smoke dashboard unavailable"

  ok "structured logs visible in Loki for API, crawler and ingestion"
  ok "Grafana smoke dashboard provisioning present"
}

print_summary() {
  log "Smoke test summary"

  cat <<EOF
    crawl_run_id:            $RUN_ID
    active applications:     $ACTIVE_APPS

    Kafka new app events:     $APP_EVENT_DELTA
    Kafka new review events:  $REVIEW_EVENT_DELTA
    Kafka final group lag:    $(group_lag)

    DB new ingested_events:   $TOTAL_EVENT_DELTA
    DB new snapshots:         $APP_EVENT_DELTA
    DB new observations:      $REVIEW_EVENT_DELTA

    API:                      PASS
    PostgreSQL/Alembic:       PASS
    Kafka topics:             PASS
    Crawler lifecycle:        PASS
    Crawler -> Kafka:         PASS
    Kafka -> Ingestion:       PASS
    Kafka offset commits:     PASS
    Ingestion persistence:    PASS
EOF

  if ((SKIP_OBSERVABILITY == 0)); then
    cat <<'EOF'
    Alloy -> Loki:            PASS
    Grafana provisioning:     PASS
EOF
  else
    cat <<'EOF'
    Observability:            SKIPPED
EOF
  fi

  printf '\nFULL SAHABINO SMOKE TEST: PASS\n'
}

maybe_restore_scheduler() {
  if ((CRAWLER_STOPPED_BY_US)); then
    docker compose start crawler >/dev/null
    CRAWLER_STOPPED_BY_US=0
    ok "crawler scheduler restored"
  fi
}

maybe_down() {
  if ((DOWN_AFTER)); then
    log "Stop runtime containers (--down-after)"
    RESTORE_ON_EXIT=0
    docker compose --profile observability down
    ok "containers stopped; named volumes preserved"
  fi
}

main() {
  preflight
  build_images_if_requested

  if service_running crawler; then
    CRAWLER_WAS_RUNNING=1
  fi

  stop_scheduler_if_needed
  start_foundation
  migrate_database
  ensure_topics
  start_runtime_services
  ensure_registry_apps
  drain_old_backlog
  stop_ingestion_and_capture_baseline
  run_controlled_crawl
  verify_crawl_lifecycle_and_kafka
  start_ingestion_and_verify_persistence
  verify_run_specific_database_rows
  verify_observability
  maybe_restore_scheduler
  print_summary
  maybe_down
}

main "$@"
