#!/usr/bin/env bash
# Sahabino production deployment assistant.
#
# A hardened, interactive wrapper around the existing Ansible playbooks.
# It bootstraps/validates the host, prepares a local Ansible controller,
# guides the operator through GitHub Deploy Key and Vault setup, executes
# provision/deploy, and performs post-deploy verification.
#
# Safety properties:
# - never resets/cleans a dirty production checkout;
# - never silently removes Docker state;
# - never edits firewall/VPN/X-Ray configuration;
# - never prints generated service secrets by default;
# - never trusts an SSH host key via accept-new;
# - keeps generated inventory and the Ansible venv outside the Git checkout.

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

# Preserve exactly what the operator supplied so root re-run guidance does not
# lose arguments or shell quoting.
ORIGINAL_ARGS=("$@")

ASSISTANT_VERSION="2026.09.14.3"
PROGRAM_NAME="$(basename "$0")"
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd -P)"
INVOCATION_DIR="${SAHABINO_INVOCATION_DIR:-$PWD}"
INVOKING_USER="${SUDO_USER:-$(id -un)}"

# ---------------------------------------------------------------------------
# Central configuration. Project-specific values are refreshed from
# deploy/ansible/group_vars/production/vars.yml after the checkout is known.
# ---------------------------------------------------------------------------
DEPLOY_USER="${SAHABINO_DEPLOY_USER:-sahabino}"
DEPLOY_GROUP="${SAHABINO_DEPLOY_GROUP:-sahabino}"
APP_PARENT="${SAHABINO_APP_PARENT:-/opt/sahabino}"
APP_DIR="${SAHABINO_APP_DIR:-/opt/sahabino/app}"
REPOSITORY_URL="${SAHABINO_REPOSITORY_URL:-}"
GITHUB_ALIAS="${SAHABINO_GITHUB_ALIAS:-github-sahabino}"
GITHUB_HOST="${SAHABINO_GITHUB_HOST:-github.com}"
DEPLOY_KEY_BASENAME="${SAHABINO_DEPLOY_KEY_BASENAME:-sahabino_github}"

ANSIBLE_VENV="${SAHABINO_ANSIBLE_VENV:-$APP_PARENT/ansible-venv}"
ANSIBLE_COLLECTIONS_DIR="${SAHABINO_ANSIBLE_COLLECTIONS_DIR:-$APP_PARENT/ansible-collections}"
RUNTIME_DIR="${SAHABINO_RUNTIME_DIR:-$APP_PARENT/runtime}"
INVENTORY_FILE="${SAHABINO_INVENTORY_FILE:-$RUNTIME_DIR/production.inventory.yml}"

COMPOSE_PROJECT_NAME="${SAHABINO_COMPOSE_PROJECT_NAME:-sahabino}"
COMPOSE_PROFILE="${SAHABINO_COMPOSE_PROFILE:-observability}"
API_BIND_ADDRESS="${SAHABINO_API_BIND_ADDRESS:-0.0.0.0}"
API_PORT="${SAHABINO_API_PORT:-8000}"
GRAFANA_PORT="${SAHABINO_GRAFANA_PORT:-3000}"
LOKI_PORT="${SAHABINO_LOKI_PORT:-3100}"
ALLOY_PORT="${SAHABINO_ALLOY_PORT:-12345}"
OBJECT_STORAGE_PORT="${SAHABINO_OBJECT_STORAGE_PORT:-8333}"
INGESTION_CONSUMER_GROUP="${SAHABINO_INGESTION_CONSUMER_GROUP:-sahabino-ingestion-v1}"
KAFKA_BOOTSTRAP_INTERNAL="${SAHABINO_KAFKA_BOOTSTRAP_INTERNAL:-kafka:19092}"

MIN_COMPOSE_VERSION="2.24.4"
DEFAULT_SWAP_GIB="2"
SUPPORTED_UBUNTU_RELEASES=("24.04")
LOG_ROOT="${SAHABINO_DEPLOY_LOG_DIR:-/var/log/sahabino-deploy-assistant}"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_LOG=""
CURRENT_STAGE="startup"
VAULT_PASSWORD_FILE=""
VAULT_PLAINTEXT_TEMP=""
VAULT_PASSWORD_STORE="${SAHABINO_VAULT_PASSWORD_STORE:-/etc/sahabino/ansible-vault-password}"
ERROR_REPORTED=0
TARGET_REVISION="${SAHABINO_DEPLOY_REVISION:-}"
MODE="full"
ASSUME_YES=0
NO_REBOOT=0
SKIP_DOCKER_MIGRATION=0
ALLOW_UNSUPPORTED_OS="${SAHABINO_ALLOW_UNSUPPORTED_OS:-0}"
CONFIRM_PUBLIC_API="${SAHABINO_CONFIRM_PUBLIC_API:-0}"
REPOSITORY_URL_EXPLICIT=0
[[ -n "${SAHABINO_REPOSITORY_URL:-}" ]] && REPOSITORY_URL_EXPLICIT=1

# Optional dependency-download SOCKS proxy. This is deliberately scoped to
# package/bootstrap traffic (pip + Ansible Galaxy); it does not proxy Git,
# Docker daemon traffic, the application, or the host globally.
SOCKS_PROXY="${SAHABINO_SOCKS_PROXY:-}"
DEFAULT_SOCKS_PROXY="${SAHABINO_DEFAULT_SOCKS_PROXY:-127.0.0.1:8080}"
SOCKS_PROXY_DISABLED=0
DEPENDENCY_PROXY_ENABLED=0
PROXYCHAINS_CONFIG=""
PYPI_INDEX_URL="${SAHABINO_PYPI_INDEX_URL:-https://pypi.org/simple}"
PIP_TIMEOUT="${SAHABINO_PIP_TIMEOUT:-120}"
PIP_RETRIES="${SAHABINO_PIP_RETRIES:-10}"
WORKTREE_UMASK="${SAHABINO_WORKTREE_UMASK:-022}"

# Official GitHub.com host keys/fingerprints, current at script revision time.
# Source: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
GITHUB_ED25519_KEY='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
GITHUB_ECDSA_KEY='github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg='
GITHUB_RSA_KEY='github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk='
GITHUB_ALLOWED_FINGERPRINTS=(
  'SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s'
  'SHA256:p2QAMXNIC1TJYWeIOttrVc98/R1BUFWu3/LiyKgUfQM'
  'SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU'
)

# Terminal colors are disabled automatically when stdout is not a TTY.
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_BLUE=$'\033[34m'
else
  C_RESET=""; C_BOLD=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""
fi

append_log() {
  # Assistant status messages are always persisted once RUN_LOG exists.
  # We deliberately do not globally tee the terminal because a one-time Vault
  # password reveal must never be copied into the run log.
  [[ -n "${RUN_LOG:-}" ]] || return 0
  printf '%s\n' "$1" >> "$RUN_LOG" 2>/dev/null || true
}

info()  { printf '%s[INFO]%s %s\n' "$C_BLUE" "$C_RESET" "$*"; append_log "[INFO] $*"; }
ok()    { printf '%s[ OK ]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; append_log "[ OK ] $*"; }
warn()  { printf '%s[WARN]%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; append_log "[WARN] $*"; }
error() { printf '%s[FAIL]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; append_log "[FAIL] $*"; }
step()  { CURRENT_STAGE="$*"; printf '\n%s==> %s%s\n' "$C_BOLD" "$*" "$C_RESET"; append_log "==> $*"; }

usage() {
  cat <<EOF_USAGE
Usage: $PROGRAM_NAME [options]

Interactive, rerun-safe production deployment assistant for Sahabino.
Run it on the Ubuntu production VPS. Root is recommended because first-run
bootstrap may need package installation, Docker migration, user creation, swap,
and systemd changes.

Modes:
  --full                    Bootstrap + provision + deploy + verify (default)
  --check                   Run host/repository/Vault preflight only
  --provision               Bootstrap and run provision.yml only
  --deploy                  Bootstrap as needed, then deploy + verify
  --verify                  Only verify the currently running deployment

Options:
  --revision REV             Branch/tag/SHA to deploy; resolved to a full SHA.
  --repository-url URL       Read-only production Git URL. Required for a
                             standalone first run when it cannot be discovered
                             from project vars.
  --yes                      Accept safe/default choices where possible.
  --confirm-public-api       Explicitly acknowledge public API exposure when
                             production bind address is 0.0.0.0/::.
  --allow-unsupported-os     Compatibility no-op; retained for older invocations.
  --no-reboot                Acknowledge a pending reboot warning. The assistant
                             never reboots the host automatically.
  --skip-docker-migration    Refuse automatic Snap Docker migration.
  --socks-proxy HOST:PORT    Route dependency downloads (pip/Ansible Galaxy)
                             through this SOCKS5 proxy only.
  --no-socks-proxy           Disable automatic local SOCKS fallback detection.
  -h, --help                 Show this help.

Environment overrides include:
  SAHABINO_REPOSITORY_URL, SAHABINO_DEPLOY_REVISION,
  SAHABINO_VAULT_PASSWORD, SAHABINO_VAULT_PASSWORD_FILE,
  SAHABINO_CONFIRM_PUBLIC_API, SAHABINO_SOCKS_PROXY,
  SAHABINO_DEFAULT_SOCKS_PROXY, SAHABINO_PYPI_INDEX_URL,
  SAHABINO_PIP_TIMEOUT, SAHABINO_PIP_RETRIES,
  SAHABINO_VAULT_PASSWORD_STORE.

The assistant never runs git reset --hard, git clean, or silently overwrites a
server-side dirty checkout.
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) MODE="full"; shift ;;
    --check) MODE="check"; shift ;;
    --provision) MODE="provision"; shift ;;
    --deploy) MODE="deploy"; shift ;;
    --verify) MODE="verify"; shift ;;
    --revision)
      [[ $# -ge 2 ]] || { error "--revision requires a value"; exit 2; }
      TARGET_REVISION="$2"; shift 2 ;;
    --repository-url)
      [[ $# -ge 2 ]] || { error "--repository-url requires a value"; exit 2; }
      REPOSITORY_URL="$2"; REPOSITORY_URL_EXPLICIT=1; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --confirm-public-api) CONFIRM_PUBLIC_API=1; shift ;;
    --allow-unsupported-os) ALLOW_UNSUPPORTED_OS=1; shift ;;  # backward-compatible; Ubuntu version no longer hard-blocks
    --no-reboot) NO_REBOOT=1; shift ;;
    --skip-docker-migration) SKIP_DOCKER_MIGRATION=1; shift ;;
    --socks-proxy)
      [[ $# -ge 2 ]] || { error "--socks-proxy requires HOST:PORT"; exit 2; }
      SOCKS_PROXY="$2"; SOCKS_PROXY_DISABLED=0; shift 2 ;;
    --no-socks-proxy) SOCKS_PROXY=""; SOCKS_PROXY_DISABLED=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) error "Unknown option: $1"; usage; exit 2 ;;
  esac
done

cleanup() {
  if [[ -n "$VAULT_PASSWORD_FILE" ]] && [[ "$VAULT_PASSWORD_FILE" == /tmp/sahabino-vault-pass.* ]]; then
    rm -f "$VAULT_PASSWORD_FILE" || true
  fi
  if [[ -n "$VAULT_PLAINTEXT_TEMP" ]] && [[ "$VAULT_PLAINTEXT_TEMP" == /tmp/sahabino-vault-plain.* ]]; then
    rm -f "$VAULT_PLAINTEXT_TEMP" || true
  fi
  if [[ -n "${PROXYCHAINS_CONFIG:-}" ]] && [[ "$PROXYCHAINS_CONFIG" == /tmp/sahabino-proxychains.* ]]; then
    rm -f "$PROXYCHAINS_CONFIG" || true
  fi
}
trap cleanup EXIT

on_error() {
  local rc=$? line=${BASH_LINENO[0]:-unknown} cmd=${BASH_COMMAND:-unknown}

  # With errtrace enabled, a failing command inside a subshell can trigger ERR
  # both in the subshell and again in its caller. Report only at the top level
  # so one failure never produces two misleading [FAIL] blocks.
  if (( BASH_SUBSHELL > 0 )); then
    return "$rc"
  fi
  (( ERROR_REPORTED == 0 )) || exit "$rc"
  ERROR_REPORTED=1
  trap - ERR

  error "Deployment assistant stopped during '$CURRENT_STAGE' at line $line (exit $rc)."
  append_log "[FAIL] Stage: $CURRENT_STAGE"
  append_log "[FAIL] Command: $cmd"
  [[ -n "$RUN_LOG" && -f "$RUN_LOG" ]] && warn "Log: $RUN_LOG"
  exit "$rc"
}
trap on_error ERR

have() { command -v "$1" >/dev/null 2>&1; }

version_ge() {
  [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]
}

array_contains() {
  local needle="$1" item; shift
  for item in "$@"; do [[ "$item" == "$needle" ]] && return 0; done
  return 1
}

ask_yes_no() {
  local prompt="$1" default="${2:-yes}" answer
  if (( ASSUME_YES )); then
    [[ "$default" == "no" ]] && return 1 || return 0
  fi
  while true; do
    if [[ "$default" == "yes" ]]; then
      read -r -p "$prompt [Y/n] " answer || return 1; answer="${answer:-y}"
    else
      read -r -p "$prompt [y/N] " answer || return 1; answer="${answer:-n}"
    fi
    case "${answer,,}" in y|yes) return 0 ;; n|no) return 1 ;; *) warn "Please answer y or n." ;; esac
  done
}

prompt_default() {
  local prompt="$1" default="$2" value
  if (( ASSUME_YES )); then printf '%s' "$default"; return 0; fi
  read -r -p "$prompt [$default]: " value
  printf '%s' "${value:-$default}"
}

print_rerun_command() {
  printf 'sudo -E bash %q' "$SCRIPT_PATH" >&2
  local arg
  for arg in "${ORIGINAL_ARGS[@]}"; do printf ' %q' "$arg" >&2; done
  printf '\n' >&2
}

require_root_for_bootstrap() {
  [[ $EUID -eq 0 || "$MODE" == "verify" ]] && return 0
  error "First-run/bootstrap mode must run as root on the production VPS."
  if have sudo; then
    warn "Re-run with:"
    print_rerun_command
  else
    warn "Switch to root (for example: su -) and re-run this script."
  fi
  exit 1
}

setup_logging() {
  if [[ $EUID -eq 0 ]]; then
    install -d -m 0750 "$LOG_ROOT"
    RUN_LOG="$LOG_ROOT/run-$RUN_ID.log"
  else
    RUN_LOG="/tmp/sahabino-deploy-$RUN_ID.log"
  fi
  touch "$RUN_LOG"; chmod 0600 "$RUN_LOG"
  printf '[INFO] Run started: %s\n' "$RUN_ID" >> "$RUN_LOG"
  info "Run log: $RUN_LOG"
  info "Deployment assistant version: $ASSISTANT_VERSION"
  info "Script path: $SCRIPT_PATH"

  # A deployment script should never be group/world writable. Correct only the
  # write bits; this does not change Git's executable-bit semantics.
  local script_mode
  script_mode=$(stat -c '%a' "$SCRIPT_PATH" 2>/dev/null || true)
  if [[ -n "$script_mode" ]] && (( (8#$script_mode & 022) != 0 )); then
    warn "Deployment script is group/world-writable (mode $script_mode); removing unsafe write bits."
    chmod go-w "$SCRIPT_PATH" || true
  fi
}

run_logged() {
  # Do not use for commands that may print secrets.
  local rc
  set +e
  "$@" 2>&1 | tee -a "$RUN_LOG"
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

deploy_home() { getent passwd "$DEPLOY_USER" | cut -d: -f6; }

deploy_path() { printf '%s' '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'; }

run_as_deploy_in() {
  # Execute with a deterministic deploy-user environment and an explicit cwd.
  # This prevents runuser from inheriting an inaccessible caller cwd such as
  # /home/<operator>/private-dir.
  local cwd="$1"; shift
  local home; home=$(deploy_home)
  [[ -n "$home" ]] || { error "Cannot resolve HOME for '$DEPLOY_USER'."; return 1; }

  local -a env_args=(
    env -i
    "HOME=$home"
    "USER=$DEPLOY_USER"
    "LOGNAME=$DEPLOY_USER"
    "PATH=$(deploy_path)"
    "LANG=C.UTF-8"
    "LC_ALL=C.UTF-8"
  )
  local -a shell_args=(bash --noprofile --norc -c 'cd -- "$1" && shift && exec "$@"' bash "$cwd" "$@")

  if [[ "$(id -un)" == "$DEPLOY_USER" ]]; then
    "${env_args[@]}" "${shell_args[@]}"
  elif [[ $EUID -eq 0 ]]; then
    runuser -u "$DEPLOY_USER" -- "${env_args[@]}" "${shell_args[@]}"
  else
    error "Need root or user '$DEPLOY_USER' to execute deploy-user commands."
    return 1
  fi
}

run_as_deploy() {
  local home; home=$(deploy_home)
  run_as_deploy_in "$home" "$@"
}

run_as_deploy_in_umask() {
  # Use a non-secret umask for Git/worktree-producing commands while keeping
  # the wrapper default at 077 for credentials, Vault material, and logs.
  local cwd="$1" mask="$2"; shift 2
  local home; home=$(deploy_home)
  [[ -n "$home" ]] || { error "Cannot resolve HOME for '$DEPLOY_USER'."; return 1; }

  local -a env_args=(
    env -i
    "HOME=$home"
    "USER=$DEPLOY_USER"
    "LOGNAME=$DEPLOY_USER"
    "PATH=$(deploy_path)"
    "LANG=C.UTF-8"
    "LC_ALL=C.UTF-8"
  )
  local -a shell_args=(bash --noprofile --norc -c 'cd -- "$1" || exit $?; shift; umask "$1" || exit $?; shift; exec "$@"' bash "$cwd" "$mask" "$@")

  if [[ "$(id -un)" == "$DEPLOY_USER" ]]; then
    "${env_args[@]}" "${shell_args[@]}"
  elif [[ $EUID -eq 0 ]]; then
    runuser -u "$DEPLOY_USER" -- "${env_args[@]}" "${shell_args[@]}"
  else
    error "Need root or user '$DEPLOY_USER' to execute deploy-user commands."
    return 1
  fi
}

run_as_deploy_worktree() {
  local home; home=$(deploy_home)
  run_as_deploy_in_umask "$home" "$WORKTREE_UMASK" "$@"
}

run_as_deploy_git() { run_as_deploy git -C "$APP_DIR" "$@"; }
run_as_deploy_git_worktree() { run_as_deploy_worktree git -C "$APP_DIR" "$@"; }

run_as_deploy_capture() {
  # Capture stdout+stderr into the run log. On failure, show the actual tail to
  # the operator instead of replacing it with a generic SSH/Git warning.
  local label="$1"; shift
  local tmp rc
  tmp=$(mktemp /tmp/sahabino-command.XXXXXX)
  set +e
  run_as_deploy "$@" >"$tmp" 2>&1
  rc=$?
  set -e
  {
    printf '\n--- %s (exit %s) ---\n' "$label" "$rc"
    cat "$tmp"
  } >> "$RUN_LOG"
  if (( rc != 0 )); then
    warn "$label failed (exit $rc). Actual error:"
    tail -n 20 "$tmp" >&2 || true
  fi
  rm -f "$tmp"
  return "$rc"
}

validate_socks_proxy() {
  local value="$1" host port
  [[ "$value" =~ ^[A-Za-z0-9._-]+:[0-9]{1,5}$ ]] || {
    error "Invalid SOCKS proxy '$value'. Expected HOST:PORT (for example 127.0.0.1:8080)."
    return 1
  }
  host="${value%:*}"; port="${value##*:}"
  (( port >= 1 && port <= 65535 )) || {
    error "SOCKS proxy port is out of range: $port"
    return 1
  }
  [[ -n "$host" ]]
}

socks_proxy_works() {
  local proxy="$1" host port
  validate_socks_proxy "$proxy" >/dev/null || return 1
  host="${proxy%:*}"; port="${proxy##*:}"
  curl -fsS \
    --socks5-hostname "$host:$port" \
    --connect-timeout 5 \
    --max-time 12 \
    -o /dev/null \
    "$PYPI_INDEX_URL/ansible-core/" >/dev/null 2>&1
}

direct_pypi_works() {
  # A short probe is intentional. If the direct route takes tens of seconds or
  # resets mid-body (the failure observed on the production network), prefer a
  # healthy local SOCKS tunnel rather than making every pip request wait.
  curl -fsS \
    --connect-timeout 5 \
    --max-time 10 \
    -o /dev/null \
    "$PYPI_INDEX_URL/ansible-core/" >/dev/null 2>&1
}

ensure_proxychains_available() {
  have proxychains4 && return 0
  [[ $EUID -eq 0 ]] || {
    error "proxychains4 is required to use the SOCKS proxy and root is needed to install it."
    return 1
  }
  info "Installing proxychains4 for dependency-only SOCKS routing."
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends proxychains4 >/dev/null
  have proxychains4 || { error "proxychains4 installation did not provide proxychains4."; return 1; }
}

activate_dependency_proxy() {
  local proxy="$1" host port
  validate_socks_proxy "$proxy"
  socks_proxy_works "$proxy" || {
    error "SOCKS proxy $proxy cannot fetch the PyPI ansible-core index."
    warn "If this is an SSH dynamic tunnel, keep it open in another session, for example: ssh -D ${proxy} -N USER@PROXY_HOST"
    return 1
  }
  ensure_proxychains_available
  host="${proxy%:*}"; port="${proxy##*:}"
  PROXYCHAINS_CONFIG=$(mktemp /tmp/sahabino-proxychains.XXXXXX)
  cat > "$PROXYCHAINS_CONFIG" <<EOF_PROXY
strict_chain
proxy_dns
quiet_mode

tcp_read_time_out 120000
tcp_connect_time_out 15000

[ProxyList]
socks5 $host $port
EOF_PROXY
  chmod 0644 "$PROXYCHAINS_CONFIG"
  SOCKS_PROXY="$proxy"
  DEPENDENCY_PROXY_ENABLED=1
  ok "Dependency downloads will use SOCKS5 $SOCKS_PROXY (pip and Ansible Galaxy only)."
}

configure_dependency_network() {
  # Explicit proxy wins. Otherwise try the normal route first, then opportunistically
  # test the conventional local SSH dynamic-forward endpoint used for this project.
  if [[ -n "$SOCKS_PROXY" ]]; then
    activate_dependency_proxy "$SOCKS_PROXY"
    return $?
  fi

  if direct_pypi_works; then
    ok "Direct PyPI route passed the dependency network probe."
    return 0
  fi

  warn "Direct PyPI route failed or was too slow during a full-body probe."
  if (( SOCKS_PROXY_DISABLED )); then
    warn "Automatic SOCKS fallback is disabled; dependency installs will use the direct route."
    return 0
  fi

  if socks_proxy_works "$DEFAULT_SOCKS_PROXY"; then
    info "Healthy local SOCKS5 proxy detected at $DEFAULT_SOCKS_PROXY."
    if (( ASSUME_YES )) || ask_yes_no "Use $DEFAULT_SOCKS_PROXY for dependency downloads?" yes; then
      activate_dependency_proxy "$DEFAULT_SOCKS_PROXY"
      return 0
    fi
  else
    info "No working SOCKS5 fallback detected at $DEFAULT_SOCKS_PROXY."
  fi

  warn "Continuing without a dependency proxy. pip will use extended timeout/retry settings."
}

run_dependency_as_deploy() {
  if (( DEPENDENCY_PROXY_ENABLED )); then
    run_as_deploy proxychains4 -q -f "$PROXYCHAINS_CONFIG" "$@"
  else
    run_as_deploy "$@"
  fi
}

run_dependency_logged() {
  # Dependency install output is safe to log and is invaluable when network
  # failures are otherwise collapsed by pip into 'No matching distribution'.
  local rc
  set +e
  if (( DEPENDENCY_PROXY_ENABLED )); then
    run_as_deploy proxychains4 -q -f "$PROXYCHAINS_CONFIG" "$@" 2>&1 | tee -a "$RUN_LOG"
  else
    run_as_deploy "$@" 2>&1 | tee -a "$RUN_LOG"
  fi
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}

diagnose_dependency_failure() {
  local log="$1"
  if grep -qiE 'Read timed out|Connection reset by peer|Could not fetch URL|Temporary failure in name resolution|Network is unreachable' "$log"; then
    warn "Dependency installation failed because the package index/download route is unstable."
    if (( DEPENDENCY_PROXY_ENABLED )); then
      warn "The configured SOCKS proxy '$SOCKS_PROXY' was in use; verify that the SSH tunnel is still alive."
    else
      warn "A local SSH SOCKS tunnel can be supplied with --socks-proxy HOST:PORT (for example 127.0.0.1:8080)."
    fi
    return 0
  fi
  if grep -qiE 'No matching distribution found for ansible-core|Could not find a version that satisfies the requirement ansible-core' "$log"; then
    warn "pip found no ansible-core candidates. This can be a misleading result when the PyPI response body timed out."
    warn "Review the preceding pip network error in $RUN_LOG."
  fi
}

project_vars_file_from_path() {
  local base="$1"
  printf '%s/deploy/ansible/group_vars/production/vars.yml' "$base"
}

simple_yaml_scalar() {
  # Reads a simple top-level scalar from vars.yml. Production values consumed
  # here are plain strings/ints; complex YAML is intentionally not parsed here.
  local file="$1" key="$2"
  awk -v k="$key" '
    $0 ~ "^" k ":[[:space:]]*" {
      sub("^[^:]+:[[:space:]]*", "", $0)
      sub(/[[:space:]]+#.*$/, "", $0)
      gsub(/^['\''\"]|['\''\"]$/, "", $0)
      print; exit
    }
  ' "$file"
}

normalize_repository_url() {
  local origin="$1"
  case "$origin" in
    "git@$GITHUB_ALIAS:"*|"ssh://git@$GITHUB_ALIAS/"*)
      printf '%s' "$origin"
      ;;
    "git@$GITHUB_HOST:"*)
      printf 'git@%s:%s' "$GITHUB_ALIAS" "${origin#git@$GITHUB_HOST:}"
      ;;
    "ssh://git@$GITHUB_HOST/"*)
      printf 'ssh://git@%s/%s' "$GITHUB_ALIAS" "${origin#ssh://git@$GITHUB_HOST/}"
      ;;
    "https://$GITHUB_HOST/"*)
      printf 'git@%s:%s' "$GITHUB_ALIAS" "${origin#https://$GITHUB_HOST/}"
      ;;
    *)
      return 1
      ;;
  esac
}

git_origin_from_config_file() {
  # Read-only fallback for normal clones when invoking Git through sudo/runuser
  # is blocked by local permissions/safe-directory policy. Worktrees still use
  # the Git-command path above because their .git is an indirection file.
  local dir="$1" config
  config="$dir/.git/config"
  [[ -f "$config" ]] || return 1
  awk '
    /^\[remote "origin"\][[:space:]]*$/ { in_origin=1; next }
    /^\[/ { in_origin=0 }
    in_origin && /^[[:space:]]*url[[:space:]]*=/ {
      sub(/^[[:space:]]*url[[:space:]]*=[[:space:]]*/, "", $0); print; exit
    }
  ' "$config"
}

git_origin_from_checkout() {
  local dir="$1" origin=""
  [[ -d "$dir" ]] || return 1

  # When sudo is used from an operator checkout, ask Git as that operator.
  # This avoids root safe.directory warnings and also supports worktrees where
  # .git is a file instead of a directory.
  if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]] && id "$SUDO_USER" >/dev/null 2>&1; then
    origin=$(runuser -u "$SUDO_USER" -- git -C "$dir" remote get-url origin 2>/dev/null || true)
  else
    origin=$(git -c "safe.directory=$dir" -C "$dir" remote get-url origin 2>/dev/null || true)
  fi

  if [[ -z "$origin" ]]; then
    origin=$(git_origin_from_config_file "$dir" 2>/dev/null || true)
  fi

  [[ -n "$origin" ]] || return 1
  printf '%s' "$origin"
}

discover_repository_url() {
  [[ -n "$REPOSITORY_URL" ]] && return 0
  local candidate value origin normalized repo_root

  repo_root=$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd -P || true)

  # Prefer the checkout the operator actually invoked the assistant from.
  # Then try the checkout containing the script and finally the production
  # checkout. This works for normal clones and Git worktrees.
  for candidate in "$INVOCATION_DIR" "$repo_root" "$APP_DIR"; do
    [[ -n "$candidate" ]] || continue
    origin=$(git_origin_from_checkout "$candidate" || true)
    [[ -n "$origin" ]] || continue
    normalized=$(normalize_repository_url "$origin" || true)
    if [[ -n "$normalized" ]]; then
      REPOSITORY_URL="$normalized"
      info "Repository discovered automatically from checkout: $candidate"
      return 0
    fi
  done

  for candidate in \
    "$(project_vars_file_from_path "$APP_DIR")" \
    "$SCRIPT_DIR/group_vars/production/vars.yml"; do
    [[ -f "$candidate" ]] || continue
    value=$(simple_yaml_scalar "$candidate" sahabino_repository_url || true)
    if [[ -n "$value" ]]; then
      normalized=$(normalize_repository_url "$value" || true)
      REPOSITORY_URL="${normalized:-$value}"
      info "Repository URL discovered from project production vars."
      return 0
    fi
  done

  if (( ASSUME_YES )); then
    error "Repository URL could not be discovered. Supply --repository-url or SAHABINO_REPOSITORY_URL."
    return 1
  fi

  warn "Repository URL could not be discovered from the invoking checkout or project vars."
  while [[ -z "$REPOSITORY_URL" ]]; do
    read -r -p "Production read-only Git URL (for example git@$GITHUB_ALIAS:OWNER/REPO.git): " REPOSITORY_URL
  done
}

validate_repository_url_policy() {
  case "$REPOSITORY_URL" in
    "git@$GITHUB_ALIAS:"*|"ssh://git@$GITHUB_ALIAS/"*) return 0 ;;
    *)
      error "Production repository URL must use the managed read-only SSH alias '$GITHUB_ALIAS'."
      warn "Configured URL: $REPOSITORY_URL"
      warn "Expected form: git@$GITHUB_ALIAS:OWNER/REPOSITORY.git"
      return 1
      ;;
  esac
}

load_project_config() {
  local vars value
  vars=$(project_vars_file_from_path "$APP_DIR")
  [[ -f "$vars" ]] || { warn "Production vars not found at $vars; keeping wrapper defaults."; return 0; }

  value=$(simple_yaml_scalar "$vars" sahabino_compose_project_name || true); [[ -n "$value" ]] && COMPOSE_PROJECT_NAME="$value"
  value=$(simple_yaml_scalar "$vars" sahabino_api_bind_address || true); [[ -n "$value" ]] && API_BIND_ADDRESS="$value"
  value=$(simple_yaml_scalar "$vars" sahabino_api_port || true); [[ -n "$value" ]] && API_PORT="$value"
  value=$(simple_yaml_scalar "$vars" sahabino_ingestion_consumer_group_id || true); [[ -n "$value" ]] && INGESTION_CONSUMER_GROUP="$value"

  if (( ! REPOSITORY_URL_EXPLICIT )); then
    value=$(simple_yaml_scalar "$vars" sahabino_repository_url || true)
    if [[ -n "$value" ]]; then
      local normalized_repository
      normalized_repository=$(normalize_repository_url "$value" || true)
      [[ -n "$normalized_repository" ]] || {
        error "Production vars contain an unsupported repository URL: $value"
        return 1
      }
      REPOSITORY_URL="$normalized_repository"
      validate_repository_url_policy || return 1
    fi
  fi

  info "Project config: compose=$COMPOSE_PROJECT_NAME api=$API_BIND_ADDRESS:$API_PORT ingestion_group=$INGESTION_CONSUMER_GROUP"
}

ubuntu_preflight() {
  step "Host preflight"
  [[ -r /etc/os-release ]] || { error "/etc/os-release not found"; return 1; }
  # shellcheck disable=SC1091
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" ]] || { error "Production automation supports Ubuntu; detected '${ID:-unknown}'."; return 1; }

  if array_contains "${VERSION_ID:-unknown}" "${SUPPORTED_UBUNTU_RELEASES[@]}"; then
    ok "Ubuntu ${VERSION_ID} is a tested release."
  else
    # Do not block normal operation just because the Ubuntu release is newer
    # than the release(s) used during development. Real capability checks below
    # are more useful than a rigid version gate.
    warn "Ubuntu ${VERSION_ID:-unknown} has not been explicitly tested by this assistant (tested: ${SUPPORTED_UBUNTU_RELEASES[*]})."
    warn "Continuing with capability-based checks instead of blocking deployment."
  fi

  local required_host_cmd
  for required_host_cmd in apt-get systemctl python3; do
    command -v "$required_host_cmd" >/dev/null 2>&1 || {
      error "Required host command is unavailable: $required_host_cmd"
      return 1
    }
  done

  local avail_kb avail_gb mem_kb mem_gib swap_kb swap_gib
  avail_kb=$(df -Pk / | awk 'NR==2 {print $4}'); avail_gb=$(( avail_kb / 1024 / 1024 ))
  mem_kb=$(awk '/MemTotal:/ {print $2}' /proc/meminfo); mem_gib=$(( mem_kb / 1024 / 1024 ))
  swap_kb=$(awk '/SwapTotal:/ {print $2}' /proc/meminfo); swap_gib=$(( swap_kb / 1024 / 1024 ))
  info "Disk available on /: ~${avail_gb} GiB"
  info "RAM: ~${mem_gib} GiB; swap: ~${swap_gib} GiB"
  (( avail_gb >= 10 )) || warn "Less than 10 GiB is free; Docker/Kafka/Loki/PostgreSQL may exhaust disk."
  (( mem_kb >= 3000000 )) || warn "Less than ~3 GiB RAM detected; full production stack may be constrained."

  if (( swap_kb < 1000000 )) && (( mem_kb <= 5000000 )) && [[ $EUID -eq 0 ]]; then
    if ask_yes_no "Low/no swap detected. Create a ${DEFAULT_SWAP_GIB} GiB /swapfile?" yes; then
      ensure_swapfile "$DEFAULT_SWAP_GIB"
    else
      warn "Continuing without the recommended swap safety margin."
    fi
  fi

  if [[ -f /var/run/reboot-required ]]; then
    warn "Ubuntu reports that a reboot is required."
    if (( NO_REBOOT )); then
      warn "--no-reboot was supplied; continuing."
    else
      # Never reboot automatically from the deployment assistant. This VPS may
      # host unrelated services (for example VPN/X-Ray); reboot is an explicit
      # operator maintenance action after deployment/verification.
      warn "Continuing without reboot. Schedule a controlled reboot later, then run --verify."
    fi
  fi
}

ensure_swapfile() {
  local size_gib="$1"
  if swapon --show=NAME --noheadings 2>/dev/null | grep -qx '/swapfile'; then ok "/swapfile is already enabled."; return 0; fi
  if [[ -e /swapfile ]]; then
    warn "/swapfile exists but is not active; refusing to overwrite it automatically."
    return 1
  fi
  info "Creating ${size_gib} GiB /swapfile..."
  if have fallocate; then fallocate -l "${size_gib}G" /swapfile; else dd if=/dev/zero of=/swapfile bs=1M count=$(( size_gib * 1024 )) status=progress; fi
  chmod 0600 /swapfile; mkswap /swapfile >/dev/null; swapon /swapfile
  grep -Eq '^/swapfile[[:space:]]' /etc/fstab || printf '/swapfile none swap sw 0 0\n' >> /etc/fstab
  ok "Swap enabled and persisted in /etc/fstab."
}

ensure_base_packages() {
  step "Base host dependencies"
  [[ $EUID -eq 0 ]] || return 0

  local -a required_packages=(
    ca-certificates curl gnupg openssl git openssh-client iproute2
    python3 python3-venv python3-pip
  )
  local -a missing_packages=()
  local pkg
  for pkg in "${required_packages[@]}"; do
    if ! dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'ok installed'; then
      missing_packages+=("$pkg")
    fi
  done

  if (( ${#missing_packages[@]} == 0 )); then
    ok "Base packages are already installed; skipping apt update/install."
    return 0
  fi

  info "Installing missing base packages: ${missing_packages[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends "${missing_packages[@]}" >/dev/null
  ok "Base packages are installed."
}

ensure_deploy_user() {
  step "Deployment account"
  if id "$DEPLOY_USER" >/dev/null 2>&1; then
    ok "Deployment account '$DEPLOY_USER' exists."
  else
    [[ $EUID -eq 0 ]] || { error "User '$DEPLOY_USER' does not exist and root is required to create it."; return 1; }
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"; ok "Created '$DEPLOY_USER'."
  fi
  getent group "$DEPLOY_GROUP" >/dev/null 2>&1 || { [[ $EUID -eq 0 ]] && groupadd "$DEPLOY_GROUP"; }
  if ! id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx "$DEPLOY_GROUP"; then
    [[ $EUID -eq 0 ]] || return 1; usermod -aG "$DEPLOY_GROUP" "$DEPLOY_USER"
  fi
  if [[ $EUID -eq 0 ]]; then
    install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$APP_PARENT" "$RUNTIME_DIR" "$ANSIBLE_COLLECTIONS_DIR"
  fi
}

docker_state_summary() {
  local containers volumes images
  containers=$(docker ps -aq 2>/dev/null | wc -l | tr -d ' ')
  volumes=$(docker volume ls -q 2>/dev/null | wc -l | tr -d ' ')
  images=$(docker images -q 2>/dev/null | sort -u | wc -l | tr -d ' ')
  printf '%s %s %s' "$containers" "$volumes" "$images"
}

docker_has_persistent_state() {
  local containers volumes images
  IFS=' ' read -r containers volumes images <<<"$(docker_state_summary)"
  (( containers > 0 || volumes > 0 ))
}

install_docker_ce() {
  [[ $EUID -eq 0 ]] || { error "Root is required to install Docker CE."; return 1; }
  info "Installing Docker CE from Docker's Ubuntu repository..."
  local pkg
  for pkg in docker.io docker-compose docker-compose-v2 podman-docker; do
    if dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q 'ok installed'; then
      if have docker && docker info >/dev/null 2>&1 && docker_has_persistent_state; then
        error "Existing distro Docker package '$pkg' has containers/volumes. Automatic replacement is blocked."
        return 1
      fi
      apt-get remove -y "$pkg" >/dev/null
    fi
  done
  if dpkg-query -W -f='${Status}' containerd 2>/dev/null | grep -q 'ok installed' && systemctl is-active --quiet containerd 2>/dev/null; then
    error "An active distro containerd service exists. Refusing automatic removal."
    return 1
  fi
  dpkg-query -W -f='${Status}' containerd 2>/dev/null | grep -q 'ok installed' && apt-get remove -y containerd >/dev/null || true
  dpkg-query -W -f='${Status}' runc 2>/dev/null | grep -q 'ok installed' && apt-get remove -y runc >/dev/null || true

  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  source /etc/os-release
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
    "$(dpkg --print-architecture)" "${UBUNTU_CODENAME:-$VERSION_CODENAME}" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker >/dev/null; hash -r || true
  ok "Docker CE installed."
}

migrate_snap_docker() {
  [[ $EUID -eq 0 ]] || { error "Root is required to migrate Snap Docker."; return 1; }
  warn "Docker is installed through Snap. Snap confinement can hide /opt from Compose."
  local containers=0 volumes=0 images=0
  if docker info >/dev/null 2>&1; then
    IFS=' ' read -r containers volumes images <<<"$(docker_state_summary)"
    info "Snap Docker state: containers=$containers volumes=$volumes images=$images"
    if (( containers > 0 || volumes > 0 )); then
      error "Snap Docker has containers or volumes. Automatic removal is blocked to avoid data loss."
      docker ps -a || true; docker volume ls || true
      return 1
    fi
    (( images == 0 )) || warn "Snap Docker has image cache only. Images are rebuildable but will be discarded if Snap is removed."
  else
    warn "Snap Docker daemon is not currently reachable."
  fi

  (( SKIP_DOCKER_MIGRATION == 0 )) || { error "Snap Docker detected and --skip-docker-migration was supplied."; return 1; }
  ask_yes_no "Replace Snap Docker with official Docker CE?" yes || { error "Official Docker CE is required for this /opt-based layout."; return 1; }
  snap remove docker >/dev/null; hash -r || true; install_docker_ce
}

ensure_docker() {
  step "Docker Engine and Compose"
  if have snap && snap list docker >/dev/null 2>&1; then
    migrate_snap_docker
  elif [[ "$(command -v docker 2>/dev/null || true)" == "/snap/bin/docker" ]]; then
    migrate_snap_docker
  elif ! have docker; then
    install_docker_ce
  fi
  hash -r || true
  if ! docker info >/dev/null 2>&1; then
    error "Docker Engine is installed but not reachable."; systemctl status docker --no-pager || true; return 1
  fi
  local compose_version
  compose_version=$(docker compose version --short 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true)
  [[ -n "$compose_version" ]] || { error "Docker Compose v2 plugin is not available."; return 1; }
  version_ge "$compose_version" "$MIN_COMPOSE_VERSION" || { error "Docker Compose >= $MIN_COMPOSE_VERSION required; found $compose_version."; return 1; }
  ok "Docker $(docker --version | sed 's/^Docker version //') / Compose $compose_version are reachable."
  if [[ $EUID -eq 0 ]]; then
    getent group docker >/dev/null 2>&1 || groupadd docker
    if ! id -nG "$DEPLOY_USER" | tr ' ' '\n' | grep -qx docker; then
      usermod -aG docker "$DEPLOY_USER"; warn "Added '$DEPLOY_USER' to docker group; new login sessions pick up membership."
    fi
  fi
}

check_reserved_ports() {
  step "Port conflict check"
  local ports=("$API_PORT" "$GRAFANA_PORT" "$LOKI_PORT" "$ALLOY_PORT" "$OBJECT_STORAGE_PORT")
  local p lines unexpected=0
  for p in "${ports[@]}"; do
    lines=$(ss -lntp "sport = :$p" 2>/dev/null | tail -n +2 || true)
    [[ -z "$lines" ]] && continue
    if docker ps --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" --format '{{.Ports}}' 2>/dev/null | grep -Eq "(^|[,:[:space:]])(127\\.0\\.0\\.1:|0\\.0\\.0\\.0:|:::)?${p}->"; then
      info "Port $p is already used by the existing '$COMPOSE_PROJECT_NAME' Compose project."
    else
      warn "Port $p is already listening and does not belong to the expected Compose project:"
      printf '%s\n' "$lines" >&2; unexpected=1
    fi
  done
  if (( unexpected )) && [[ "$MODE" != "verify" ]]; then
    error "Resolve unexpected port conflicts before deploying; unrelated services will not be stopped."; return 1
  fi
  (( unexpected )) || ok "No unexpected project port conflicts detected."
}

write_pinned_github_known_hosts() {
  local file="$1"
  if [[ "$GITHUB_HOST" != "github.com" ]]; then
    error "Pinned host-key bootstrap is implemented only for github.com; configured host is '$GITHUB_HOST'."
    warn "For a custom Git host, provision and validate a dedicated known_hosts file manually before using this assistant."
    return 1
  fi
  cat > "$file" <<EOF_HOSTS
$GITHUB_ED25519_KEY
$GITHUB_ECDSA_KEY
$GITHUB_RSA_KEY
EOF_HOSTS
  chmod 0644 "$file"
  [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$file"

  local fp
  while read -r fp; do
    array_contains "$fp" "${GITHUB_ALLOWED_FINGERPRINTS[@]}" || {
      error "Pinned GitHub known_hosts contains an unexpected fingerprint: $fp"; return 1;
    }
  done < <(ssh-keygen -lf "$file" -E sha256 | awk '{print $2}')
  ok "Pinned GitHub host keys verified against the script allowlist."
}

ssh_alias_values() {
  local alias="$1"
  run_as_deploy ssh -G "$alias" 2>/dev/null
}

validate_ssh_alias() {
  local alias="$1" key_path="$2" known_hosts_file="$3" cfg
  cfg=$(ssh_alias_values "$alias") || return 1
  local hostname user identities strict batch connect_timeout connection_attempts ident kh
  hostname=$(awk '$1=="hostname"{print $2;exit}' <<<"$cfg")
  user=$(awk '$1=="user"{print $2;exit}' <<<"$cfg")
  identities=$(awk '$1=="identitiesonly"{print $2;exit}' <<<"$cfg")
  batch=$(awk '$1=="batchmode"{print $2;exit}' <<<"$cfg")
  connect_timeout=$(awk '$1=="connecttimeout"{print $2;exit}' <<<"$cfg")
  connection_attempts=$(awk '$1=="connectionattempts"{print $2;exit}' <<<"$cfg")
  strict=$(awk '$1=="stricthostkeychecking"{print $2;exit}' <<<"$cfg")
  ident=$(awk '$1=="identityfile"{print $2}' <<<"$cfg" | grep -Fx "$key_path" | head -n1 || true)
  kh=$(awk '$1=="userknownhostsfile"{for(i=2;i<=NF;i++) print $i}' <<<"$cfg" | grep -Fx "$known_hosts_file" | head -n1 || true)

  [[ "$hostname" == "$GITHUB_HOST" ]] || { warn "SSH alias hostname is '$hostname', expected '$GITHUB_HOST'."; return 1; }
  [[ "$user" == "git" ]] || { warn "SSH alias user is '$user', expected 'git'."; return 1; }
  [[ "$identities" == "yes" ]] || { warn "SSH alias IdentitiesOnly is '$identities', expected 'yes'."; return 1; }
  [[ "$batch" == "yes" ]] || { warn "SSH alias BatchMode is '$batch', expected 'yes'."; return 1; }
  [[ "$connect_timeout" == "10" ]] || { warn "SSH alias ConnectTimeout is '$connect_timeout', expected '10'."; return 1; }
  [[ "$connection_attempts" == "1" ]] || { warn "SSH alias ConnectionAttempts is '$connection_attempts', expected '1'."; return 1; }
  [[ "$strict" == "yes" || "$strict" == "true" ]] || { warn "SSH alias StrictHostKeyChecking is '$strict', expected 'yes'."; return 1; }
  [[ -n "$ident" ]] || { warn "SSH alias does not resolve the expected IdentityFile '$key_path'."; return 1; }
  [[ -n "$kh" ]] || { warn "SSH alias does not resolve the pinned UserKnownHostsFile '$known_hosts_file'."; return 1; }
  return 0
}

replace_managed_ssh_block() {
  local config_path="$1" block_file="$2"
  python3 - "$config_path" "$block_file" "$GITHUB_ALIAS" <<'PY'
from pathlib import Path
import sys
cfg = Path(sys.argv[1]); block = Path(sys.argv[2]).read_text()
alias = sys.argv[3]
begin = f"# BEGIN SAHABINO DEPLOY ASSISTANT: {alias}"
end = f"# END SAHABINO DEPLOY ASSISTANT: {alias}"
text = cfg.read_text() if cfg.exists() else ""
if begin in text and end in text:
    before, rest = text.split(begin, 1)
    _, after = rest.split(end, 1)
    text = before.rstrip() + "\n\n" + block.rstrip() + "\n" + after.lstrip("\n")
else:
    text = text.rstrip() + ("\n\n" if text.strip() else "") + block.rstrip() + "\n"
cfg.write_text(text)
PY
}

upgrade_legacy_ssh_block() {
  local config_path="$1" block_file="$2" backup rc
  backup=$(mktemp /tmp/sahabino-ssh-config-backup.XXXXXX)
  cp -a "$config_path" "$backup" || {
    error "Could not create a safety backup of $config_path before SSH migration."
    rm -f "$backup"
    return 1
  }

  set +e
  python3 - "$config_path" "$block_file" "$GITHUB_ALIAS" <<'PY_LEGACY'
from pathlib import Path
import sys
path = Path(sys.argv[1]); block = Path(sys.argv[2]).read_text().rstrip()
alias = sys.argv[3]
lines = path.read_text().splitlines() if path.exists() else []
legacy = "# Managed by Sahabino deployment assistant"
start = None
for i, line in enumerate(lines):
    if line.strip() == legacy:
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and lines[j].strip() == f"Host {alias}":
            start = i
            break
if start is None:
    raise SystemExit(2)
end = len(lines)
for k in range(start + 1, len(lines)):
    if lines[k].lstrip().startswith("Host ") and lines[k].strip() != f"Host {alias}":
        end = k
        break
new = lines[:start]
while new and not new[-1].strip():
    new.pop()
if new:
    new += [""]
new += block.splitlines()
rest = lines[end:]
if rest:
    new += [""] + rest
path.write_text("\n".join(new).rstrip() + "\n")
PY_LEGACY
  rc=$?
  set -e

  if (( rc != 0 )); then
    cp -a "$backup" "$config_path" || true
    rm -f "$backup"
    error "Legacy SSH alias migration failed (exit $rc); original config was restored."
    return 1
  fi

  rm -f "$backup"
  append_log "[INFO] Legacy SSH alias block rewritten successfully."
  return 0
}


git_repository_read_test() {
  local label="$1"
  # SSH config already enforces BatchMode and a short connect timeout. The
  # outer timeout also bounds DNS/TCP edge cases so an interactive deployment
  # cannot hang indefinitely on repository verification.
  run_as_deploy_capture "$label" timeout 25s git ls-remote "$REPOSITORY_URL"
}

ensure_deploy_key_and_alias() {
  step "GitHub read-only Deploy Key"
  local home ssh_dir key_path pub_path config_path known_hosts_file block_file extracted actual expected fp
  home=$(deploy_home); ssh_dir="$home/.ssh"; key_path="$ssh_dir/$DEPLOY_KEY_BASENAME"; pub_path="$key_path.pub"
  config_path="$ssh_dir/config"; known_hosts_file="$ssh_dir/known_hosts.github"

  if [[ $EUID -eq 0 ]]; then install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$ssh_dir"; else mkdir -p "$ssh_dir"; chmod 0700 "$ssh_dir"; fi

  if [[ ! -f "$key_path" ]]; then
    warn "Dedicated deploy key not found: $key_path"
    if ask_yes_no "Generate a dedicated Ed25519 deploy key now?" yes; then
      run_as_deploy ssh-keygen -q -t ed25519 -f "$key_path" -C "sahabino-production-deploy" -N ''
      ok "Deploy key generated."
    else
      warn "Create the key as '$DEPLOY_USER' at $key_path, then re-run."; return 1
    fi
  fi
  chmod 0600 "$key_path"; [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$key_path"

  extracted=$(run_as_deploy ssh-keygen -y -f "$key_path") || { error "Private deploy key cannot be read by ssh-keygen."; return 1; }
  if [[ ! -f "$pub_path" ]]; then
    info "Public deploy key is missing; regenerating it from the private key."
    printf '%s sahabino-production-deploy\n' "$extracted" > "$pub_path"
  fi
  chmod 0644 "$pub_path"; [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$pub_path"

  expected=$(awk '{print $1" "$2}' <<<"$extracted")
  actual=$(awk 'NF>=2{print $1" "$2; exit}' "$pub_path")
  [[ "$expected" == "$actual" ]] || { error "Private/public deploy key files do not form the same key pair."; return 1; }
  fp=$(ssh-keygen -lf "$pub_path" -E sha256 | awk '{print $2}')
  info "Deploy key fingerprint: $fp"

  write_pinned_github_known_hosts "$known_hosts_file"

  touch "$config_path"; chmod 0600 "$config_path"; [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$config_path"
  block_file=$(mktemp /tmp/sahabino-ssh-block.XXXXXX)
  cat > "$block_file" <<EOF_SSH
# BEGIN SAHABINO DEPLOY ASSISTANT: $GITHUB_ALIAS
Host $GITHUB_ALIAS
    HostName $GITHUB_HOST
    User git
    IdentityFile $key_path
    IdentitiesOnly yes
    BatchMode yes
    ConnectTimeout 10
    ConnectionAttempts 1
    UserKnownHostsFile $known_hosts_file
    StrictHostKeyChecking yes
# END SAHABINO DEPLOY ASSISTANT: $GITHUB_ALIAS
EOF_SSH

  if grep -Eq "^[[:space:]]*Host[[:space:]]+$GITHUB_ALIAS([[:space:]]|$)" "$config_path"; then
    if validate_ssh_alias "$GITHUB_ALIAS" "$key_path" "$known_hosts_file"; then
      ok "Existing SSH alias '$GITHUB_ALIAS' resolves to the expected secure configuration."
    elif grep -Fq "# BEGIN SAHABINO DEPLOY ASSISTANT: $GITHUB_ALIAS" "$config_path"; then
      warn "Managed SSH alias drift detected; repairing only the assistant-managed block."
      replace_managed_ssh_block "$config_path" "$block_file"
    elif grep -Fq "# Managed by Sahabino deployment assistant" "$config_path"; then
      warn "Legacy assistant-managed SSH alias detected; upgrading it to pinned-host-key policy."
      if ! upgrade_legacy_ssh_block "$config_path" "$block_file"; then
        rm -f "$block_file"
        error "Legacy marker exists but could not be safely associated with Host $GITHUB_ALIAS; refusing automatic edit."
        return 1
      fi
      ok "Legacy SSH alias block upgraded."
    else
      rm -f "$block_file"
      error "SSH alias '$GITHUB_ALIAS' exists but does not match production policy and is not assistant-managed."
      warn "Fix/remove that alias manually; the assistant will not overwrite an unmanaged SSH block."
      return 1
    fi
  else
    replace_managed_ssh_block "$config_path" "$block_file"
    ok "Added managed SSH alias '$GITHUB_ALIAS'."
  fi
  rm -f "$block_file"
  if ! chmod 0600 "$config_path"; then
    error "Could not set mode 0600 on $config_path."
    return 1
  fi
  if [[ $EUID -eq 0 ]] && ! chown "$DEPLOY_USER:$DEPLOY_GROUP" "$config_path"; then
    error "Could not set ownership $DEPLOY_USER:$DEPLOY_GROUP on $config_path."
    return 1
  fi
  if ! validate_ssh_alias "$GITHUB_ALIAS" "$key_path" "$known_hosts_file"; then
    error "SSH alias validation still fails after configuration."
    warn "Resolved SSH settings (non-secret):"
    run_as_deploy ssh -G "$GITHUB_ALIAS" 2>&1 | grep -E '^(hostname|user|identityfile|identitiesonly|batchmode|connecttimeout|connectionattempts|stricthostkeychecking|userknownhostsfile) ' | tee -a "$RUN_LOG" >&2 || true
    return 1
  fi
  ok "SSH alias '$GITHUB_ALIAS' is configured and validated."

  discover_repository_url
  validate_repository_url_policy

  # The repository read itself is the authoritative SSH/authentication test.
  # Avoid an extra `ssh -T` probe: it has GitHub-specific exit semantics and
  # adds no value once host-key policy and the alias have already been validated.
  if git_repository_read_test "Git repository read test"; then
    ok "Deploy Key can read the Git repository."
    ok "GitHub deploy-key stage complete; continuing to the production checkout."
    return 0
  fi

  printf '\n%sPublic Deploy Key (fingerprint %s):%s\n' "$C_BOLD" "$fp" "$C_RESET"
  cat "$pub_path"
  cat <<EOF_GITHUB

Add this PUBLIC key in GitHub:
  Repository -> Settings -> Deploy keys -> Add deploy key
  - Title: Sahabino production VPS
  - Allow write access: OFF

Private key remains only at: $key_path
EOF_GITHUB

  if (( ASSUME_YES )); then
    error "Repository access is not ready. Add the Deploy Key in GitHub, then re-run."
    return 1
  fi

  local attempt reply
  for attempt in 1 2 3; do
    read -r -p "Press Enter after adding/fixing the Deploy Key (attempt $attempt/3), or type q to stop: " reply
    [[ "${reply,,}" == "q" ]] && return 1
    if git_repository_read_test "Git repository read retry $attempt"; then
      ok "GitHub Deploy Key verified."
      ok "GitHub deploy-key stage complete; continuing to the production checkout."
      return 0
    fi
  done
  error "Repository access failed after 3 retries."
  warn "Diagnosis: verify the shown fingerprint/key in GitHub, the repository URL '$REPOSITORY_URL', and the validated SSH alias '$GITHUB_ALIAS'."
  warn "Full SSH/Git stderr is preserved in $RUN_LOG."
  return 1
}

assert_safe_app_layout() {
  local parent_real app_real
  parent_real=$(realpath -m -- "$APP_PARENT")
  app_real=$(realpath -m -- "$APP_DIR")

  [[ "$app_real" == "$parent_real"/* ]] || {
    error "Application directory '$APP_DIR' must be below application parent '$APP_PARENT'."
    return 1
  }
  [[ ! -L "$APP_PARENT" ]] || { error "Application parent must not be a symlink: $APP_PARENT"; return 1; }
  [[ ! -L "$APP_DIR" ]] || { error "Application checkout must not be a symlink: $APP_DIR"; return 1; }
}

mode_has_other_read() {
  local mode="$1"
  (( (8#$mode & 004) != 0 ))
}

mode_has_other_exec() {
  local mode="$1"
  (( (8#$mode & 001) != 0 ))
}

protect_checkout_secrets() {
  local vault="$APP_DIR/deploy/ansible/group_vars/production/vault.yml"
  if [[ -f "$APP_DIR/.env" ]]; then
    chmod a-s,u=rw,go= "$APP_DIR/.env"
    [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$APP_DIR/.env"
  fi
  if [[ -f "$vault" ]]; then
    chmod a-s,u=rw,go= "$vault"
    [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$vault"
  fi
}

discover_relative_bind_sources() {
  # Parse Compose short-syntax bind mounts conservatively. We only emit
  # repository-relative sources (./...). Named volumes and absolute host paths
  # such as /var/run/docker.sock are deliberately ignored.
  python3 - "$APP_DIR/docker-compose.yml" "$APP_DIR/compose.prod.yml" <<'PY_BIND'
import pathlib, re, sys
seen = set()
short_pat = re.compile(r"^\s*-\s*[\"']?(\.{1,2}/[^:\"']+):")
source_pat = re.compile(r"^\s*source\s*:\s*[\"']?(\.{1,2}/[^\"']+)")
for name in sys.argv[1:]:
    p = pathlib.Path(name)
    if not p.is_file():
        continue
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = short_pat.match(line) or source_pat.match(line)
        if m:
            raw = m.group(1)
            rel = raw[2:] if raw.startswith("./") else raw
            if rel not in seen:
                seen.add(rel)
                print(rel)
PY_BIND
}

validate_container_bind_permissions() {
  local rel src mode failed=0
  while IFS= read -r rel; do
    [[ -n "$rel" ]] || continue
    [[ "$rel" != /* && "$rel" != ../* && "$rel" != *'/../'* ]] || {
      error "Unsafe repository-relative bind source in Compose: $rel"
      failed=1
      continue
    }
    src="$APP_DIR/$rel"
    if [[ ! -e "$src" ]]; then
      error "Compose bind source does not exist: $src"
      failed=1
      continue
    fi
    local resolved
    resolved=$(realpath -e -- "$src" 2>/dev/null || true)
    [[ -n "$resolved" && ( "$resolved" == "$APP_DIR" || "$resolved" == "$APP_DIR"/* ) ]] || {
      error "Compose bind source escapes the production checkout through a symlink: $src"
      failed=1
      continue
    }

    # Every repository directory between the checkout root and the bind source
    # must be traversable/readable for non-root container UIDs. The checkout
    # root itself intentionally remains 0750; Docker resolves the host source as
    # root before presenting the bind mount inside the container.
    local parent
    parent=$(dirname -- "$src")
    while [[ "$parent" != "$APP_DIR" && "$parent" == "$APP_DIR"/* ]]; do
      mode=$(stat -c '%a' "$parent")
      if ! mode_has_other_read "$mode" || ! mode_has_other_exec "$mode"; then
        error "Parent directory for container bind is not readable/traversable: $parent (mode $mode)"
        failed=1
      fi
      parent=$(dirname -- "$parent")
    done

    if [[ -f "$src" ]]; then
      mode=$(stat -c '%a' "$src")
      if ! mode_has_other_read "$mode"; then
        error "Container-mounted file is not readable by arbitrary container UIDs: $src (mode $mode)"
        failed=1
      fi
    elif [[ -d "$src" ]]; then
      while IFS= read -r -d '' d; do
        mode=$(stat -c '%a' "$d")
        if ! mode_has_other_read "$mode" || ! mode_has_other_exec "$mode"; then
          error "Container-mounted directory is not readable/traversable: $d (mode $mode)"
          failed=1
        fi
      done < <(find "$src" -type d -print0)
      while IFS= read -r -d '' f; do
        [[ -L "$f" ]] && continue
        mode=$(stat -c '%a' "$f")
        if ! mode_has_other_read "$mode"; then
          error "File below container-mounted directory is not readable: $f (mode $mode)"
          failed=1
        fi
      done < <(find "$src" -type f -print0)
    fi
  done < <(discover_relative_bind_sources)
  (( failed == 0 ))
}

normalize_checkout_permissions() {
  step "Production checkout permissions"
  assert_safe_app_layout
  [[ -d "$APP_DIR/.git" ]] || { error "Cannot normalize permissions before a Git checkout exists at $APP_DIR."; return 1; }

  # Keep the application root private to the deploy group. Tracked source below
  # it is normalized separately so bind-mounted container configs are readable.
  if [[ $EUID -eq 0 ]]; then
    install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$APP_PARENT"
    chown "$DEPLOY_USER:$DEPLOY_GROUP" "$APP_DIR"
  fi
  chmod a-s,u=rwx,g=rx,o= "$APP_DIR"

  local tmp record meta rel git_mode abs dir
  tmp=$(mktemp /tmp/sahabino-git-index.XXXXXX)
  run_as_deploy_git ls-files -s -z > "$tmp"

  declare -A dirs=()
  while IFS= read -r -d '' record; do
    meta=${record%%$'\t'*}
    rel=${record#*$'\t'}
    git_mode=${meta%% *}
    [[ -n "$rel" && "$rel" != /* && "$rel" != ../* && "$rel" != *'/../'* ]] || {
      rm -f "$tmp"
      error "Unsafe tracked path returned by Git: '$rel'"
      return 1
    }
    abs="$APP_DIR/$rel"

    case "$git_mode" in
      100644)
        [[ -e "$abs" && ! -L "$abs" ]] || continue
        chmod a-s,u=rw,go=r "$abs"
        [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$abs"
        ;;
      100755)
        [[ -e "$abs" && ! -L "$abs" ]] || continue
        chmod a-s,u=rwx,go=rx "$abs"
        [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$abs"
        ;;
      120000)
        # Never chmod through a symlink. Git records the link itself, not the
        # target's permissions.
        ;;
      160000)
        warn "Git submodule path '$rel' is present; its internal permissions are not modified automatically."
        ;;
      *)
        warn "Unknown Git index mode '$git_mode' for '$rel'; leaving its file mode unchanged."
        ;;
    esac

    dir=$(dirname -- "$rel")
    while [[ "$dir" != "." && "$dir" != "/" ]]; do
      dirs["$dir"]=1
      dir=$(dirname -- "$dir")
    done
  done < "$tmp"
  rm -f "$tmp"

  for dir in "${!dirs[@]}"; do
    [[ -d "$APP_DIR/$dir" && ! -L "$APP_DIR/$dir" ]] || continue
    chmod a-s,u=rwx,go=rx "$APP_DIR/$dir"
    [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$APP_DIR/$dir"
  done

  # Secrets are intentionally untracked and must remain owner-only even though
  # their parent directories are traversable/readable for container configs.
  protect_checkout_secrets

  validate_container_bind_permissions || {
    error "One or more Compose bind mounts still have unsafe/unusable permissions."
    return 1
  }

  # Changing only modes according to the Git index must never make the checkout
  # dirty. If it does, Git's executable-bit metadata and filesystem disagree.
  local dirty
  dirty=$(run_as_deploy_git status --porcelain)
  if [[ -n "$dirty" ]]; then
    error "Permission normalization unexpectedly changed Git-tracked state:"
    printf '%s\n' "$dirty" >&2
    return 1
  fi
  ok "Tracked worktree modes, container bind mounts, and checkout secrets are normalized."
}

validate_repository_ownership() {
  local owner group
  owner=$(stat -c '%U' "$APP_DIR")
  group=$(stat -c '%G' "$APP_DIR")
  if [[ "$owner" != "$DEPLOY_USER" ]]; then
    error "$APP_DIR is owned by '$owner:$group'; expected deploy owner '$DEPLOY_USER'."
    warn "Fix ownership deliberately before continuing; the assistant will not recursively chown an existing production checkout."
    return 1
  fi
  run_as_deploy test -r "$APP_DIR/.git/HEAD" || { error "'$DEPLOY_USER' cannot read $APP_DIR/.git."; return 1; }
  run_as_deploy test -w "$APP_DIR/.git" || { error "'$DEPLOY_USER' cannot write $APP_DIR/.git; fetch/checkout will fail."; return 1; }
}

ensure_repository() {
  step "Production Git checkout"
  discover_repository_url
  validate_repository_url_policy
  assert_safe_app_layout

  if [[ ! -e "$APP_DIR" && $EUID -eq 0 ]]; then install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$APP_DIR"; fi
  if [[ -d "$APP_DIR/.git" ]]; then
    ok "Existing Git checkout found at $APP_DIR."
    validate_repository_ownership
  else
    if [[ -d "$APP_DIR" ]] && [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
      error "$APP_DIR exists, is non-empty, and is not a Git checkout."; return 1
    fi
    rmdir "$APP_DIR" 2>/dev/null || true
    info "Cloning repository into $APP_DIR with worktree umask $WORKTREE_UMASK..."
    run_as_deploy_worktree git clone "$REPOSITORY_URL" "$APP_DIR"
    validate_repository_ownership
    ok "Repository cloned."
  fi

  load_project_config
  local current_remote
  current_remote=$(run_as_deploy_git remote get-url origin 2>/dev/null || true)
  if [[ "$current_remote" != "$REPOSITORY_URL" ]]; then
    warn "origin policy mismatch: checkout uses '$current_remote'; configured production remote is '$REPOSITORY_URL'."
    warn "These URLs may point to equivalent repositories, but production policy requires the configured read-only SSH alias."
    if ask_yes_no "Set origin to the configured production remote?" yes; then
      run_as_deploy_git remote set-url origin "$REPOSITORY_URL"; ok "origin updated."
    else
      return 1
    fi
  fi

  local dirty
  dirty=$(run_as_deploy_git status --porcelain)
  if [[ -n "$dirty" ]]; then
    error "Production checkout has local changes; nothing will be overwritten:"
    printf '%s\n' "$dirty" >&2
    warn "Reconcile via normal Git workflow. Do not use reset --hard/clean merely to make deployment pass."
    return 1
  fi
  run_as_deploy_git fetch --prune origin
  run_as_deploy_git fetch --tags origin
  ok "Repository access, ownership, and clean checkout verified."
  normalize_checkout_permissions
}

ansible_dir() { printf '%s/deploy/ansible' "$APP_DIR"; }

ansible_exec() {
  env "ANSIBLE_COLLECTIONS_PATH=$ANSIBLE_COLLECTIONS_DIR" "$@"
}

ensure_ansible_controller() {
  step "Local Ansible controller"
  local dir stamp dependency_hash cached_hash collection_dir
  dir=$(ansible_dir)
  stamp="$RUNTIME_DIR/ansible-dependencies.sha256"
  collection_dir="$ANSIBLE_COLLECTIONS_DIR/ansible_collections/community/docker"
  [[ -f "$dir/requirements.txt" ]] || { error "Missing $dir/requirements.txt"; return 1; }
  [[ -f "$dir/requirements.yml" ]] || { error "Missing $dir/requirements.yml"; return 1; }

  if [[ $EUID -eq 0 ]]; then
    install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$ANSIBLE_VENV" "$ANSIBLE_COLLECTIONS_DIR" "$RUNTIME_DIR"
  fi

  dependency_hash=$(cat "$dir/requirements.txt" "$dir/requirements.yml" | sha256sum | awk '{print $1}')
  cached_hash=$(cat "$stamp" 2>/dev/null || true)

  if [[ ! -x "$ANSIBLE_VENV/bin/ansible-playbook" ]]; then
    info "Creating Ansible venv outside the production checkout: $ANSIBLE_VENV"
    run_as_deploy python3 -m venv "$ANSIBLE_VENV"
    cached_hash=""
  fi

  if [[ "$cached_hash" != "$dependency_hash" || ! -d "$collection_dir" ]]; then
    info "Ansible dependency definition changed or cache is missing; syncing controller dependencies."
    configure_dependency_network

    if ! run_dependency_logged "$ANSIBLE_VENV/bin/python" -m pip install \
      --upgrade pip \
      --index-url "$PYPI_INDEX_URL" \
      --timeout "$PIP_TIMEOUT" \
      --retries "$PIP_RETRIES" \
      --disable-pip-version-check; then
      diagnose_dependency_failure "$RUN_LOG"
      error "Failed to bootstrap/upgrade pip for the local Ansible controller."
      return 1
    fi

    if ! run_dependency_logged "$ANSIBLE_VENV/bin/python" -m pip install \
      -r "$dir/requirements.txt" \
      --index-url "$PYPI_INDEX_URL" \
      --timeout "$PIP_TIMEOUT" \
      --retries "$PIP_RETRIES" \
      --disable-pip-version-check; then
      diagnose_dependency_failure "$RUN_LOG"
      error "Failed to install Ansible controller Python requirements."
      return 1
    fi

    if ! run_dependency_logged env \
      "ANSIBLE_COLLECTIONS_PATH=$ANSIBLE_COLLECTIONS_DIR" \
      "$ANSIBLE_VENV/bin/ansible-galaxy" collection install \
      -p "$ANSIBLE_COLLECTIONS_DIR" -r "$dir/requirements.yml"; then
      diagnose_dependency_failure "$RUN_LOG"
      error "Failed to install Ansible Galaxy collections."
      return 1
    fi

    printf '%s\n' "$dependency_hash" > "$stamp"
    chmod 0644 "$stamp"
    [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$stamp"
  else
    ok "Ansible controller dependencies are already synchronized; skipping network installs."
  fi

  ok "$($ANSIBLE_VENV/bin/ansible --version | head -n1)"
}

ensure_local_inventory() {
  step "Local production inventory"
  [[ $EUID -eq 0 ]] && install -d -m 0750 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$RUNTIME_DIR"
  cat > "$INVENTORY_FILE" <<EOF_INVENTORY
---
all:
  children:
    production:
      hosts:
        sahabino-production:
          ansible_connection: local
          ansible_python_interpreter: $ANSIBLE_VENV/bin/python
EOF_INVENTORY
  chmod 0600 "$INVENTORY_FILE"; [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$INVENTORY_FILE"
  [[ "$INVENTORY_FILE" != "$APP_DIR"/* ]] || { error "Generated inventory must be outside the Git checkout."; return 1; }
  ok "Generated runtime inventory outside the checkout: $INVENTORY_FILE"
}

validate_secret() {
  local name="$1" value="$2" regex="$3"
  [[ "$value" =~ $regex ]] || { warn "$name does not satisfy the production-safe format."; return 1; }
}

random_hex() { openssl rand -hex "$1"; }

secret_or_generate() {
  local label="$1" bytes="$2" regex="$3" choice value
  while true; do
    if (( ASSUME_YES )); then
      value=$(random_hex "$bytes")
    else
      printf '%s\n  1) Generate securely (not displayed)\n  2) Enter my own value\n' "$label:" >&2
      read -r -p "Choose [1]: " choice >&2; choice="${choice:-1}"
      case "$choice" in
        1) value=$(random_hex "$bytes"); info "$label generated securely; it will be written only to the encrypted Vault." >&2 ;;
        2) read -r -s -p "Enter $label: " value >&2; printf '\n' >&2 ;;
        *) warn "Choose 1 or 2."; continue ;;
      esac
    fi
    if [[ "$value" =~ $regex ]]; then printf '%s' "$value"; return 0; fi
    warn "$label must match: $regex"
  done
}

validate_vault_password_file() {
  local file="$1" real mode owner_uid deploy_uid
  [[ -f "$file" && -r "$file" ]] || { error "Vault password file is not a readable regular file: $file"; return 1; }
  [[ ! -L "$file" ]] || { error "Vault password file must not be a symlink: $file"; return 1; }
  real=$(readlink -f "$file")
  [[ "$real" != "$APP_DIR" && "$real" != "$APP_DIR"/* ]] || { error "Vault password file must stay outside the Git checkout."; return 1; }
  mode=$(stat -c '%a' "$real")
  if (( (8#$mode & 07077) != 0 )); then
    error "Vault password file permissions are unsafe ($mode). Require owner-only access without special bits (for example 0600)."; return 1
  fi
  owner_uid=$(stat -c '%u' "$real"); deploy_uid=$(id -u "$DEPLOY_USER")
  if [[ "$owner_uid" != "0" && "$owner_uid" != "$deploy_uid" && "$owner_uid" != "$EUID" ]]; then
    error "Vault password file owner uid $owner_uid is neither root nor '$DEPLOY_USER'."; return 1
  fi
  [[ -s "$real" ]] || { error "Vault password file is empty."; return 1; }
}

persist_vault_password_value() {
  local value="$1" dir tmp
  [[ $EUID -eq 0 ]] || { error "Persisting the Vault password requires root."; return 1; }
  dir=$(dirname -- "$VAULT_PASSWORD_STORE")
  install -d -m 0750 -o root -g root "$dir"
  [[ ! -L "$VAULT_PASSWORD_STORE" ]] || { error "Refusing to overwrite symlink Vault password store: $VAULT_PASSWORD_STORE"; return 1; }
  tmp=$(mktemp "$dir/.ansible-vault-password.XXXXXX")
  chmod 0600 "$tmp"
  chown root:root "$tmp"
  printf '%s\n' "$value" > "$tmp"
  mv -f -- "$tmp" "$VAULT_PASSWORD_STORE"
  chmod a-s,u=rw,go= "$VAULT_PASSWORD_STORE"
  chown root:root "$VAULT_PASSWORD_STORE"
  VAULT_PASSWORD_FILE="$VAULT_PASSWORD_STORE"
  ok "Vault password stored root-only at $VAULT_PASSWORD_STORE for rerun-safe deployments."
}

prepare_vault_password() {
  local dir="$1" existing_vault="$2" vault="$dir/group_vars/production/vault.yml"

  # An explicit password file always wins and is never copied implicitly.
  if [[ -n "${SAHABINO_VAULT_PASSWORD_FILE:-}" ]]; then
    validate_vault_password_file "$SAHABINO_VAULT_PASSWORD_FILE"
    VAULT_PASSWORD_FILE=$(readlink -f "$SAHABINO_VAULT_PASSWORD_FILE")
    return 0
  fi

  # Environment-provided passwords remain ephemeral by design; CI/automation
  # callers may not want them persisted to disk beyond this invocation.
  if [[ -n "${SAHABINO_VAULT_PASSWORD:-}" ]]; then
    VAULT_PASSWORD_FILE=$(mktemp /tmp/sahabino-vault-pass.XXXXXX)
    chmod 0600 "$VAULT_PASSWORD_FILE"
    printf '%s\n' "$SAHABINO_VAULT_PASSWORD" > "$VAULT_PASSWORD_FILE"
    unset SAHABINO_VAULT_PASSWORD
    return 0
  fi

  # Prefer the assistant-managed root-only store on reruns. This avoids asking
  # an operator for the same production Vault password on every deployment.
  if [[ -f "$VAULT_PASSWORD_STORE" ]]; then
    validate_vault_password_file "$VAULT_PASSWORD_STORE"
    if [[ "$existing_vault" == "yes" ]] && ! ansible_exec "$ANSIBLE_VENV/bin/ansible-vault" view \
        --vault-password-file "$VAULT_PASSWORD_STORE" "$vault" >/dev/null 2>&1; then
      error "Stored Vault password does not unlock the existing production Vault: $VAULT_PASSWORD_STORE"
      warn "Do not delete the encrypted Vault. Supply the correct password with SAHABINO_VAULT_PASSWORD_FILE or repair the root-only store deliberately."
      return 1
    fi
    VAULT_PASSWORD_FILE="$VAULT_PASSWORD_STORE"
    ok "Using persisted root-only Vault password file: $VAULT_PASSWORD_STORE"
    return 0
  fi

  local pass pass2 choice
  if [[ "$existing_vault" == "yes" ]]; then
    if (( ASSUME_YES )); then
      error "Existing Vault has no persisted password. Supply SAHABINO_VAULT_PASSWORD or SAHABINO_VAULT_PASSWORD_FILE for --yes mode."
      return 1
    fi
    while true; do
      read -r -s -p "Ansible Vault password: " pass; printf '\n'
      [[ -n "$pass" ]] || { warn "Vault password cannot be empty."; continue; }
      local probe
      probe=$(mktemp /tmp/sahabino-vault-pass.XXXXXX); chmod 0600 "$probe"
      printf '%s\n' "$pass" > "$probe"
      if ansible_exec "$ANSIBLE_VENV/bin/ansible-vault" view --vault-password-file "$probe" "$vault" >/dev/null 2>&1; then
        rm -f "$probe"
        persist_vault_password_value "$pass"
        unset pass
        return 0
      fi
      rm -f "$probe"
      warn "Vault password was not accepted. Try again."
    done
  fi

  if (( ASSUME_YES )); then
    error "Creating a new Vault in --yes mode requires SAHABINO_VAULT_PASSWORD or SAHABINO_VAULT_PASSWORD_FILE."
    return 1
  fi

  printf 'Ansible Vault password:\n  1) Generate securely and store root-only on this VPS\n  2) Enter my own password and store it root-only\n'
  while true; do
    read -r -p "Choose [1]: " choice; choice="${choice:-1}"
    case "$choice" in
      1)
        pass=$(random_hex 24)
        persist_vault_password_value "$pass"
        warn "Generated Vault password is hidden; back up $VAULT_PASSWORD_STORE through your normal secure server-secret backup process."
        if ask_yes_no "Reveal the generated Vault password ONCE as an optional external backup?" no; then
          printf '%sGenerated Vault password (one-time reveal):%s %s\n' "$C_BOLD" "$C_RESET" "$pass" >&2
        fi
        unset pass
        return 0
        ;;
      2)
        while true; do
          read -r -s -p "New Vault password: " pass; printf '\n'
          read -r -s -p "Repeat Vault password: " pass2; printf '\n'
          [[ -n "$pass" ]] || { warn "Vault password cannot be empty."; continue; }
          [[ "$pass" == "$pass2" ]] || { warn "Passwords do not match."; continue; }
          break
        done
        persist_vault_password_value "$pass"
        unset pass pass2
        return 0
        ;;
      *) warn "Choose 1 or 2." ;;
    esac
  done
}

assert_vault_git_policy() {
  local vault_rel='deploy/ansible/group_vars/production/vault.yml'
  run_as_deploy_git check-ignore -q "$vault_rel" || { error "$vault_rel is not ignored by Git. Refusing to use it."; return 1; }
  if run_as_deploy_git ls-files --error-unmatch "$vault_rel" >/dev/null 2>&1; then
    error "$vault_rel is tracked by Git. Production secrets must never be tracked."; return 1
  fi
}

ensure_vault() {
  step "Production Ansible Vault"
  local dir vault example tmp_plain existing
  dir=$(ansible_dir); vault="$dir/group_vars/production/vault.yml"; example="$dir/group_vars/production/vault.yml.example"
  [[ -f "$example" ]] || { error "Missing $example"; return 1; }
  assert_vault_git_policy

  if [[ -f "$vault" ]]; then
    head -n1 "$vault" | grep -q '^\$ANSIBLE_VAULT;' || { error "$vault exists but is not Ansible-Vault encrypted."; return 1; }
    chmod a-s,u=rw,go= "$vault"; [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$vault"
    prepare_vault_password "$dir" yes
    ansible_exec "$ANSIBLE_VENV/bin/ansible-vault" view --vault-password-file "$VAULT_PASSWORD_FILE" "$vault" >/dev/null
    ok "Existing encrypted and Git-ignored production Vault verified."; return 0
  fi

  existing="no"; prepare_vault_password "$dir" "$existing"
  local pg_db pg_user pg_password grafana_user grafana_password storage_access storage_secret proxy_input proxy_json
  pg_db=$(prompt_default "PostgreSQL database name" "sahabino")
  while ! validate_secret "PostgreSQL database" "$pg_db" '^[A-Za-z0-9_.-]+$'; do pg_db=$(prompt_default "PostgreSQL database name" "sahabino"); done
  pg_user=$(prompt_default "PostgreSQL user" "sahabino")
  while ! validate_secret "PostgreSQL user" "$pg_user" '^[A-Za-z0-9_.-]+$'; do pg_user=$(prompt_default "PostgreSQL user" "sahabino"); done
  pg_password=$(secret_or_generate "PostgreSQL password" 24 '^[A-Za-z0-9_.-]{20,}$')
  grafana_user=$(prompt_default "Grafana admin user" "admin")
  while ! validate_secret "Grafana admin user" "$grafana_user" '^[A-Za-z0-9_.@-]+$'; do grafana_user=$(prompt_default "Grafana admin user" "admin"); done
  grafana_password=$(secret_or_generate "Grafana admin password" 24 '^[A-Za-z0-9_.-]{20,}$')
  storage_access=$(secret_or_generate "SeaweedFS access key" 12 '^[A-Za-z0-9_.-]{12,}$')
  storage_secret=$(secret_or_generate "SeaweedFS secret key" 24 '^[A-Za-z0-9_.-]{20,}$')
  if (( ASSUME_YES )); then proxy_json='[]'; else read -r -p "Play Store proxy URLs as comma-separated values (Enter for none): " proxy_input; proxy_json=$(python3 -c 'import json,sys; print(json.dumps([x.strip() for x in sys.argv[1].split(",") if x.strip()]))' "$proxy_input"); fi

  tmp_plain=$(mktemp /tmp/sahabino-vault-plain.XXXXXX); VAULT_PLAINTEXT_TEMP="$tmp_plain"; chmod 0600 "$tmp_plain"
  # Use PyYAML safe_dump instead of interpolating strings into YAML. Secrets are
  # passed only to the short-lived serializer process and are never printed.
  VAULT_OUT="$tmp_plain" \
  V_PG_DB="$pg_db" V_PG_USER="$pg_user" V_PG_PASSWORD="$pg_password" \
  V_GRAFANA_USER="$grafana_user" V_GRAFANA_PASSWORD="$grafana_password" \
  V_STORAGE_ACCESS="$storage_access" V_STORAGE_SECRET="$storage_secret" \
  V_PROXY_JSON="$proxy_json" \
  "$ANSIBLE_VENV/bin/python" -c '
import json, os, yaml
obj = {
  "vault_sahabino_postgres_db": os.environ["V_PG_DB"],
  "vault_sahabino_postgres_user": os.environ["V_PG_USER"],
  "vault_sahabino_postgres_password": os.environ["V_PG_PASSWORD"],
  "vault_sahabino_grafana_admin_user": os.environ["V_GRAFANA_USER"],
  "vault_sahabino_grafana_admin_password": os.environ["V_GRAFANA_PASSWORD"],
  "vault_sahabino_object_storage_access_key": os.environ["V_STORAGE_ACCESS"],
  "vault_sahabino_object_storage_secret_key": os.environ["V_STORAGE_SECRET"],
  "vault_sahabino_playstore_proxy_urls": json.loads(os.environ["V_PROXY_JSON"]),
}
with open(os.environ["VAULT_OUT"], "w", encoding="utf-8") as f:
    yaml.safe_dump(obj, f, sort_keys=False, default_flow_style=False)
'
  unset pg_password grafana_password storage_access storage_secret

  if ! ansible_exec "$ANSIBLE_VENV/bin/ansible-vault" encrypt --vault-password-file "$VAULT_PASSWORD_FILE" "$tmp_plain" >/dev/null; then
    rm -f "$tmp_plain"; VAULT_PLAINTEXT_TEMP=""; error "Failed to encrypt new Vault; plaintext temp was removed."; return 1
  fi
  mv "$tmp_plain" "$vault"; VAULT_PLAINTEXT_TEMP=""; chmod a-s,u=rw,go= "$vault"; [[ $EUID -eq 0 ]] && chown "$DEPLOY_USER:$DEPLOY_GROUP" "$vault"
  assert_vault_git_policy
  ok "Encrypted production Vault created. Generated service secrets were not printed."
  info "Grafana admin username: $grafana_user"
}

ensure_vault_password_for_existing() {
  local dir vault; dir=$(ansible_dir); vault="$dir/group_vars/production/vault.yml"
  [[ -f "$vault" ]] || { error "Production Vault does not exist."; return 1; }
  assert_vault_git_policy
  [[ -n "$VAULT_PASSWORD_FILE" ]] || prepare_vault_password "$dir" yes
}

remote_default_revision() {
  local ref
  ref=$(run_as_deploy_git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || true)
  if [[ -z "$ref" ]]; then
    ref=$(run_as_deploy_git ls-remote --symref origin HEAD 2>/dev/null | awk '/^ref:/ {sub("refs/heads/", "origin/", $2); print $2; exit}')
  fi
  printf '%s' "$ref"
}

resolve_revision() {
  step "Deployment revision"
  local input resolved default_revision
  run_as_deploy_git fetch --prune origin; run_as_deploy_git fetch --tags origin
  default_revision=$(remote_default_revision)
  if [[ -z "$TARGET_REVISION" ]]; then
    if [[ -n "$default_revision" ]]; then
      if (( ASSUME_YES )); then input="$default_revision"; else input=$(prompt_default "Revision to deploy (branch/tag/SHA)" "$default_revision"); fi
    else
      if (( ASSUME_YES )); then
        error "Remote default branch could not be discovered. Supply --revision explicitly."; return 1
      fi
      while [[ -z "${input:-}" ]]; do read -r -p "Revision to deploy (branch/tag/SHA): " input; done
    fi
  else input="$TARGET_REVISION"; fi
  resolved=$(run_as_deploy_git rev-parse --verify "${input}^{commit}" 2>/dev/null || true)
  if [[ -z "$resolved" && "$input" != origin/* ]]; then resolved=$(run_as_deploy_git rev-parse --verify "origin/${input}^{commit}" 2>/dev/null || true); fi
  [[ -n "$resolved" ]] || { error "Cannot resolve revision '$input' to a commit."; return 1; }
  TARGET_REVISION="$resolved"; info "Resolved '$input' -> $TARGET_REVISION"
}

run_ansible_syntax_check() {
  step "Ansible syntax validation"
  local dir; dir=$(ansible_dir)
  (
    umask "$WORKTREE_UMASK"
    cd "$dir"
    ansible_exec "$ANSIBLE_VENV/bin/ansible-playbook" -i "$INVENTORY_FILE" --vault-password-file "$VAULT_PASSWORD_FILE" --syntax-check provision.yml >/dev/null
    ansible_exec "$ANSIBLE_VENV/bin/ansible-playbook" -i "$INVENTORY_FILE" --vault-password-file "$VAULT_PASSWORD_FILE" --syntax-check deploy.yml >/dev/null
  )
  ok "Provision and deploy playbooks pass syntax-check."
}

run_provision() {
  step "Idempotent host provisioning"
  local dir; dir=$(ansible_dir); ensure_vault_password_for_existing
  (
    umask "$WORKTREE_UMASK"
    cd "$dir"
    run_logged env "ANSIBLE_COLLECTIONS_PATH=$ANSIBLE_COLLECTIONS_DIR" "$ANSIBLE_VENV/bin/ansible-playbook" -i "$INVENTORY_FILE" provision.yml --vault-password-file "$VAULT_PASSWORD_FILE"
  ) || { diagnose_failure "$RUN_LOG" provision; return 1; }
  ok "Provisioning completed."
}

confirm_api_exposure() {
  [[ "$API_BIND_ADDRESS" == "0.0.0.0" || "$API_BIND_ADDRESS" == "::" ]] || return 0
  warn "Production API is configured to bind publicly on $API_BIND_ADDRESS:$API_PORT."
  warn "This assistant does not configure TLS, reverse proxy, firewall, or VPN policy."
  if [[ "$CONFIRM_PUBLIC_API" == "1" ]]; then ok "Public API exposure explicitly acknowledged."; return 0; fi
  if (( ASSUME_YES )); then
    error "Public API exposure requires --confirm-public-api or SAHABINO_CONFIRM_PUBLIC_API=1 in non-interactive mode."; return 1
  fi
  ask_yes_no "Continue with the API publicly exposed on port $API_PORT?" no || { error "Deployment cancelled before service changes."; return 1; }
  CONFIRM_PUBLIC_API=1
}

run_deploy() {
  local dir deploy_log rc
  dir=$(ansible_dir)
  ensure_vault_password_for_existing
  resolve_revision
  confirm_api_exposure
  # resolve_revision has its own stage; reset it here so failures from the
  # playbook are attributed to the actual deployment rather than revision lookup.
  step "Production deployment"
  deploy_log="$LOG_ROOT/deploy-$RUN_ID.log"; [[ $EUID -eq 0 ]] || deploy_log="/tmp/sahabino-deploy-playbook-$RUN_ID.log"
  touch "$deploy_log"; chmod 0600 "$deploy_log"
  set +e
  (
    umask "$WORKTREE_UMASK"
    cd "$dir"
    env "ANSIBLE_COLLECTIONS_PATH=$ANSIBLE_COLLECTIONS_DIR" "$ANSIBLE_VENV/bin/ansible-playbook" \
      -i "$INVENTORY_FILE" deploy.yml --vault-password-file "$VAULT_PASSWORD_FILE" -e "sahabino_deploy_revision=$TARGET_REVISION"
  ) 2>&1 | tee -a "$RUN_LOG" "$deploy_log"
  rc=${PIPESTATUS[0]}; set -e
  if (( rc != 0 )); then diagnose_failure "$deploy_log" deploy; return "$rc"; fi
  ok "Ansible deployment completed successfully."
}

diagnose_failure() {
  local logfile="$1" phase="${2:-deployment}"
  printf '\n%sAutomatic failure diagnosis (%s)%s\n' "$C_BOLD" "$phase" "$C_RESET" >&2
  if grep -qiE 'One or more requested Sahabino production services are not running|requested production services are not running' "$logfile"; then
    warn "Final Compose service verification failed. Showing the actual missing service(s) and logs."
    show_service_diagnostics || true
  elif grep -qiE 'permission denied.*(loki-config|config\.alloy|grafana|seaweedfs)|open .*permission denied' "$logfile"; then
    warn "A container-mounted configuration path is not readable. The assistant now normalizes tracked worktree modes and validates every relative Compose bind source before deployment."
    [[ -d "$APP_DIR/.git" ]] && normalize_checkout_permissions || true
  elif grep -qiE 'server checkout has local changes|Refuse to overwrite server-side repository changes' "$logfile"; then
    warn "Git safety guard stopped deployment because checkout is dirty."; run_as_deploy_git status --short >&2 || true
  elif grep -qi '/var/lib/snapd/void/.env' "$logfile"; then
    warn "Docker Snap confinement is hiding /opt. Use official Docker CE; do not patch Ansible chdir paths."
  elif grep -qiE 'Failed to download.*jmespath|HTTP status client error \(404 Not Found\).*pythonhosted|uv sync --frozen' "$logfile"; then
    warn "Frozen uv dependency build failed. Refresh only the affected lock artifact on a development machine, test, commit/push, then deploy the new SHA."
  elif grep -qiE 'could not decrypt|Decryption failed|vault password' "$logfile"; then
    warn "Ansible Vault password/encrypted Vault could not be read."
  elif grep -qiE 'Permission denied \(publickey\)|Could not read from remote repository|git ls-remote' "$logfile"; then
    warn "Git/SSH access failed. Review the actual SSH/Git stderr earlier in $RUN_LOG and verify deploy-key fingerprint/alias/repository URL."
  elif grep -qiE 'No space left on device|ENOSPC' "$logfile"; then
    warn "Disk is full/nearly full. Inspect df -h and project storage before retrying."
  elif grep -qiE 'Killed|out of memory|oom-kill|OOMKilled' "$logfile"; then
    warn "A process appears OOM-killed. Inspect free -h, swap, project docker stats, and kernel logs."
  elif grep -qiE 'backup.*failed|pg_dump.*failed|Require a verified backup' "$logfile"; then
    warn "Pre-deploy backup failed. Deployment correctly stopped before migration; do not bypass it."
  elif grep -qiE 'port is already allocated|address already in use' "$logfile"; then
    warn "A required host port is occupied. Use ss -lntp and inspect project labels before retrying."
  elif grep -qiE 'unhealthy|health check|wait_timeout' "$logfile"; then
    warn "A service did not become healthy in time. Inspect Compose service health/logs; Kafka can be transiently starting during first boot."
  else
    warn "No known signature matched. Review the final failed TASK and the preserved log; no destructive recovery is applied automatically."
  fi
}

compose() {
  (
    cd "$APP_DIR"
    docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file .env \
      --file docker-compose.yml --file compose.prod.yml --profile "$COMPOSE_PROFILE" "$@"
  )
}

compose_expected_services() {
  compose config --services 2>/dev/null | sed '/^$/d' | sort -u
}

compose_running_services() {
  compose ps --services --filter status=running 2>/dev/null | sed '/^$/d' | sort -u
}

compose_missing_services() {
  comm -23 <(compose_expected_services) <(compose_running_services)
}

show_service_diagnostics() {
  local -a missing=()
  mapfile -t missing < <(compose_missing_services 2>/dev/null || true)
  if (( ${#missing[@]} == 0 )); then
    return 0
  fi

  error "Missing/non-running production services: ${missing[*]}"
  local service
  for service in "${missing[@]}"; do
    printf '\n%s--- docker compose ps %s ---%s\n' "$C_BOLD" "$service" "$C_RESET" >&2
    compose ps -a "$service" >&2 || true
    printf '%s--- last 120 log lines: %s ---%s\n' "$C_BOLD" "$service" "$C_RESET" >&2
    compose logs --tail=120 "$service" >&2 || true
  done
  return 1
}

wait_http_ready() {
  local label="$1" url="$2" timeout_seconds="${3:-90}" delay="${4:-2}"
  local deadline=$((SECONDS + timeout_seconds)) body_file status
  body_file=$(mktemp /tmp/sahabino-http-ready.XXXXXX)
  while (( SECONDS < deadline )); do
    status=$(curl -sS -o "$body_file" -w '%{http_code}' --connect-timeout 3 --max-time 8 "$url" 2>/dev/null || true)
    if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
      rm -f "$body_file"
      ok "$label readiness passed: $url ($status)"
      return 0
    fi
    sleep "$delay"
  done
  warn "$label readiness did not become successful within ${timeout_seconds}s: $url (last HTTP ${status:-none})."
  if [[ -s "$body_file" ]]; then
    warn "$label last response: $(tr '\n' ' ' < "$body_file" | head -c 300)"
  fi
  rm -f "$body_file"
  return 1
}

wait_for_kafka_health() {
  local cid deadline status
  cid=$(compose ps -q kafka 2>/dev/null | head -n1 || true)
  [[ -n "$cid" ]] || { warn "Kafka container is not present."; return 1; }
  deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    status=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null || true)
    case "$status" in
      healthy) ok "Kafka container health: healthy"; return 0 ;;
      unhealthy|starting) sleep 5 ;;
      *) warn "Kafka container has no health status: $status"; return 0 ;;
    esac
  done
  warn "Kafka did not report healthy within 120s. Last health state:"
  docker inspect --format '{{json .State.Health}}' "$cid" 2>/dev/null || true
  return 1
}

require_private_file_mode() {
  local file="$1" label="$2" mode
  [[ -e "$file" ]] || return 0
  [[ -f "$file" && ! -L "$file" ]] || { error "$label must be a non-symlink regular file: $file"; return 1; }
  mode=$(stat -c '%a' "$file")
  if (( (8#$mode & 07077) != 0 )); then
    error "$label permissions are unsafe: $file (mode $mode; expected owner-only without special bits, e.g. 0600)."
    return 1
  fi
}

audit_sensitive_permissions() {
  local failed=0 home key config known_hosts vault
  home=$(deploy_home)
  key="$home/.ssh/$DEPLOY_KEY_BASENAME"
  config="$home/.ssh/config"
  known_hosts="$home/.ssh/known_hosts"
  vault="$APP_DIR/deploy/ansible/group_vars/production/vault.yml"

  require_private_file_mode "$APP_DIR/.env" "Production .env" || failed=1
  require_private_file_mode "$vault" "Encrypted production Vault" || failed=1
  require_private_file_mode "$VAULT_PASSWORD_STORE" "Persisted Vault password" || failed=1
  require_private_file_mode "$key" "GitHub Deploy Key private key" || failed=1
  require_private_file_mode "$config" "Deploy-user SSH config" || failed=1
  require_private_file_mode "$INVENTORY_FILE" "Runtime Ansible inventory" || failed=1

  if [[ -f "$known_hosts" ]]; then
    local kh_mode
    kh_mode=$(stat -c '%a' "$known_hosts")
    if (( (8#$kh_mode & 022) != 0 )); then
      error "SSH known_hosts is writable by group/others: $known_hosts (mode $kh_mode)."
      failed=1
    fi
  fi
  (( failed == 0 )) && ok "Sensitive file permission audit passed."
  (( failed == 0 ))
}

verify_project_resources() {
  local -a ids=()
  mapfile -t ids < <(compose ps -q | sed '/^$/d')
  if (( ${#ids[@]} )); then docker stats --no-stream "${ids[@]}" || true; else warn "No project container IDs found for resource stats."; fi
}

verify_deployment() {
  step "Post-deploy verification"
  [[ -d "$APP_DIR" ]] || { error "$APP_DIR does not exist."; return 1; }
  [[ -f "$APP_DIR/.env" ]] || { error "$APP_DIR/.env does not exist."; return 1; }
  [[ -d "$APP_DIR/.git" ]] && load_project_config

  # A deployment/check-out may have introduced new tracked paths. Re-apply the
  # index-derived worktree policy before validating containers. In verify-only
  # mode a non-root operator may lack permission to repair, so validate instead.
  if [[ $EUID -eq 0 || "$(id -un)" == "$DEPLOY_USER" ]]; then
    normalize_checkout_permissions
  else
    validate_container_bind_permissions || { error "Container bind permission validation failed; re-run --verify as root to repair tracked modes."; return 1; }
  fi
  audit_sensitive_permissions || return 1

  compose ps
  show_service_diagnostics || return 1
  wait_for_kafka_health || { error "Kafka did not become healthy during post-deploy verification."; return 1; }

  wait_http_ready "API" "http://127.0.0.1:$API_PORT/docs" 60 2 || {
    compose logs --tail=120 api >&2 || true
    return 1
  }

  # Loki intentionally returns HTTP 503 for a short ingester warm-up window;
  # retry readiness instead of treating the first response as a failure.
  wait_http_ready "Loki" "http://127.0.0.1:$LOKI_PORT/ready" 90 2 || {
    compose logs --tail=160 loki >&2 || true
    return 1
  }

  # Grafana exposes a stable API health endpoint. Alloy is validated by the
  # running-service assertion above because its HTTP endpoint contract is not
  # needed by the application and may vary independently of log shipping.
  wait_http_ready "Grafana" "http://127.0.0.1:$GRAFANA_PORT/api/health" 60 2 || {
    compose logs --tail=120 grafana >&2 || true
    return 1
  }

  info "Database pipeline counts:"
  compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT
  (SELECT COUNT(*) FROM applications) AS applications,
  (SELECT COUNT(*) FROM crawl_runs) AS crawl_runs,
  (SELECT COUNT(*) FROM crawl_tasks) AS crawl_tasks,
  (SELECT COUNT(*) FROM ingested_events) AS ingested_events,
  (SELECT COUNT(*) FROM playstore_app_snapshots) AS app_snapshots,
  (SELECT COUNT(*) FROM reviews) AS reviews,
  (SELECT COUNT(*) FROM review_observations) AS review_observations;
"' || warn "Database count verification failed."

  info "Non-succeeded crawler tasks:"
  compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT a.package_name, ct.task_type, ct.status, ct.error_code
FROM crawl_tasks ct JOIN applications a ON a.id = ct.application_id
WHERE ct.status <> '\''succeeded'\'' ORDER BY a.package_name, ct.task_type;
"' || warn "Crawler task verification query failed."

  info "Kafka ingestion consumer lag (20s timeout):"
  compose exec -T kafka timeout 20s /opt/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server "$KAFKA_BOOTSTRAP_INTERNAL" --describe --group "$INGESTION_CONSUMER_GROUP" \
    || warn "Consumer-group describe timed out/failed."

  # Recent log markers are informational only; health/readiness above is the
  # verification source of truth so an idle healthy system does not fail.
  info "Recent crawler scheduler markers (informational):"
  compose logs --tail=250 crawler 2>&1 | grep -E 'crawler\.scheduler\.started|crawler\.run\.(started|completed)|next run at' | tail -n 12 || info "No recent scheduler marker in the tail; this is not a health failure."
  info "Recent ingestion markers (informational):"
  compose logs --tail=80 ingestion 2>&1 | grep 'ingestion.message.processed' | tail -n 5 || info "No recent ingestion marker in the tail; this is not a health failure."

  info "Sahabino project container resource snapshot:"
  verify_project_resources

  [[ -f "$APP_PARENT/DEPLOYED_REVISION" ]] && { printf 'Deployed revision: '; cat "$APP_PARENT/DEPLOYED_REVISION"; }
  print_access_hints
}

print_access_hints() {
  cat <<EOF_ACCESS

${C_BOLD}Private observability access${C_RESET}
Grafana, Loki and Alloy stay loopback-only on the VPS. From your workstation:

  ssh -N \\
    -L $GRAFANA_PORT:127.0.0.1:$GRAFANA_PORT \\
    -L $LOKI_PORT:127.0.0.1:$LOKI_PORT \\
    -L $ALLOY_PORT:127.0.0.1:$ALLOY_PORT \\
    $DEPLOY_USER@SERVER_IP

Then open:
  Grafana: http://127.0.0.1:$GRAFANA_PORT
  Loki:    http://127.0.0.1:$LOKI_PORT
  Alloy:   http://127.0.0.1:$ALLOY_PORT

API exposure currently configured as: $API_BIND_ADDRESS:$API_PORT
The assistant does not configure TLS/reverse-proxy/firewall policy.
EOF_ACCESS
}

main() {
  setup_logging
  require_root_for_bootstrap

  if [[ "$MODE" == "verify" ]]; then
    ensure_docker
    [[ -d "$APP_DIR/.git" ]] && load_project_config || true
    verify_deployment
    exit 0
  fi

  ubuntu_preflight
  ensure_base_packages
  ensure_deploy_user
  discover_repository_url
  ensure_docker
  ensure_deploy_key_and_alias
  info "Deploy-key verification returned successfully; proceeding to production checkout."
  ensure_repository
  # Reload config from the checked-out source of truth before project-specific
  # checks and deployment.
  load_project_config
  check_reserved_ports
  ensure_ansible_controller
  ensure_local_inventory
  ensure_vault
  run_ansible_syntax_check

  if [[ "$MODE" == "check" ]]; then ok "Preflight completed. No provision/deploy action requested."; exit 0; fi
  run_provision
  if [[ "$MODE" == "provision" ]]; then ok "Provision-only run completed."; exit 0; fi
  run_deploy
  verify_deployment
  ok "Sahabino production deployment flow completed."
  if [[ -f /var/run/reboot-required ]]; then
    warn "Host reboot is still pending. Schedule a controlled reboot and run --verify afterward."
  fi
  return 0
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
