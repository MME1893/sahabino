#!/bin/sh
set -eu

input=${SAHABINO_SEAWEEDFS_CONFIG_INPUT:-/run/secrets/seaweedfs-s3.json}
runtime_dir=${SAHABINO_SEAWEEDFS_RUNTIME_DIR:-/run/sahabino-seaweedfs}
staged_config=$runtime_dir/seaweedfs-s3.json
upstream_entrypoint=${SAHABINO_SEAWEEDFS_UPSTREAM_ENTRYPOINT:-/entrypoint.sh}

fail() {
  printf 'SeaweedFS credential staging failed: %s\n' "$1" >&2
  exit 1
}

# The exact same runtime audit is called by Ansible before the maintenance
# window, by Ansible after the release, and by the interactive deployment
# assistant.  Keep shell quoting local to this file: embedding awk and `$2` in
# nested YAML/Jinja/shell strings previously stopped production after migration.
verify_runtime() {
  [ "$(id -u)" = "0" ] || fail "runtime verification needs root"
  seaweed_uid=$(id -u seaweed 2>/dev/null) || fail "seaweed account is missing"
  seaweed_gid=$(id -g seaweed 2>/dev/null) || fail "seaweed group is missing"
  pid1_uid=$(awk '/^Uid:/ {print $2; exit}' /proc/1/status)
  pid1_gid=$(awk '/^Gid:/ {print $2; exit}' /proc/1/status)
  [ -n "$pid1_uid" ] && [ -n "$pid1_gid" ] \
    || fail "cannot determine PID 1 identity"
  [ "$pid1_uid" -ne 0 ] || fail "SeaweedFS PID 1 must not run as root"
  [ "$pid1_uid" = "$seaweed_uid" ] || fail "PID 1 UID does not match seaweed"
  [ "$pid1_gid" = "$seaweed_gid" ] || fail "PID 1 GID does not match seaweed"
  [ -d /data ] && [ ! -L /data ] || fail "SeaweedFS /data is missing or unsafe"
  [ "$(stat -c %u /data)" = "$seaweed_uid" ] \
    && [ "$(stat -c %g /data)" = "$seaweed_gid" ] \
    && su-exec seaweed test -w /data \
    || fail "SeaweedFS /data ownership or writability is invalid"
  [ -f "$staged_config" ] && [ ! -L "$staged_config" ] \
    && [ "$(stat -c %u "$staged_config")" = 0 ] \
    && [ "$(stat -c %g "$staged_config")" = "$seaweed_gid" ] \
    && [ "$(stat -c %a "$staged_config")" = 640 ] \
    && su-exec seaweed test -r "$staged_config" \
    && ! su-exec seaweed test -w "$staged_config" \
    || fail "SeaweedFS staged credential permissions are invalid"
  printf 'SEAWEEDFS_RUNTIME_VERIFIED\n'
}

if [ "${1:-}" = "--verify-runtime" ]; then
  [ "$#" -eq 1 ] || fail "--verify-runtime does not take arguments"
  verify_runtime
  exit 0
fi

[ "$(id -u)" = "0" ] || fail "wrapper must start as root"
[ -f "$input" ] && [ ! -L "$input" ] || fail "input is not a regular file"
[ -x "$upstream_entrypoint" ] || fail "upstream entrypoint is unavailable"

seaweed_uid=$(id -u seaweed 2>/dev/null) || fail "seaweed user is unavailable"
seaweed_gid=$(id -g seaweed 2>/dev/null) || fail "seaweed group is unavailable"
[ -n "$seaweed_uid" ] && [ -n "$seaweed_gid" ] || fail "seaweed account is invalid"

if [ -e "$runtime_dir" ] || [ -L "$runtime_dir" ]; then
  [ -d "$runtime_dir" ] && [ ! -L "$runtime_dir" ] || fail "runtime path is unsafe"
else
  mkdir -m 0700 "$runtime_dir"
fi
chown "0:$seaweed_gid" "$runtime_dir"
chmod 0750 "$runtime_dir"

candidate=$(mktemp "$runtime_dir/.seaweedfs-s3.XXXXXX") \
  || fail "could not create a secure candidate"
cleanup_candidate() {
  rm -f -- "$candidate"
}
trap cleanup_candidate EXIT HUP INT TERM

cp -- "$input" "$candidate" || fail "could not stage the input"
chown "0:$seaweed_gid" "$candidate"
chmod 0640 "$candidate"
su-exec seaweed test -r "$candidate" || fail "seaweed cannot read the staged input"
if su-exec seaweed test -w "$candidate"; then
  fail "seaweed can write the staged input"
fi

mv -f -- "$candidate" "$staged_config"
trap - EXIT HUP INT TERM

# The pinned image's /entrypoint.sh starts as root, repairs /data ownership when
# necessary, and re-enters itself through su-exec before launching weed.
exec "$upstream_entrypoint" "$@" "-s3.config=$staged_config"
