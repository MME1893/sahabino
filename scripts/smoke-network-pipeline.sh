#!/usr/bin/env bash
set -Eeuo pipefail

TIMEOUT_SECONDS="${SMOKE_NETWORK_TIMEOUT_SECONDS:-180}"
PACKAGE_NAME="${SMOKE_NETWORK_PACKAGE_NAME:-com.example.sahabino.networksmoke}"
BUILD=0
DOWN_AFTER=0
TMP_DIR=""

for argument in "$@"; do
  case "$argument" in
    --build) BUILD=1 ;;
    --down-after) DOWN_AFTER=1 ;;
    *) printf 'Unknown argument: %s\n' "$argument" >&2; exit 2 ;;
  esac
done

log() { printf '\n==> %s\n' "$1"; }
ok() { printf '    PASS: %s\n' "$1"; }
die() { printf '    FAIL: %s\n' "$1" >&2; exit 1; }

cleanup() {
  [[ -z "$TMP_DIR" ]] || rm -rf -- "$TMP_DIR"
  if ((DOWN_AFTER)); then
    docker compose --profile network down >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_for() {
  local description="$1" timeout="$2"
  shift 2
  local deadline=$((SECONDS + timeout))
  until "$@"; do
    ((SECONDS < deadline)) || die "Timed out waiting for $description"
    sleep 2
  done
  ok "$description"
}

json_field() {
  local field="$1"
  python -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$field"
}

psql_scalar() {
  local sql="$1"
  docker compose exec -T postgres sh -lc \
    'psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    <<<"$sql" | tr -d '\r'
}

http_ready() { curl -fsS --max-time 5 http://localhost:8000/docs >/dev/null; }

capture_analyzed() {
  local response status
  response="$(curl -fsS --max-time 5 "http://localhost:8000/network-captures/$CAPTURE_ID")" || return 1
  status="$(printf '%s' "$response" | json_field status)"
  [[ "$status" == "analyzed" ]]
}

analysis_ingested() {
  [[ "$(psql_scalar "SELECT count(*) FROM network_analysis_results WHERE capture_id = '$CAPTURE_ID'::uuid;")" == "1" ]] \
    && [[ "$(psql_scalar "SELECT count(*) FROM ingested_events WHERE event_id = (SELECT analysis_event_id FROM network_captures WHERE id = '$CAPTURE_ID'::uuid);")" == "1" ]]
}

group_lag() {
  local group="$1" topic="$2"
  docker compose exec -T kafka sh -lc \
    "/opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:19092 --describe --group '$group'" \
    2>/dev/null | tr -d '\r' | awk -v topic="$topic" '
      $2 == topic && $6 ~ /^[0-9]+$/ { found=1; lag += $6 }
      END { if (found) print lag + 0; else print -1 }
    '
}

lags_zero() {
  [[ "$(group_lag sahabino-network-analyzer-v1 network.capture-ready.v1)" == "0" ]] \
    && [[ "$(group_lag sahabino-ingestion-v1 network.analysis-collected.v1)" == "0" ]]
}

log "Preflight and Compose validation"
for command in docker curl python awk tr; do
  command -v "$command" >/dev/null || die "Required command is missing: $command"
done
docker info >/dev/null 2>&1 || die "Docker daemon is not available"
docker compose config >/dev/null
docker compose --profile network config >/dev/null
docker compose --profile observability --profile network config >/dev/null
ok "Compose configurations"

if ((BUILD)); then
  log "Build application and analyzer images"
  docker compose --profile network build api ingestion network-analyzer
  ok "images built"
fi

log "Start infrastructure and initialize schema/topics/storage"
docker compose --profile network up -d postgres kafka seaweedfs
wait_for "PostgreSQL healthy" "$TIMEOUT_SECONDS" docker compose exec -T postgres sh -lc \
  'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null'
wait_for "Kafka ready" "$TIMEOUT_SECONDS" docker compose exec -T kafka sh -lc \
  '/opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:19092 >/dev/null 2>&1'
wait_for "SeaweedFS ready" "$TIMEOUT_SECONDS" docker compose exec -T seaweedfs \
  wget -q --spider http://127.0.0.1:9333/cluster/status
docker compose run --rm --no-deps api uv run --no-sync alembic upgrade head
docker compose run --rm --no-deps api uv run --no-sync python -m sahabino.messaging.admin
docker compose --profile network run --rm network-analyzer \
  uv run --no-sync python -m sahabino.network storage-init
ok "migration, topics, and bucket"

log "Start API, ingestion, and analyzer"
docker compose --profile network up -d api ingestion network-analyzer
wait_for "API ready" "$TIMEOUT_SECONDS" http_ready

log "Ensure smoke application"
APPLICATIONS="$(curl -fsS 'http://localhost:8000/applications')"
APPLICATION_ID="$(printf '%s' "$APPLICATIONS" | python -c \
  'import json,sys; p=sys.argv[1]; print(next((x["id"] for x in json.load(sys.stdin) if x["package_name"] == p), ""))' \
  "$PACKAGE_NAME")"
if [[ -z "$APPLICATION_ID" ]]; then
  APPLICATION="$(curl -fsS -X POST 'http://localhost:8000/applications' \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"Network Smoke\",\"package_name\":\"$PACKAGE_NAME\",\"category_codes\":[\"messaging\"],\"primary_category_code\":\"messaging\"}")"
  APPLICATION_ID="$(printf '%s' "$APPLICATION" | json_field id)"
fi
ok "application_id=$APPLICATION_ID"

log "Generate deterministic PCAPNG and register metadata"
TMP_DIR="$(mktemp -d)"
FIXTURE="$TMP_DIR/synthetic.pcapng"
python scripts/generate_network_fixture.py "$FIXTURE" >/dev/null
CAPTURE_SIZE="$(python -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$FIXTURE")"
CAPTURE_SHA="$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$FIXTURE")"
IDEMPOTENCY_KEY="$(python -c 'import uuid; print(uuid.uuid4())')"
CREATE_BODY="{\"application_id\":\"$APPLICATION_ID\",\"scenario\":\"upload\",\"filename\":\"synthetic.pcapng\",\"capture_size_bytes\":$CAPTURE_SIZE,\"transfer_file_size_bytes\":20,\"sha256\":\"$CAPTURE_SHA\"}"
CREATE_RESPONSE="$(curl -fsS -X POST 'http://localhost:8000/network-captures' \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $IDEMPOTENCY_KEY" -d "$CREATE_BODY")"
CAPTURE_ID="$(printf '%s' "$CREATE_RESPONSE" | json_field capture_id)"
ANALYSIS_ID="$(printf '%s' "$CREATE_RESPONSE" | json_field analysis_id)"
UPLOAD_REQUIRED="$(printf '%s' "$CREATE_RESPONSE" | json_field upload_required)"
UPLOAD_URL="$(printf '%s' "$CREATE_RESPONSE" | json_field upload_url)"
[[ "$UPLOAD_REQUIRED" == "True" ]] || die "fresh capture unexpectedly skipped upload"
[[ -n "$UPLOAD_URL" && "$UPLOAD_URL" != "None" ]] || die "upload URL missing"
ok "capture_id=$CAPTURE_ID analysis_id=$ANALYSIS_ID"

log "Direct upload and idempotent completion"
curl -fsS -X PUT -H 'Content-Type: application/x-pcapng' --upload-file "$FIXTURE" "$UPLOAD_URL" >/dev/null
unset UPLOAD_URL
curl -fsS -X POST "http://localhost:8000/network-captures/$CAPTURE_ID/complete" >/dev/null
curl -fsS -X POST "http://localhost:8000/network-captures/$CAPTURE_ID/complete" >/dev/null
ok "upload and repeated /complete"

log "Wait for analysis, ingestion, and committed offsets"
wait_for "capture analyzed" "$TIMEOUT_SECONDS" capture_analyzed
wait_for "one analytical row and one ingested event" "$TIMEOUT_SECONDS" analysis_ingested
wait_for "network Kafka consumer lag zero" "$TIMEOUT_SECONDS" lags_zero

log "Verify end-to-end idempotency and physical-key reuse"
REPEATED="$(curl -fsS -X POST 'http://localhost:8000/network-captures' \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $IDEMPOTENCY_KEY" -d "$CREATE_BODY")"
[[ "$(printf '%s' "$REPEATED" | json_field capture_id)" == "$CAPTURE_ID" ]] \
  || die "repeated create returned another capture"
curl -fsS -X POST "http://localhost:8000/network-captures/$CAPTURE_ID/complete" >/dev/null
[[ "$(psql_scalar "SELECT count(*) FROM network_captures WHERE application_id='$APPLICATION_ID'::uuid AND scenario='upload' AND expected_sha256='$CAPTURE_SHA';")" == "1" ]] \
  || die "logical capture was duplicated"
[[ "$(psql_scalar "SELECT count(*) FROM network_analysis_results WHERE capture_id='$CAPTURE_ID'::uuid;")" == "1" ]] \
  || die "analysis result was duplicated"
OBJECT_KEY="$(psql_scalar "SELECT object_key FROM network_captures WHERE id='$CAPTURE_ID'::uuid;")"
[[ "$(psql_scalar "SELECT count(DISTINCT object_key) FROM network_captures WHERE expected_sha256='$CAPTURE_SHA';")" == "1" ]] \
  || die "content-addressed object key was duplicated"
OBJECT_COUNT="$(docker compose exec -T api uv run --no-sync python -c \
  "from sahabino.common.config import get_settings; from sahabino.network.storage import S3ObjectStorage; s=S3ObjectStorage(get_settings()); print(len(s._client.list_objects_v2(Bucket=s._bucket, Prefix='$OBJECT_KEY').get('Contents', [])))" \
  | tr -d '\r')"
[[ "$OBJECT_COUNT" == "1" ]] || die "expected exactly one physical S3 object, found $OBJECT_COUNT"
ok "one capture, result, event identity, and content key"

STATUS="$(psql_scalar "SELECT status FROM network_captures WHERE id='$CAPTURE_ID'::uuid;")"
ATTEMPTS="$(psql_scalar "SELECT analysis_attempt_count FROM network_captures WHERE id='$CAPTURE_ID'::uuid;")"
COMPARISON_READY="$(psql_scalar "SELECT comparison_ready FROM network_analysis_results WHERE capture_id='$CAPTURE_ID'::uuid;")"
EFFECTIVE="$(psql_scalar "SELECT effective_file_throughput_mbps FROM network_analysis_results WHERE capture_id='$CAPTURE_ID'::uuid;")"
AMPLIFICATION="$(psql_scalar "SELECT total_transfer_amplification_ratio FROM network_analysis_results WHERE capture_id='$CAPTURE_ID'::uuid;")"
RECOVERY_TAX="$(psql_scalar "SELECT coalesce(tcp_recovery_tax::text, 'NULL') FROM network_analysis_results WHERE capture_id='$CAPTURE_ID'::uuid;")"
ANALYZER_LAG="$(group_lag sahabino-network-analyzer-v1 network.capture-ready.v1)"
INGESTION_LAG="$(group_lag sahabino-ingestion-v1 network.analysis-collected.v1)"
TSHARK_VERSION="$(docker compose exec -T network-analyzer tshark --version | tr -d '\r' | awk 'NR==1')"

cat <<EOF

capture_id:                    $CAPTURE_ID
analysis_id:                   $ANALYSIS_ID
application_id:                $APPLICATION_ID
scenario:                      upload
capture status:                $STATUS
analysis attempt count:        $ATTEMPTS
object key:                    $OBJECT_KEY
verified SHA-256:              $CAPTURE_SHA
analyzer Kafka lag:            $ANALYZER_LAG
ingestion Kafka lag:           $INGESTION_LAG
comparison_ready:              $COMPARISON_READY
effective throughput Mbit/s:   $EFFECTIVE
total transfer amplification:  $AMPLIFICATION
TCP recovery tax:              $RECOVERY_TAX
TShark:                        $TSHARK_VERSION
DB result count:               1
physical object count:         $OBJECT_COUNT

NETWORK PIPELINE SMOKE TEST: PASS
EOF
