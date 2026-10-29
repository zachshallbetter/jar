#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/titan_env.sh"


AI_ROOT="${AI_ROOT:-/ai}"
TITAN_RUNTIME_ROOT="${TITAN_RUNTIME_ROOT:-${AI_ROOT}/projects/runtimes}"
TITAN_NODE_ROOT="${TITAN_NODE_ROOT:-${AI_ROOT}/tools/node}"
# Keep NODE_ROOT defined for heredoc bodies that may be expanded under nounset.
NODE_ROOT="${NODE_ROOT:-${TITAN_NODE_ROOT}}"
TITAN_NODE_VERSION="${TITAN_NODE_VERSION:-20.19.0}"
RUNTIMES_SOURCE_ROOT="${RUNTIMES_SOURCE_ROOT:-/Users/zachshallbetter/Projects/Runtimes}"
LOG_ROOT="${LOG_ROOT:-${AI_ROOT}/logs}"
TMUX_PREFIX="${TMUX_PREFIX:-runtime}"

# is_titan_host_context is defined canonically in titan_env.sh (sourced above).

run_remote_or_local() {
  local remote_body="$1"
  shift || true
  if is_titan_host_context; then
    bash -s -- "$@" <<<"${remote_body}"
  else
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "bash -s" -- "$@" <<<"${remote_body}"
  fi
}

usage() {
  cat <<'EOF'
Usage:
  titan runtime list [--json]
  titan runtime host status
  titan runtime host install-node [--version X.Y.Z]
  titan runtime bootstrap <runtime_name> [--source-root PATH] [--target-root PATH]
  titan runtime deploy <runtime_name> [--source-root PATH] [--target-root PATH]
  titan runtime start <runtime_name> [--source-root PATH] [--target-root PATH]
  titan runtime status <runtime_name> [--target-root PATH]
  titan runtime stop <runtime_name>
  titan runtime logs <runtime_name> [--tail N] [--follow|--no-follow]
  titan runtime help

Examples:
  titan runtime list
  titan runtime host status
  titan runtime host install-node
  titan runtime deploy text-runtime
  titan runtime deploy identity-runtime
  titan runtime deploy document-runtime
  titan runtime deploy fetch-runtime
  titan runtime deploy record-runtime
  titan runtime deploy retrieval-runtime
  titan runtime deploy event-runtime
EOF
}

runtime_names() {
  python3 - "${RUNTIME_REGISTRY_FILE}" <<'PY'
import json
import sys
from pathlib import Path

registry = Path(sys.argv[1])
data = json.loads(registry.read_text(encoding="utf-8"))
for item in data.get("runtimes", []):
    name = item.get("name")
    if name:
        print(name)
PY
}

runtime_kind() {
  runtime_registry_field "$1" kind
}

runtime_port() {
  runtime_registry_field "$1" port
}

runtime_registry_field() {
  local name="$1"
  local field="$2"
  python3 - "${RUNTIME_REGISTRY_FILE}" "${name}" "${field}" <<'PY'
import json
import sys
from pathlib import Path

registry = Path(sys.argv[1])
name = sys.argv[2]
field = sys.argv[3]
data = json.loads(registry.read_text(encoding="utf-8"))
for item in data.get("runtimes", []):
    if item.get("name") == name:
        value = item.get(field)
        if value is None:
            raise SystemExit(f"missing {field} for {name}")
        if isinstance(value, bool):
            print("true" if value else "false")
        elif isinstance(value, (dict, list)):
            print(json.dumps(value, separators=(",", ":")))
        else:
            print(value)
        break
else:
    raise SystemExit(f"unknown runtime: {name}")
PY
}

runtime_source_root() {
  local name="$1"
  printf '%s/%s' "${RUNTIMES_SOURCE_ROOT}" "$name"
}

runtime_target_root() {
  local name="$1"
  printf '%s/%s' "${TITAN_RUNTIME_ROOT}" "$name"
}

runtime_session() {
  local name="$1"
  printf '%s-%s' "${TMUX_PREFIX}" "$name"
}

runtime_logfile() {
  local name="$1"
  local port="$2"
  printf '%s/%s-%s.log' "${LOG_ROOT}" "$(runtime_session "$name")" "$port"
}

runtime_cmd() {
  local name="$1"
  # Every runtime is Rust and systemd-managed (runtime-registry.json); its launch.sh wraps the binary. The
  # Python-era uvicorn and npm commands, and the HS256 secret they passed, went with titan-node#1118.
  [[ "$(runtime_kind "$name")" == rust ]] || { echo "Unknown or non-Rust runtime: $name" >&2; exit 1; }
  printf 'bash ./launch.sh'
}

runtime_health_url() {
  runtime_registry_field "$1" health_url
}

sync_runtime() {
  local name="$1"
  local source_root="${RUNTIMES_SOURCE_ROOT}"
  local target_root="${TITAN_RUNTIME_ROOT}"
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source-root)
        source_root="$2"
        shift 2
        ;;
      --target-root)
        target_root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan runtime deploy <runtime_name> [--source-root PATH] [--target-root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local source_dir="${source_root}/${name}"
  local remote_dir="${target_root}/${name}"
  if [[ ! -d "${source_dir}" ]]; then
    echo "source runtime not found: ${source_dir}" >&2
    exit 1
  fi

  if is_titan_host_context; then
    mkdir -p "${remote_dir}" "${LOG_ROOT}" "${TITAN_NODE_ROOT}"
  else
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "mkdir -p '${remote_dir}' '${LOG_ROOT}' '${TITAN_NODE_ROOT}'"
  fi
  local rsync_target
  if is_titan_host_context; then
    rsync_target="${remote_dir}/"
  else
    rsync_target="${TITAN_HOST}:${remote_dir}/"
  fi
  rsync -a --delete \
    --exclude '.git/' \
    --exclude 'node_modules/' \
    --exclude '.env' \
    --exclude '.env.local' \
    --exclude 'service.env' \
    --exclude '.venv/' \
    --exclude '.titan-venv/' \
    --exclude 'venv/' \
    --exclude 'venv311/' \
    --exclude 'venv_local/' \
    --exclude '.pytest_cache/' \
    --exclude '.mypy_cache/' \
    --exclude '.ruff_cache/' \
    --exclude '.vercel/' \
    --exclude '/dist/' \
    --exclude '/build/' \
    --exclude '/target/' \
    --exclude '/output/' \
    --exclude 'screenshots/' \
    --exclude '.tmp/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "${source_dir}/" "${rsync_target}"

  echo "runtime_name=${name}"
  echo "runtime_source=${source_dir}"
  echo "runtime_target=${remote_dir}"
}

host_status() {
  run_remote_or_local "$(cat <<'EOF'
set -e
printf 'host=%s\n' "$(hostname)"
for cmd in node npm python3 python uv pip rsync tmux git curl wget; do
  if command -v "$cmd" >/dev/null 2>&1; then
    printf '%s=%s\n' "$cmd" "$(command -v "$cmd")"
  else
    printf '%s=missing\n' "$cmd"
  fi
done
if [[ -x /ai/tools/node/current/bin/node ]]; then
  printf 'node_toolchain=%s\n' "/ai/tools/node/current/bin/node"
  /ai/tools/node/current/bin/node --version || true
  PATH="/ai/tools/node/current/bin:${PATH}" /ai/tools/node/current/bin/npm --version || true
fi
EOF
 )"
}

install_node() {
  local version="${TITAN_NODE_VERSION}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --version)
        version="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan runtime host install-node [--version X.Y.Z]" >&2
        exit 1
        ;;
    esac
  done

  run_remote_or_local "$(cat <<'EOF'
set -euo pipefail

VERSION="$1"
NODE_ROOT="$2"
ARCHIVE="node-v${VERSION}-linux-x64.tar.xz"
URL="https://nodejs.org/dist/v${VERSION}/${ARCHIVE}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT
mkdir -p "${NODE_ROOT}"
  if [[ -x "${NODE_ROOT}/current/bin/node" ]]; then
    current_version="$("${NODE_ROOT}/current/bin/node" --version | sed 's/^v//')"
    case "${current_version}" in
      ${VERSION}|${VERSION}*)
        printf 'node_toolchain=%s\n' "${NODE_ROOT}/current"
        PATH="${NODE_ROOT}/current/bin:${PATH}" "${NODE_ROOT}/current/bin/npm" --version >/dev/null
        exit 0
        ;;
    esac
  fi
cd "${TMP_DIR}"
curl -fsSLO "${URL}"
tar -xJf "${ARCHIVE}"
rm -f "${NODE_ROOT}/current"
mv "node-v${VERSION}-linux-x64" "${NODE_ROOT}/${VERSION}"
ln -sfn "${NODE_ROOT}/${VERSION}" "${NODE_ROOT}/current"
printf 'node_toolchain=%s\n' "${NODE_ROOT}/current"
printf 'node_version='
"${NODE_ROOT}/current/bin/node" --version
printf 'npm_version='
PATH="${NODE_ROOT}/current/bin:${PATH}" "${NODE_ROOT}/current/bin/npm" --version
EOF
)" "${version}" "${TITAN_NODE_ROOT}"
}

bootstrap_runtime() {
  local name="$1"
  local source_root="${RUNTIMES_SOURCE_ROOT}"
  local target_root="${TITAN_RUNTIME_ROOT}"
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source-root)
        source_root="$2"
        shift 2
        ;;
      --target-root)
        target_root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan runtime bootstrap <runtime_name> [--source-root PATH] [--target-root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local kind target_dir
  kind="$(runtime_kind "$name")"
  target_dir="${target_root}/${name}"

  case "${kind}" in
    rust)
      run_remote_or_local "$(cat <<'EOF'
set -euo pipefail

export PATH="$HOME/.cargo/bin:$PATH"

TARGET_DIR="$1"
cd "${TARGET_DIR}"
mkdir -p state output

if ! command -v cargo >/dev/null 2>&1; then
  echo "cargo not found. Rust must be installed on the titan host to compile the binary." >&2
  exit 1
fi

cargo build --release
EOF
)" "${target_dir}"
      ;;
    *)
      echo "Unsupported runtime kind: ${kind}" >&2
      exit 1
      ;;
  esac

  echo "runtime_name=${name}"
  echo "runtime_kind=${kind}"
  echo "runtime_target=${target_dir}"
}

start_runtime() {
  local name="$1"
  local source_root="${RUNTIMES_SOURCE_ROOT}"
  local target_root="${TITAN_RUNTIME_ROOT}"
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source-root)
        source_root="$2"
        shift 2
        ;;
      --target-root)
        target_root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan runtime start <runtime_name> [--source-root PATH] [--target-root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local target_dir session logfile cmd health_url
  target_dir="${target_root}/${name}"
  session="$(runtime_session "$name")"
  logfile="$(runtime_logfile "$name" "$(runtime_port "$name")")"
  cmd="$(runtime_cmd "$name" "$target_dir")"
  health_url="$(runtime_health_url "$name")"

  local remote_cmd
  remote_cmd="$(printf 'TARGET_DIR=%q SESSION=%q LOGFILE=%q CMD=%q HEALTH_URL=%q bash -s' \
    "${target_dir}" "${session}" "${logfile}" "${cmd}" "${health_url}")"

if is_titan_host_context; then
bash -s -- "${target_dir}" "${session}" "${logfile}" "${cmd}" "${health_url}" <<'EOF'
set -euo pipefail

TARGET_DIR="$1"
SESSION="$2"
LOGFILE="$3"
CMD="$4"
HEALTH_URL="$5"
mkdir -p "$(dirname "$LOGFILE")"
mkdir -p "${TARGET_DIR}/state" "${TARGET_DIR}/output"
if [[ ! -d "${TARGET_DIR}" ]]; then
  echo "runtime not synced yet: ${TARGET_DIR}" >&2
  exit 1
fi
if tmux has-session -t "${SESSION}" 2>/dev/null; then
  echo "runtime_session=${SESSION}"
  echo "runtime_status=already_running"
  echo "runtime_log=${LOGFILE}"
  echo "runtime_health=${HEALTH_URL}"
  exit 0
fi
tmux new-session -d -s "${SESSION}" "bash -lc 'cd \"${TARGET_DIR}\" && ${CMD}' 2>&1 | tee -a '${LOGFILE}'"
echo "runtime_session=${SESSION}"
echo "runtime_status=started"
echo "runtime_log=${LOGFILE}"
echo "runtime_health=${HEALTH_URL}"
EOF
else
ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "${remote_cmd}" <<'EOF'
set -euo pipefail

mkdir -p "$(dirname "$LOGFILE")"
mkdir -p "${TARGET_DIR}/state" "${TARGET_DIR}/output"
if [[ ! -d "${TARGET_DIR}" ]]; then
  echo "runtime not synced yet: ${TARGET_DIR}" >&2
  exit 1
fi
if tmux has-session -t "${SESSION}" 2>/dev/null; then
  echo "runtime_session=${SESSION}"
  echo "runtime_status=already_running"
  echo "runtime_log=${LOGFILE}"
  echo "runtime_health=${HEALTH_URL}"
  exit 0
fi
tmux new-session -d -s "${SESSION}" "bash -lc 'cd \"${TARGET_DIR}\" && ${CMD}' 2>&1 | tee -a '${LOGFILE}'"
echo "runtime_session=${SESSION}"
echo "runtime_status=started"
echo "runtime_log=${LOGFILE}"
echo "runtime_health=${HEALTH_URL}"
EOF
fi
}

status_runtime() {
  local name="$1"
  local target_root="${TITAN_RUNTIME_ROOT}"
  shift || true
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --target-root)
        target_root="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan runtime status <runtime_name> [--target-root PATH]" >&2
        exit 1
        ;;
    esac
  done

  local session logfile health_url target_dir
  session="$(runtime_session "$name")"
  logfile="$(runtime_logfile "$name" "$(runtime_port "$name")")"
  health_url="$(runtime_health_url "$name")"
  target_dir="${target_root}/${name}"

  if is_titan_host_context; then
    bash -s -- "${session}" "${logfile}" "${health_url}" "${target_dir}" <<'EOF'
set -euo pipefail

SESSION="$1"
LOGFILE="$2"
HEALTH_URL="$3"
TARGET_DIR="$4"
if tmux has-session -t "${SESSION}" 2>/dev/null; then
  status="running"
elif [ -n "${HEALTH_URL}" ] && curl -fsS --max-time 4 "${HEALTH_URL}" >/dev/null 2>&1; then
  status="running"
else
  status="stopped"
fi
printf 'runtime_session=%s\n' "${SESSION}"
printf 'runtime_status=%s\n' "${status}"
printf 'runtime_log=%s\n' "${LOGFILE}"
printf 'runtime_health=%s\n' "${HEALTH_URL}"
printf 'runtime_target=%s\n' "${TARGET_DIR}"
EOF
  else
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "bash -s" -- "${session}" "${logfile}" "${health_url}" "${target_dir}" <<'EOF'
set -euo pipefail

SESSION="$1"
LOGFILE="$2"
HEALTH_URL="$3"
TARGET_DIR="$4"
if tmux has-session -t "${SESSION}" 2>/dev/null; then
  status="running"
elif [ -n "${HEALTH_URL}" ] && curl -fsS --max-time 4 "${HEALTH_URL}" >/dev/null 2>&1; then
  status="running"
else
  status="stopped"
fi
printf 'runtime_session=%s\n' "${SESSION}"
printf 'runtime_status=%s\n' "${status}"
printf 'runtime_log=%s\n' "${LOGFILE}"
printf 'runtime_health=%s\n' "${HEALTH_URL}"
printf 'runtime_target=%s\n' "${TARGET_DIR}"
EOF
  fi
}

stop_runtime() {
  local name="$1"
  local session
  session="$(runtime_session "$name")"
  if is_titan_host_context; then
    bash -s -- "${session}" <<'EOF'
set -euo pipefail

SESSION="$1"
if tmux has-session -t "${SESSION}" 2>/dev/null; then
  tmux kill-session -t "${SESSION}"
  echo "runtime_session=${SESSION}"
  echo "runtime_status=stopped"
else
  echo "runtime_session=${SESSION}"
  echo "runtime_status=not_running"
fi
EOF
  else
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "bash -s" -- "${session}" <<'EOF'
set -euo pipefail

SESSION="$1"
if tmux has-session -t "${SESSION}" 2>/dev/null; then
  tmux kill-session -t "${SESSION}"
  echo "runtime_session=${SESSION}"
  echo "runtime_status=stopped"
else
  echo "runtime_session=${SESSION}"
  echo "runtime_status=not_running"
fi
EOF
  fi
}

logs_runtime() {
  local name="$1"
  shift || true
  local tail=200
  local follow="true"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --tail)
        tail="${2:-}"
        if [[ -z "${tail}" ]]; then
          echo "Usage: titan runtime logs <runtime_name> [--tail N] [--follow|--no-follow]" >&2
          exit 1
        fi
        shift 2
        ;;
      --follow)
        follow="true"
        shift
        ;;
      --no-follow)
        follow="false"
        shift
        ;;
      *)
        echo "Usage: titan runtime logs <runtime_name> [--tail N] [--follow|--no-follow]" >&2
        exit 1
        ;;
    esac
  done
  local logfile
  logfile="$(runtime_logfile "$name" "$(runtime_port "$name")")"
  if is_titan_host_context; then
    bash -s -- "${logfile}" "${tail}" "${follow}" <<'EOF'
set -euo pipefail

LOGFILE="$1"
TAIL_LINES="$2"
FOLLOW="$3"
if [[ ! -f "${LOGFILE}" ]]; then
  echo "No runtime log found: ${LOGFILE}" >&2
  exit 1
fi
if [[ "${FOLLOW}" == "true" ]]; then
  tail -n "${TAIL_LINES}" -f "${LOGFILE}"
else
  tail -n "${TAIL_LINES}" "${LOGFILE}"
fi
EOF
  else
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "bash -s" -- "${logfile}" "${tail}" "${follow}" <<'EOF'
set -euo pipefail

LOGFILE="$1"
TAIL_LINES="$2"
FOLLOW="$3"
if [[ ! -f "${LOGFILE}" ]]; then
  echo "No runtime log found: ${LOGFILE}" >&2
  exit 1
fi
if [[ "${FOLLOW}" == "true" ]]; then
  tail -n "${TAIL_LINES}" -f "${LOGFILE}"
else
  tail -n "${TAIL_LINES}" "${LOGFILE}"
fi
EOF
  fi
}

list_runtime() {
  local json="${1:-false}"
  if [[ "${json}" == "true" ]]; then
    python3 - "${RUNTIME_REGISTRY_FILE}" "${RUNTIMES_SOURCE_ROOT}" "${TITAN_RUNTIME_ROOT}" <<'PY'
import json
import sys
from pathlib import Path

registry = Path(sys.argv[1])
source_root = sys.argv[2]
target_root = sys.argv[3]
data = json.loads(registry.read_text(encoding="utf-8"))
catalog = []
for item in data.get("runtimes", []):
    name = item.get("name")
    if not name:
        continue
    catalog.append({
        "name": name,
        "kind": item.get("kind", "unknown"),
        "port": item.get("port", 0),
        "health_url": item.get("health_url", ""),
        "source": f"{source_root}/{name}",
        "target": f"{target_root}/{name}",
    })
print(json.dumps(catalog, indent=2))
PY
    return
  fi
  runtime_names | while read -r name; do
    printf '%s kind=%s port=%s source=%s target=%s\n' \
      "$name" \
      "$(runtime_kind "$name")" \
      "$(runtime_port "$name")" \
      "$(runtime_source_root "$name")" \
      "$(runtime_target_root "$name")"
  done
}

deploy_runtime() {
  local name="$1"
  shift || true
  sync_runtime "$name" "$@"
  bootstrap_runtime "$name" "$@"
  # Ensure deploys always activate new source/env; leaving an existing tmux
  # session running can keep serving stale runtime code/config.
  stop_runtime "$name" >/dev/null 2>&1 || true
  start_runtime "$name" "$@"
}

main() {
  local subcommand="${1:-help}"
  shift || true

  case "${subcommand}" in
    help|-h|--help)
      usage
      ;;
    list)
      if [[ "${1:-}" == "--json" ]]; then
        list_runtime true
      else
        list_runtime false
      fi
      ;;
    host)
      case "${1:-}" in
        status)
          shift || true
          host_status
          ;;
        install-node)
          shift || true
          install_node "$@"
          ;;
        *)
          echo "Usage: titan runtime host <status|install-node>" >&2
          exit 1
          ;;
      esac
      ;;
    bootstrap)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan runtime bootstrap <runtime_name> [--source-root PATH] [--target-root PATH]" >&2
        exit 1
      fi
      bootstrap_runtime "$@"
      ;;
    deploy)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan runtime deploy <runtime_name> [--source-root PATH] [--target-root PATH]" >&2
        exit 1
      fi
      deploy_runtime "$@"
      ;;
    start)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan runtime start <runtime_name> [--source-root PATH] [--target-root PATH]" >&2
        exit 1
      fi
      start_runtime "$@"
      ;;
    status)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan runtime status <runtime_name> [--target-root PATH]" >&2
        exit 1
      fi
      status_runtime "$@"
      ;;
    stop)
      if [[ $# -ne 1 ]]; then
        echo "Usage: titan runtime stop <runtime_name>" >&2
        exit 1
      fi
      stop_runtime "$1"
      ;;
    logs)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan runtime logs <runtime_name> [--tail N] [--follow|--no-follow]" >&2
        exit 1
      fi
      logs_runtime "$@"
      ;;
    *)
      echo "Unknown runtime subcommand: ${subcommand}" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
