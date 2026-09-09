#!/usr/bin/env bash
set -Eeuo pipefail

# Production-oriented launcher for the Docker Compose stack. It deliberately
# keeps persistent runtime secrets outside the Git checkout and maps the four
# legacy cpp_markup.env variables used by the previous deployment.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="${EDUPROG_PROJECT_DIR:-${CONTOUR_PROJECT_DIR:-$SCRIPT_DIR}}"
COMPOSE_FILE="${EDUPROG_COMPOSE_FILE:-${CONTOUR_COMPOSE_FILE:-$PROJECT_DIR/compose.yml}}"
DEFAULT_EDUPROG_RUNTIME_ENV="${HOME:+$HOME/.config/eduprog/runtime.env}"
DEFAULT_EDUPROG_HOST_ENV="${HOME:+$HOME/cpp_markup.env}"

if [[ -n "${EDUPROG_HOST_ENV_FILE:-}" ]]; then
  HOST_ENV_FILE="$EDUPROG_HOST_ENV_FILE"
elif [[ -n "${HOME:-}" ]]; then
  HOST_ENV_FILE="$DEFAULT_EDUPROG_HOST_ENV"
else
  HOST_ENV_FILE=""
fi

if [[ -n "${EDUPROG_ENV_FILE:-}" ]]; then
  RUNTIME_ENV_FILE="$EDUPROG_ENV_FILE"
elif [[ -n "${CONTOUR_ENV_FILE:-}" ]]; then
  RUNTIME_ENV_FILE="$CONTOUR_ENV_FILE"
elif [[ -n "${HOME:-}" ]]; then
  RUNTIME_ENV_FILE="$DEFAULT_EDUPROG_RUNTIME_ENV"
else
  printf 'error: set EDUPROG_ENV_FILE when HOME is unavailable\n' >&2
  exit 1
fi

LEGACY_RUNTIME_ENV_FILE="${EDUPROG_LEGACY_ENV_FILE:-${HOME:+$HOME/.config/contour/runtime.env}}"
MIGRATE_LEGACY_RUNTIME=false
if [[ -n "$LEGACY_RUNTIME_ENV_FILE" && "$LEGACY_RUNTIME_ENV_FILE" != "$RUNTIME_ENV_FILE" ]]; then
  if [[ "$RUNTIME_ENV_FILE" == "$DEFAULT_EDUPROG_RUNTIME_ENV" \
    || -n "${EDUPROG_LEGACY_ENV_FILE:-}" ]]; then
    MIGRATE_LEGACY_RUNTIME=true
  fi
fi

log() {
  printf '[eduprog] %s\n' "$*"
}

die() {
  printf '[eduprog] error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

random_hex() {
  openssl rand -hex "$1"
}

read_host_env_value() {
  local key="$1"
  python3 - "$HOST_ENV_FILE" "$key" <<'PY'
import re
import sys

path, requested_key = sys.argv[1:]
value = None
with open(path, encoding="utf-8") as source:
    for raw_line in source:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)",
            raw_line.rstrip("\r\n"),
        )
        if match is None or match.group(1) != requested_key:
            continue
        raw_value = match.group(2).strip()
        if raw_value.startswith("'"):
            end = raw_value.find("'", 1)
            remainder = raw_value[end + 1 :].strip() if end >= 0 else ""
            if end < 0 or (remainder and not remainder.startswith("#")):
                raise SystemExit(2)
            value = raw_value[1:end]
        elif raw_value.startswith('"'):
            end = 1
            escaped = False
            decoded = []
            while end < len(raw_value):
                character = raw_value[end]
                if escaped:
                    decoded.append({"n": "\n", "r": "\r", "t": "\t"}.get(character, character))
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    break
                else:
                    decoded.append(character)
                end += 1
            remainder = raw_value[end + 1 :].strip() if end < len(raw_value) else ""
            if end >= len(raw_value) or (remainder and not remainder.startswith("#")):
                raise SystemExit(2)
            value = "".join(decoded)
        else:
            value = re.split(r"[ \t]+#", raw_value, maxsplit=1)[0].rstrip()

if value is None:
    raise SystemExit(3)
sys.stdout.write(value)
PY
}

load_host_environment() {
  [[ -n "$HOST_ENV_FILE" && -e "$HOST_ENV_FILE" ]] || return 0
  [[ -f "$HOST_ENV_FILE" ]] || die "host env is not a regular file: $HOST_ENV_FILE"

  local key value
  local allowed_keys=(
    DBLOGIN DBPASSWORD
    POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_VOLUME_NAME DATABASE_URL
    OPENROUTER_API_KEY OPENROUTER_API_BASE OPENROUTER_MODEL
    AI_ENABLED AI_BASE_URL AI_API_KEY AI_MODEL AI_API_STYLE
  )
  for key in "${allowed_keys[@]}"; do
    if [[ -n "${!key+x}" ]]; then
      continue
    fi
    if value="$(read_host_env_value "$key")"; then
      printf -v "$key" '%s' "$value"
      export "$key"
    else
      case "$?" in
        3)
          ;;
        *)
          die "could not safely parse $key from $HOST_ENV_FILE"
          ;;
      esac
    fi
  done
}

ensure_runtime_env() {
  local env_dir
  env_dir="$(dirname -- "$RUNTIME_ENV_FILE")"

  if [[ "$MIGRATE_LEGACY_RUNTIME" == "true" \
    && ! -e "$RUNTIME_ENV_FILE" \
    && -e "$LEGACY_RUNTIME_ENV_FILE" ]]; then
    [[ ! -L "$LEGACY_RUNTIME_ENV_FILE" && -f "$LEGACY_RUNTIME_ENV_FILE" ]] \
      || die "legacy runtime env is not a regular file"
    mkdir -p "$env_dir"
    chmod 700 "$env_dir"
    (
      umask 077
      cp -- "$LEGACY_RUNTIME_ENV_FILE" "$RUNTIME_ENV_FILE"
    )
    chmod 600 "$RUNTIME_ENV_FILE"
    log "copied existing runtime secrets to $RUNTIME_ENV_FILE"
  fi

  [[ ! -L "$RUNTIME_ENV_FILE" ]] || die "runtime env must not be a symbolic link"
  if [[ -e "$RUNTIME_ENV_FILE" ]]; then
    [[ -f "$RUNTIME_ENV_FILE" ]] || die "runtime env is not a regular file"
    chmod 600 "$RUNTIME_ENV_FILE"
    if ! grep -q '^MOODLE_CREDENTIAL_ENCRYPTION_KEY=' "$RUNTIME_ENV_FILE"; then
      local credential_secret
      credential_secret="$(random_hex 32)"
      printf '\nMOODLE_CREDENTIAL_ENCRYPTION_KEY=%s\n' "$credential_secret" >>"$RUNTIME_ENV_FILE"
      log "added the persistent Moodle credential encryption key"
    fi
    if ! grep -q '^MOODLE_BROWSER_SHARED_SECRET=' "$RUNTIME_ENV_FILE"; then
      local browser_secret
      browser_secret="$(random_hex 32)"
      printf '\nMOODLE_BROWSER_SHARED_SECRET=%s\n' "$browser_secret" >>"$RUNTIME_ENV_FILE"
      log "added the internal Moodle browser HMAC key"
    fi
    return
  fi

  mkdir -p "$env_dir"
  chmod 700 "$env_dir"

  local app_secret runner_secret moodle_secret moodle_credential_secret
  local moodle_browser_secret authorship_secret
  app_secret="$(random_hex 48)"
  runner_secret="$(random_hex 32)"
  moodle_secret="$(random_hex 32)"
  moodle_credential_secret="$(random_hex 32)"
  moodle_browser_secret="$(random_hex 32)"
  authorship_secret="$(random_hex 32)"

  if ! (
    umask 077
    set -o noclobber
    {
      printf 'APP_SECRET_KEY=%s\n' "$app_secret"
      printf 'RUNNER_SHARED_SECRET=%s\n' "$runner_secret"
      printf 'MOODLE_LAUNCH_SHARED_SECRET=%s\n' "$moodle_secret"
      printf 'MOODLE_CREDENTIAL_ENCRYPTION_KEY=%s\n' "$moodle_credential_secret"
      printf 'MOODLE_BROWSER_SHARED_SECRET=%s\n' "$moodle_browser_secret"
      printf 'AUTHORSHIP_PSEUDONYM_SECRET=%s\n' "$authorship_secret"
      printf 'APP_IMAGE_TAG=n150\n'
      printf 'WEB_CONCURRENCY=2\n'
      printf 'RUNNER_MAX_CONCURRENT_JOBS=2\n'
      printf 'RUNNER_CONTAINER_MEMORY=3g\n'
      printf 'RUNNER_CONTAINER_CPUS=2.0\n'
      printf 'RUNNER_WORKSPACE_TMPFS_SIZE=768m\n'
    } >"$RUNTIME_ENV_FILE"
  ); then
    [[ -f "$RUNTIME_ENV_FILE" ]] || die "could not create $RUNTIME_ENV_FILE"
  fi
  chmod 600 "$RUNTIME_ENV_FILE"
  log "created persistent runtime configuration: $RUNTIME_ENV_FILE"
}

urlencode() {
  python3 -c \
    'from urllib.parse import quote; import sys; print(quote(sys.argv[1], safe=""))' \
    "$1"
}

map_legacy_environment() {
  # PostgreSQL lives only in the Compose network. Fresh installations use
  # eduprog_postgres-data. During migration the launcher reuses the earlier
  # contour_postgres-data only while the new eduprog volume does not exist,
  # without attaching to the unrelated legacy mmcs_cpp_course database.
  export POSTGRES_DB="${POSTGRES_DB:-programming_course}"
  export POSTGRES_USER="${POSTGRES_USER:-${DBLOGIN:-}}"
  export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-${DBPASSWORD:-}}"

  [[ -n "$POSTGRES_USER" ]] || die "set POSTGRES_USER or DBLOGIN"
  [[ -n "$POSTGRES_PASSWORD" ]] || die "set POSTGRES_PASSWORD or DBPASSWORD"

  if [[ -z "${DATABASE_URL:-}" ]]; then
    local encoded_user encoded_password encoded_database
    encoded_user="$(urlencode "$POSTGRES_USER")"
    encoded_password="$(urlencode "$POSTGRES_PASSWORD")"
    encoded_database="$(urlencode "$POSTGRES_DB")"
    export DATABASE_URL="postgresql://${encoded_user}:${encoded_password}@postgres:5432/${encoded_database}"
  fi

  if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    export AI_ENABLED="${AI_ENABLED:-true}"
    export AI_BASE_URL="${AI_BASE_URL:-${OPENROUTER_API_BASE:-https://openrouter.ai/api/v1}}"
    export AI_API_KEY="${AI_API_KEY:-$OPENROUTER_API_KEY}"
    export AI_MODEL="${AI_MODEL:-${OPENROUTER_MODEL:-deepseek/deepseek-v4-pro}}"
    export AI_API_STYLE="${AI_API_STYLE:-chat_completions}"
  fi

  if [[ -z "${POSTGRES_VOLUME_NAME:-}" ]]; then
    if docker volume inspect eduprog_postgres-data >/dev/null 2>&1; then
      export POSTGRES_VOLUME_NAME=eduprog_postgres-data
    elif docker volume inspect contour_postgres-data >/dev/null 2>&1; then
      export POSTGRES_VOLUME_NAME=contour_postgres-data
      log "reusing legacy PostgreSQL volume: $POSTGRES_VOLUME_NAME"
    else
      export POSTGRES_VOLUME_NAME=eduprog_postgres-data
    fi
  fi
}

COMPOSE_ENV_ARGS=()
COMPOSE_ENV_ARGS+=(--env-file "$RUNTIME_ENV_FILE")
if [[ -n "$HOST_ENV_FILE" && -f "$HOST_ENV_FILE" && "$HOST_ENV_FILE" != "$RUNTIME_ENV_FILE" ]]; then
  COMPOSE_ENV_ARGS+=(--env-file "$HOST_ENV_FILE")
fi

COMPOSE=(
  docker compose
  --ansi never
  --project-name eduprog
  --project-directory "$PROJECT_DIR"
  "${COMPOSE_ENV_ARGS[@]}"
  -f "$COMPOSE_FILE"
)

LEGACY_COMPOSE=(
  docker compose
  --ansi never
  --project-name contour
  --project-directory "$PROJECT_DIR"
  "${COMPOSE_ENV_ARGS[@]}"
  -f "$COMPOSE_FILE"
)

compose() {
  "${COMPOSE[@]}" "$@"
}

legacy_compose() {
  "${LEGACY_COMPOSE[@]}" "$@"
}

stop_legacy_stack() {
  local running
  running="$(docker ps -q --filter label=com.docker.compose.project=contour)"
  if [[ -n "$running" ]]; then
    log "stopping the legacy contour Compose stack before eduprog starts"
    legacy_compose stop --timeout "${EDUPROG_STOP_TIMEOUT_SECONDS:-${CONTOUR_STOP_TIMEOUT_SECONDS:-20}}"
  fi
}

ensure_postgres_volume() {
  [[ "$POSTGRES_VOLUME_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] \
    || die "POSTGRES_VOLUME_NAME is invalid"
  if ! docker volume inspect "$POSTGRES_VOLUME_NAME" >/dev/null 2>&1; then
    log "creating persistent PostgreSQL volume: $POSTGRES_VOLUME_NAME"
    docker volume create \
      --label io.eduprog.purpose=postgres-data \
      "$POSTGRES_VOLUME_NAME" >/dev/null
  fi
}

wait_for_service() {
  local service="$1"
  local timeout_seconds="$2"
  shift 2
  local waited=0

  until compose exec -T "$service" "$@" >/dev/null 2>&1; do
    if (( waited >= timeout_seconds )); then
      log "$service did not become ready in ${timeout_seconds}s"
      compose logs --no-color --tail=80 "$service" >&2 || true
      return 1
    fi
    sleep 2
    ((waited += 2))
  done
  log "$service is ready"
}

wait_for_running_container() {
  local service="$1"
  local timeout_seconds="$2"
  local waited=0

  until [[ "$(compose ps --status running --services "$service" 2>/dev/null)" == "$service" ]]; do
    if (( waited >= timeout_seconds )); then
      log "$service is not running after ${timeout_seconds}s"
      compose ps "$service" >&2 || true
      compose logs --no-color --tail=80 "$service" >&2 || true
      return 1
    fi
    sleep 2
    ((waited += 2))
  done
  log "$service is running"
}

runner_readiness() {
  compose exec -T runner python -c \
    "import urllib.error,urllib.request; url='http://127.0.0.1:8081/health/ready';
try:
 response=urllib.request.urlopen(url, timeout=3)
except urllib.error.HTTPError as exc:
 response=exc
print(response.status, response.read().decode('utf-8', errors='replace'))"
}

diagnose_runner() {
  local runner_container
  log "runner readiness"
  runner_readiness || true

  log "runner container limits (code executes locally inside this container)"
  compose ps runner || true
  runner_container="$(compose ps -q runner 2>/dev/null || true)"
  if [[ -n "$runner_container" ]]; then
    docker inspect --format \
      'read_only={{.HostConfig.ReadonlyRootfs}} memory={{.HostConfig.Memory}} nano_cpus={{.HostConfig.NanoCpus}} pids_limit={{.HostConfig.PidsLimit}} security_opt={{json .HostConfig.SecurityOpt}}' \
      "$runner_container" 2>/dev/null || true
  fi

  log "recent runner logs"
  compose logs --no-color --tail=80 runner || true
}

diagnose_ai() {
  local launcher_key_status="not configured"
  if [[ -n "${AI_API_KEY:-}" ]]; then
    launcher_key_status="configured"
  fi

  log "effective AI configuration in the launcher (the API key is never printed)"
  printf 'AI_ENABLED=%s\n' "${AI_ENABLED:-false}"
  printf 'AI_BASE_URL=%s\n' "${AI_BASE_URL:-}"
  printf 'AI_MODEL=%s\n' "${AI_MODEL:-}"
  printf 'AI_API_STYLE=%s\n' "${AI_API_STYLE:-}"
  printf 'AI_API_KEY=%s\n' "$launcher_key_status"

  if compose ps --status running --services 2>/dev/null | grep -qx backend; then
    log "effective AI configuration inside the running backend"
    compose exec -T backend python -c \
      "import os; names=('AI_ENABLED','AI_BASE_URL','AI_MODEL','AI_API_STYLE'); [print(f'{name}={os.getenv(name, \"\")}') for name in names]; print('AI_API_KEY=' + ('configured' if os.getenv('AI_API_KEY') else 'not configured'))"
  else
    log "backend is not running; only launcher values are shown"
  fi
}

prepare_stack() {
  compose config --quiet
  if [[ "${EDUPROG_SKIP_BUILD:-${CONTOUR_SKIP_BUILD:-false}}" != "true" ]]; then
    log "building application images"
    compose build
  fi

  ensure_postgres_volume
  stop_legacy_stack
  log "starting PostgreSQL"
  compose up -d postgres
  wait_for_service postgres 120 pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"

  log "applying committed Alembic migrations"
  compose run --rm --no-deps backend alembic upgrade head
}

start_detached() {
  prepare_stack
  log "starting Мехмат.Практикум in detached mode"
  if ! compose up -d --remove-orphans; then
    log "one or more containers failed during startup"
    compose ps >&2 || true
    compose logs --no-color --tail=120 moodle-browser backend runner deadline-worker sync-worker frontend >&2 || true
    return 1
  fi
  wait_for_service moodle-browser 180 python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8083/health/ready', timeout=3).read()"
  wait_for_service backend 180 python -c \
    "import urllib.request; request=urllib.request.Request('http://127.0.0.1:8000/api/v1/system/readiness', headers={'X-Forwarded-Proto': 'https'}); urllib.request.urlopen(request, timeout=3).read()"
  wait_for_running_container deadline-worker 30
  wait_for_running_container sync-worker 30
  if ! wait_for_service runner 30 python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/ready', timeout=3).read()"; then
    log "warning: runner is unavailable; the site will start, but compile/run requests remain disabled"
    diagnose_runner
  fi
  wait_for_service frontend 120 wget -q -O /dev/null http://127.0.0.1:8080/healthz
  compose ps
}

serve_foreground() {
  start_detached
  log "following Compose logs without mutating the running stack"
  exec "${COMPOSE[@]}" logs --follow --tail=0
}

stop_stack() {
  log "stopping Мехмат.Практикум containers (the PostgreSQL volume is preserved)"
  compose stop --timeout "${EDUPROG_STOP_TIMEOUT_SECONDS:-${CONTOUR_STOP_TIMEOUT_SECONDS:-20}}"
  stop_legacy_stack
}

usage() {
  cat <<'EOF'
Usage: ./run_eduprog.sh [command]

Commands:
  start     Build, migrate, health-check and start in the background (default)
  serve     Start and attach logs; intended for Type=simple systemd services
  stop      Stop containers without deleting them or the PostgreSQL volume
  restart   Stop and start in detached mode
  update    Pull base images, rebuild, migrate and start in detached mode
  status    Show Compose service state
  logs      Follow application logs
  config    Validate the effective Compose configuration
  admin-token-hash
             Interactively generate an Argon2id administrator-token verifier
  diagnose-moodle-login [base-url]
             Diagnose Moodle mobile login using a hidden password prompt
  diagnose-runner
             Print runner readiness and effective container limits
  diagnose-ai
             Print effective AI provider settings without revealing the API key
  bootstrap-moodle [base-url] [display-name]
             Create/update the Moodle login connection in the application DB
  bootstrap-moodle-bridge [base-url] [display-name]
             Create/update an optional Moodle bridge connection
  help      Show this message

Environment:
  DBLOGIN / DBPASSWORD are accepted as POSTGRES_USER / POSTGRES_PASSWORD aliases.
  OPENROUTER_API_KEY / OPENROUTER_MODEL enable the chat-completions AI adapter.
  EDUPROG_ENV_FILE selects the persistent runtime configuration file.
  EDUPROG_HOST_ENV_FILE selects the host deployment env (default: ~/cpp_markup.env).
  EDUPROG_LEGACY_ENV_FILE selects an existing runtime file to copy once.
  Legacy CONTOUR_* launcher variables remain accepted during migration.
EOF
}

command_name="${1:-start}"
if [[ "$command_name" == "help" || "$command_name" == "--help" || "$command_name" == "-h" ]]; then
  usage
  exit 0
fi

[[ -d "$PROJECT_DIR" ]] || die "project directory does not exist: $PROJECT_DIR"
[[ -f "$COMPOSE_FILE" ]] || die "Compose file does not exist: $COMPOSE_FILE"
require_command docker
require_command openssl
require_command python3
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is unavailable"
if [[ "$command_name" != "config" ]]; then
  docker info >/dev/null 2>&1 || die "Docker daemon is unavailable to the current user"
fi
load_host_environment
ensure_runtime_env
map_legacy_environment

case "$command_name" in
  start)
    start_detached
    ;;
  serve)
    serve_foreground
    ;;
  stop)
    stop_stack
    ;;
  restart)
    stop_stack
    start_detached
    ;;
  update)
    log "pulling base images and rebuilding"
    compose build --pull
    export EDUPROG_SKIP_BUILD=true
    start_detached
    ;;
  status)
    compose ps
    ;;
  logs)
    compose logs --follow --tail=200
    ;;
  config)
    compose config --quiet
    log "Compose configuration is valid"
    ;;
  admin-token-hash)
    compose build backend
    compose run --rm --no-deps backend python -m app.cli hash-admin-token
    ;;
  diagnose-moodle-login)
    moodle_diagnostic_base_url="${2:-${MOODLE_BASE_URL:-https://edu.mmcs.sfedu.ru}}"
    compose build backend
    compose run --rm --no-deps backend python -m app.cli diagnose-moodle-login \
      --base-url "$moodle_diagnostic_base_url"
    ;;
  diagnose-runner)
    diagnose_runner
    ;;
  diagnose-ai)
    diagnose_ai
    ;;
  bootstrap-moodle)
    moodle_base_url="${2:-${MOODLE_BASE_URL:-https://edu.mmcs.sfedu.ru}}"
    moodle_display_name="${3:-${MOODLE_CONNECTION_NAME:-University Moodle}}"
    compose run --rm --no-deps backend python -m app.cli bootstrap-connection \
      --name "$moodle_display_name" \
      --provider MOODLE \
      --auth-mode PLUGINLESS \
      --pluginless-transport PLAYWRIGHT \
      --base-url "$moodle_base_url"
    log "Moodle connection now uses the Playwright pluginless transport"
    ;;
  bootstrap-moodle-bridge)
    moodle_base_url="${2:-${MOODLE_BASE_URL:-https://edu.mmcs.sfedu.ru}}"
    moodle_display_name="${3:-${MOODLE_CONNECTION_NAME:-University Moodle bridge}}"
    compose run --rm --no-deps backend python -m app.cli bootstrap-connection \
      --name "$moodle_display_name" \
      --provider MOODLE \
      --auth-mode BRIDGE \
      --base-url "$moodle_base_url"
    log "Moodle connection now uses the optional bridge transport"
    ;;
  *)
    usage >&2
    die "unknown command: $command_name"
    ;;
esac
