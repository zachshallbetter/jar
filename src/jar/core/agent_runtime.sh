#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="${AGENT_ROOT:-/ai/projects/titan-local-agent}"
DEFAULT_NAME="${AGENT_NAME:-titan-local-agent}"
DEFAULT_SESSION="${AGENT_SESSION:-${DEFAULT_NAME}}"
DEFAULT_MODEL_DIR="${AGENT_MODEL_DIR:-/ai/models/titan-local-agent}"
DEFAULT_MODEL_PATH="${AGENT_MODEL_PATH:-}"
DEFAULT_RUNTIME_CMD="${AGENT_RUNTIME_CMD:-}"
DEFAULT_LOG_DIR="${AGENT_LOG_DIR:-/ai/logs}"
DEFAULT_PORT="${AGENT_PORT:-8010}"
DEFAULT_HOST="${AGENT_HOST:-0.0.0.0}"
DEFAULT_MODEL_KIND="${AGENT_MODEL_KIND:-gguf}"
DEFAULT_MODEL_ID="${AGENT_MODEL_ID:-}"
DEFAULT_ROUTER_MODE="${AGENT_ROUTER_MODE:-none}"
DEFAULT_ROUTER_URL="${AGENT_ROUTER_URL:-http://127.0.0.1:8500}"
DEFAULT_ROUTER_TIMEOUT_SECONDS="${AGENT_ROUTER_TIMEOUT_SECONDS:-15}"
DEFAULT_RETRIEVAL_CMD="${AGENT_RETRIEVAL_CMD:-}"
DEFAULT_CONTEXT_MODE="${AGENT_CONTEXT_MODE:-summary}"
if [[ -n "${AGENT_CONTEXT_CMD:-}" ]]; then
  DEFAULT_CONTEXT_CMD="${AGENT_CONTEXT_CMD}"
else
  # This checkout's context.sh first: an installed `titan` on PATH can be months stale, and its
  # made-up warnings reach the model as "live" context.
  TITAN_BIN="$(command -v titan 2>/dev/null || true)"
  if [[ -x "${SCRIPT_DIR}/context.sh" ]]; then
    DEFAULT_CONTEXT_CMD="${SCRIPT_DIR}/context.sh --json"
  elif [[ -n "${TITAN_BIN}" ]]; then
    DEFAULT_CONTEXT_CMD="${TITAN_BIN} context --json"
  else
    DEFAULT_CONTEXT_CMD="titan context --json"
  fi
fi

print_usage() {
  cat <<'EOF'
Usage:
  titan agent init [--root PATH] [--name NAME] [--session NAME] [--model-dir PATH] [--model-path PATH] [--model-kind gguf|hf] [--model-id REPO_ID] [--runtime-cmd CMD] [--log-dir PATH] [--port PORT]
  titan agent status [--root PATH] [--json]
  titan agent serve [--root PATH] [--session NAME] [--min-free-mb MB] [--max-util PERCENT] [--gpu auto|INDEX] [--log-dir PATH] [--] [command...]
  titan agent stop [--root PATH]
  titan agent logs [--root PATH]
  titan agent prompt [--root PATH] [--endpoint URL] [--model NAME] [--system TEXT] [--router none|parametric] [--router-url URL] [--router-timeout SECONDS] [--context auto|summary|full|none] [--context-cmd CMD] [--temperature FLOAT] [--max-tokens INT] [--json] [prompt...]
  titan agent chat [--root PATH] [--endpoint URL] [--model NAME] [--system TEXT] [--router none|parametric] [--router-url URL] [--router-timeout SECONDS] [--context auto|summary|full|none] [--context-cmd CMD] [--temperature FLOAT] [--max-tokens INT] [--json] [prompt...]
EOF
}

load_config() {
  if [[ -f "${AGENT_ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${AGENT_ENV_FILE}"
    set +a
  fi
}

resolve_defaults() {
  AGENT_NAME="${AGENT_NAME:-${DEFAULT_NAME}}"
  AGENT_SESSION="${AGENT_SESSION:-${DEFAULT_SESSION}}"
  AGENT_MODEL_DIR="${AGENT_MODEL_DIR:-${DEFAULT_MODEL_DIR}}"
  AGENT_MODEL_PATH="${AGENT_MODEL_PATH:-${DEFAULT_MODEL_PATH}}"
  AGENT_MODEL_KIND="${AGENT_MODEL_KIND:-${DEFAULT_MODEL_KIND}}"
  AGENT_MODEL_ID="${AGENT_MODEL_ID:-${DEFAULT_MODEL_ID}}"
  AGENT_RUNTIME_CMD="${AGENT_RUNTIME_CMD:-${DEFAULT_RUNTIME_CMD}}"
  AGENT_LOG_DIR="${AGENT_LOG_DIR:-${DEFAULT_LOG_DIR}}"
  AGENT_PORT="${AGENT_PORT:-${DEFAULT_PORT}}"
  AGENT_HOST="${AGENT_HOST:-${DEFAULT_HOST}}"
  AGENT_ROUTER_MODE="${AGENT_ROUTER_MODE:-${DEFAULT_ROUTER_MODE}}"
  AGENT_ROUTER_URL="${AGENT_ROUTER_URL:-${DEFAULT_ROUTER_URL}}"
  AGENT_ROUTER_TIMEOUT_SECONDS="${AGENT_ROUTER_TIMEOUT_SECONDS:-${DEFAULT_ROUTER_TIMEOUT_SECONDS}}"
  AGENT_RETRIEVAL_CMD="${AGENT_RETRIEVAL_CMD:-${DEFAULT_RETRIEVAL_CMD}}"
}

ensure_tmux() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required for this command." >&2
    exit 1
  fi
}

write_default_env() {
  local root="$1"
  local env_file="$2"
  local name="$3"
  local session="$4"
  local model_dir="$5"
  local model_path="$6"
  local model_kind="$7"
  local model_id="$8"
  local runtime_cmd="$9"
  local log_dir="${10}"
  local host="${11}"
  local port="${12}"
  local router_mode="${13}"
  local router_url="${14}"
  local router_timeout_seconds="${15}"
  cat >"${env_file}" <<EOF
# Titan local agent workspace configuration.
AGENT_NAME="${name}"
AGENT_SESSION="${session}"
AGENT_MODEL_DIR="${model_dir}"
AGENT_MODEL_PATH="${model_path}"
AGENT_MODEL_KIND="${model_kind}"
AGENT_MODEL_ID="${model_id}"
AGENT_RUNTIME_CMD="${runtime_cmd}"
AGENT_LOG_DIR="${log_dir}"
AGENT_HOST="${host}"
AGENT_PORT="${port}"
AGENT_ROUTER_MODE="${router_mode}"
AGENT_ROUTER_URL="${router_url}"
AGENT_ROUTER_TIMEOUT_SECONDS="${router_timeout_seconds}"
EOF
  chmod 0644 "${env_file}"
}

write_agent_readme() {
  local root="$1"
  local name="$2"
  local env_file="$3"
  local model_dir="$4"
  local log_dir="$5"
  cat >"${root}/README.md" <<EOF
# ${name}

Titan-managed local agent workspace.

Default paths:
- model dir: ${model_dir}
- logs: ${log_dir}
- runtime config: ${env_file}

Edit ${env_file} to point AGENT_RUNTIME_CMD at your local model server.
Set AGENT_ROUTER_MODE=parametric to consult Titan's epistemic runtime before generation.

Model selection is dual-mode:

- GGUF/local-file presets use the Prism llama.cpp server
- Transformer presets use the TurboQuant HF server and an HF repo id

Examples:
- llama.cpp server
- Python OpenAI-compatible server
- custom agent loop
EOF
}

init_workspace() {
  local root="$1"
  local name="$2"
  local session="$3"
  local model_dir="$4"
  local model_path="$5"
  local model_kind="$6"
  local model_id="$7"
  local runtime_cmd="$8"
  local log_dir="$9"
  local host="${10}"
  local port="${11}"
  mkdir -p "${root}/"{models,logs,runtime,prompts,config}
  mkdir -p "${model_dir}"
  mkdir -p "${log_dir}"
  local env_file="${root}/agent.env"
  write_default_env "${root}" "${env_file}" "${name}" "${session}" "${model_dir}" "${model_path}" "${model_kind}" "${model_id}" "${runtime_cmd}" "${log_dir}" "${host}" "${port}" "${AGENT_ROUTER_MODE:-${DEFAULT_ROUTER_MODE}}" "${AGENT_ROUTER_URL:-${DEFAULT_ROUTER_URL}}" "${AGENT_ROUTER_TIMEOUT_SECONDS:-${DEFAULT_ROUTER_TIMEOUT_SECONDS}}"
  if [[ ! -f "${root}/README.md" ]]; then
    write_agent_readme "${root}" "${name}" "${env_file}" "${model_dir}" "${log_dir}"
  fi
  echo "agent_root=${root}"
  echo "agent_env=${env_file}"
  echo "agent_name=${name}"
  echo "agent_session=${session}"
  echo "agent_model_dir=${model_dir}"
  echo "agent_model_path=${model_path:-unset}"
  echo "agent_model_kind=${model_kind:-unset}"
  echo "agent_model_id=${model_id:-unset}"
  echo "agent_runtime_cmd=${runtime_cmd:-unset}"
  echo "agent_log_dir=${log_dir}"
  echo "agent_router_mode=${AGENT_ROUTER_MODE:-${DEFAULT_ROUTER_MODE}}"
  echo "agent_router_url=${AGENT_ROUTER_URL:-${DEFAULT_ROUTER_URL}}"
  echo "agent_router_timeout_seconds=${AGENT_ROUTER_TIMEOUT_SECONDS:-${DEFAULT_ROUTER_TIMEOUT_SECONDS}}"
}

latest_log() {
  local root="$1"
  local session="$2"
  find "${root}" -maxdepth 1 -type f -name "${session}-*.log" 2>/dev/null | sort | tail -n 1
}

status_text() {
  local root="$1"
  local env_file="${root}/agent.env"
  if [[ ! -d "${root}" ]]; then
    echo "agent_root=${root}"
    echo "status=missing"
    return
  fi
  AGENT_ENV_FILE="${env_file}"
  load_config
  resolve_defaults
  local log_file
  log_file="$(latest_log "${AGENT_LOG_DIR}" "${AGENT_SESSION}")"
  local tmux_state="absent"
  if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "${AGENT_SESSION}" 2>/dev/null; then
      tmux_state="running"
    else
      tmux_state="stopped"
    fi
  fi
  echo "agent_root=${root}"
  echo "agent_env=${env_file}"
  echo "agent_name=${AGENT_NAME}"
  echo "agent_session=${AGENT_SESSION}"
  echo "agent_model_dir=${AGENT_MODEL_DIR}"
  echo "agent_model_path=${AGENT_MODEL_PATH:-unset}"
  echo "agent_model_kind=${AGENT_MODEL_KIND:-unset}"
  echo "agent_model_id=${AGENT_MODEL_ID:-unset}"
  echo "agent_runtime_cmd=${AGENT_RUNTIME_CMD:-unset}"
  echo "agent_log_dir=${AGENT_LOG_DIR}"
  echo "agent_port=${AGENT_PORT}"
  echo "agent_host=${AGENT_HOST}"
  echo "tmux_session=${tmux_state}"
  echo "latest_log=${log_file:-none}"
  if [[ -d "${AGENT_MODEL_DIR}" ]]; then
    echo "model_files=$(find "${AGENT_MODEL_DIR}" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"
  else
    echo "model_files=0"
  fi
}

status_json() {
  /usr/bin/env python3 "${SCRIPT_DIR}/agent_runtime_lib.py" status_json_13  "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" "$9" "${10}" "${11}" "${12}"
}

serve_agent() {
  local root="$1"
  shift
  local session=""
  local min_free_mb=0
  local max_util=100
  local gpu_spec="auto"
  local runtime_override=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --session)
        session="$2"
        shift 2
        ;;
      --min-free-mb)
        min_free_mb="$2"
        shift 2
        ;;
      --max-util)
        max_util="$2"
        shift 2
        ;;
      --gpu)
        gpu_spec="$2"
        shift 2
        ;;
      --)
        shift
        runtime_override="$(printf '%q ' "$@")"
        break
        ;;
      *)
        runtime_override="$(printf '%q ' "$@")"
        break
        ;;
    esac
  done

  AGENT_ENV_FILE="${root}/agent.env"
  load_config
  resolve_defaults
  if [[ -z "${session}" ]]; then
    session="${AGENT_SESSION}"
  fi
  if [[ -z "${runtime_override}" ]]; then
    runtime_override="${AGENT_RUNTIME_CMD}"
  fi
  if [[ -z "${runtime_override}" ]]; then
    echo "No runtime command configured. Set AGENT_RUNTIME_CMD in ${AGENT_ENV_FILE} or pass a command to titan agent serve." >&2
    exit 1
  fi
  ensure_tmux
  if tmux has-session -t "${session}" 2>/dev/null; then
    echo "tmux session already exists: ${session}" >&2
    exit 1
  fi
  mkdir -p "${AGENT_LOG_DIR}"
  local stamp log_file
  stamp="$(date -u +%Y%m%d-%H%M%SZ)"
  log_file="${AGENT_LOG_DIR}/${session}-${stamp}.log"

  local launch_cmd
  launch_cmd=(
    env
    "AGENT_ROOT=${root}"
    "AGENT_NAME=${AGENT_NAME}"
    "AGENT_SESSION=${session}"
    "AGENT_MODEL_DIR=${AGENT_MODEL_DIR}"
    "AGENT_MODEL_PATH=${AGENT_MODEL_PATH}"
    "AGENT_MODEL_KIND=${AGENT_MODEL_KIND:-${DEFAULT_MODEL_KIND}}"
    "AGENT_MODEL_ID=${AGENT_MODEL_ID:-${DEFAULT_MODEL_ID}}"
    "AGENT_RUNTIME_CMD=${runtime_override}"
    "AGENT_LOG_DIR=${AGENT_LOG_DIR}"
    "AGENT_HOST=${AGENT_HOST}"
    "AGENT_PORT=${AGENT_PORT}"
    bash
    -lc
    "$runtime_override"
  )

  if [[ "${gpu_spec}" == "auto" ]]; then
    "${SCRIPT_DIR}/resource_broker.sh" tmux "${session}" --min-free-mb "${min_free_mb}" --max-util "${max_util}" "${launch_cmd[@]}"
  else
    local gpu="${gpu_spec}"
    COMMAND_STR="$(printf '%q ' "${launch_cmd[@]}")"
    if [[ "${gpu}" == "cpu" ]]; then
      gpu=""
    fi
    tmux new-session -d -s "${session}" \
      "export CUDA_VISIBLE_DEVICES=${gpu}; export TITAN_NODE_GPU=${gpu}; echo \"[$(date -Is)] session=${session} gpu=${gpu}\" | tee -a ${log_file}; ${COMMAND_STR} 2>&1 | tee -a ${log_file}"
    echo "session=${session}"
    echo "gpu=${gpu}"
    echo "log=${log_file}"
  fi
}

agent_tasks() {
  local root="${1:-/ai/projects/titan-local-agent}"
  AGENT_ENV_FILE="${root}/agent.env"
  load_config   # sources agent.env (COORDINATOR_JWT_SECRET etc.) so _runtime_request can auth
  /usr/bin/env python3 "${SCRIPT_DIR}/agent_runtime_lib.py" list_tasks_json
}

agent_tool() {
  AGENT_ENV_FILE="${AGENT_ROOT:-/ai/projects/titan-local-agent}/agent.env"
  load_config
  /usr/bin/env python3 "${SCRIPT_DIR}/agent_runtime_lib.py" tool_cli "$@"
}

prompt_agent() {
  local root="$1"
  shift
  local endpoint=""
  local model=""
  local system=""
  local router_mode="${AGENT_ROUTER_MODE:-${DEFAULT_ROUTER_MODE}}"
  local router_url="${AGENT_ROUTER_URL:-${DEFAULT_ROUTER_URL}}"
  local router_timeout_seconds="${AGENT_ROUTER_TIMEOUT_SECONDS:-${DEFAULT_ROUTER_TIMEOUT_SECONDS}}"
  local retrieval_cmd="${AGENT_RETRIEVAL_CMD:-${DEFAULT_RETRIEVAL_CMD}}"
  local context_mode="${AGENT_CONTEXT_MODE:-${DEFAULT_CONTEXT_MODE}}"
  local context_cmd="${AGENT_CONTEXT_CMD:-${DEFAULT_CONTEXT_CMD}}"
  local temperature="0.2"
  local max_tokens=""
  local json_mode="0"
  local tools_enabled="0"
  local prompt_parts=()

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root)
        root="$2"
        shift 2
        ;;
      --endpoint)
        endpoint="$2"
        shift 2
        ;;
      --model)
        model="$2"
        shift 2
        ;;
      --system)
        system="$2"
        shift 2
        ;;
      --router)
        router_mode="$2"
        shift 2
        ;;
      --router-url)
        router_url="$2"
        shift 2
        ;;
      --router-timeout)
        router_timeout_seconds="$2"
        shift 2
        ;;
      --retrieval-cmd)
        retrieval_cmd="$2"
        shift 2
        ;;
      --context)
        context_mode="$2"
        shift 2
        ;;
      --context-cmd)
        context_cmd="$2"
        shift 2
        ;;
      --temperature)
        temperature="$2"
        shift 2
        ;;
      --max-tokens)
        max_tokens="$2"
        shift 2
        ;;
      --json)
        json_mode="1"
        shift
        ;;
      --tools)
        tools_enabled="1"
        shift
        ;;
      --)
        shift
        prompt_parts+=("$@")
        break
        ;;
      *)
        prompt_parts+=("$1")
        shift
        ;;
    esac
  done

  AGENT_ENV_FILE="${root}/agent.env"
  load_config
  resolve_defaults

  if [[ -z "${endpoint}" ]]; then
    endpoint="http://127.0.0.1:${AGENT_PORT}"
  fi
  if [[ -z "${model}" ]]; then
    model="${AGENT_MODEL_PATH:-local}"
  fi

  local prompt=""
  prompt="${prompt_parts[*]}"
  if [[ -z "${prompt}" && ! -t 0 ]]; then
    prompt="$(cat)"
  fi
  if [[ -z "${prompt}" ]]; then
    echo "Usage: titan agent prompt [--root PATH] [--endpoint URL] [--model NAME] [--system TEXT] [--router none|parametric] [--router-url URL] [--router-timeout SECONDS] [--context auto|summary|full|none] [--context-cmd CMD] [--retrieval-cmd CMD] [--temperature FLOAT] [--max-tokens INT] [--json] [--tools] [prompt...]" >&2
    exit 1
  fi

  AGENT_ENDPOINT="${endpoint}" \
  AGENT_MODEL="${model}" \
  AGENT_SYSTEM="${system:-${AGENT_SYSTEM:-}}" \
  AGENT_ROUTER_MODE="${router_mode}" \
  AGENT_ROUTER_URL="${router_url}" \
  AGENT_ROUTER_TIMEOUT_SECONDS="${router_timeout_seconds}" \
  AGENT_RETRIEVAL_CMD="${retrieval_cmd}" \
  AGENT_CONTEXT_MODE="${context_mode}" \
  AGENT_CONTEXT_CMD="${context_cmd}" \
  AGENT_CHAT_HISTORY_FILE="${AGENT_CHAT_HISTORY_FILE:-}" \
  AGENT_PROMPT="${prompt}" \
  AGENT_SCRIPT_DIR="${SCRIPT_DIR}" \
  AGENT_TEMPERATURE="${temperature}" \
  AGENT_MAX_TOKENS="${max_tokens:-${AGENT_MAX_TOKENS:-512}}" \
  AGENT_JSON_MODE="${json_mode}" \
  AGENT_TOOLS_ENABLED="${tools_enabled}" \
  /usr/bin/env python3 "${SCRIPT_DIR}/agent_runtime_lib.py" prompt_agent_14 
}

stop_agent() {
  local session="$1"
  ensure_tmux
  if tmux has-session -t "${session}" 2>/dev/null; then
    tmux kill-session -t "${session}"
    echo "stopped=${session}"
  else
    echo "stopped=${session}"
  fi
}

print_usage() {
  cat <<'EOF'
Usage:
  titan agent init [--root PATH] [--name NAME] [--session NAME] [--model-dir PATH] [--model-path PATH] [--model-kind gguf|hf] [--model-id REPO_ID] [--runtime-cmd CMD] [--port PORT]
  titan agent status [--root PATH] [--json]
  titan agent serve [--root PATH] [--session NAME] [--min-free-mb MB] [--max-util PERCENT] [--gpu auto|INDEX] [--] [command...]
  titan agent stop [--root PATH]
  titan agent logs [--root PATH]
  titan agent prompt [--root PATH] [--endpoint URL] [--model NAME] [--system TEXT] [--router none|parametric] [--router-url URL] [--router-timeout SECONDS] [--context auto|summary|full|none] [--context-cmd CMD] [--temperature FLOAT] [--max-tokens INT] [--json] [prompt...]
  titan agent chat [--root PATH] [--endpoint URL] [--model NAME] [--system TEXT] [--router none|parametric] [--router-url URL] [--router-timeout SECONDS] [--context auto|summary|full|none] [--context-cmd CMD] [--temperature FLOAT] [--max-tokens INT] [--json] [prompt...]
EOF
}

root="$DEFAULT_ROOT"
action="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

  case "${action}" in
  help|-h|--help)
    print_usage
    ;;
  init)
    AGENT_NAME="${AGENT_NAME:-${DEFAULT_NAME}}"
    AGENT_SESSION="${AGENT_SESSION:-${DEFAULT_SESSION}}"
    AGENT_MODEL_DIR="${AGENT_MODEL_DIR:-${DEFAULT_MODEL_DIR}}"
    AGENT_MODEL_PATH="${AGENT_MODEL_PATH:-${DEFAULT_MODEL_PATH}}"
    AGENT_MODEL_KIND="${AGENT_MODEL_KIND:-${DEFAULT_MODEL_KIND}}"
    AGENT_MODEL_ID="${AGENT_MODEL_ID:-${DEFAULT_MODEL_ID}}"
    AGENT_RUNTIME_CMD="${AGENT_RUNTIME_CMD:-${DEFAULT_RUNTIME_CMD}}"
    AGENT_LOG_DIR="${AGENT_LOG_DIR:-${DEFAULT_LOG_DIR}}"
    AGENT_PORT="${AGENT_PORT:-${DEFAULT_PORT}}"
    AGENT_HOST="${AGENT_HOST:-${DEFAULT_HOST}}"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root)
          root="$2"
          shift 2
          ;;
        --name)
          AGENT_NAME="$2"
          shift 2
          ;;
        --session)
          AGENT_SESSION="$2"
          shift 2
          ;;
        --model-dir)
          AGENT_MODEL_DIR="$2"
          shift 2
          ;;
        --model-path)
          AGENT_MODEL_PATH="$2"
          shift 2
          ;;
        --model-kind)
          AGENT_MODEL_KIND="$2"
          shift 2
          ;;
        --model-id)
          AGENT_MODEL_ID="$2"
          shift 2
          ;;
        --runtime-cmd)
          AGENT_RUNTIME_CMD="$2"
          shift 2
          ;;
        --log-dir)
          AGENT_LOG_DIR="$2"
          shift 2
          ;;
        --port)
          AGENT_PORT="$2"
          shift 2
          ;;
        --host)
          AGENT_HOST="$2"
          shift 2
          ;;
        *)
          echo "Usage: titan agent init [--root PATH] [--name NAME] [--session NAME] [--model-dir PATH] [--model-path PATH] [--model-kind gguf|hf] [--model-id REPO_ID] [--runtime-cmd CMD] [--log-dir PATH] [--port PORT]" >&2
          exit 1
          ;;
      esac
    done
    AGENT_ENV_FILE="${root}/agent.env"
    init_workspace "${root}" "${AGENT_NAME}" "${AGENT_SESSION}" "${AGENT_MODEL_DIR}" "${AGENT_MODEL_PATH}" "${AGENT_MODEL_KIND}" "${AGENT_MODEL_ID}" "${AGENT_RUNTIME_CMD}" "${AGENT_LOG_DIR}" "${AGENT_HOST}" "${AGENT_PORT}"
    ;;
  status)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root)
          root="$2"
          shift 2
          ;;
        --log-dir)
          AGENT_LOG_DIR="$2"
          shift 2
          ;;
        --json)
          shift
          AGENT_ENV_FILE="${root}/agent.env"
          load_config
          resolve_defaults
          status_json "${root}" "${AGENT_ENV_FILE}" "${AGENT_NAME}" "${AGENT_SESSION}" "${AGENT_MODEL_DIR}" "${AGENT_MODEL_PATH}" "${AGENT_MODEL_KIND:-${DEFAULT_MODEL_KIND}}" "${AGENT_MODEL_ID:-${DEFAULT_MODEL_ID}}" "${AGENT_RUNTIME_CMD}" "${AGENT_LOG_DIR}" "${AGENT_HOST}" "${AGENT_PORT}"
          exit 0
          ;;
        *)
          echo "Usage: titan agent status [--root PATH] [--json]" >&2
          exit 1
          ;;
      esac
    done
    status_text "${root}"
    ;;
  serve)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root)
          root="$2"
          shift 2
          ;;
        --log-dir)
          AGENT_LOG_DIR="$2"
          shift 2
          ;;
        *)
          break
          ;;
      esac
    done
    serve_agent "${root}" "$@"
    ;;
  stop)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root)
          root="$2"
          shift 2
          ;;
        --log-dir)
          AGENT_LOG_DIR="$2"
          shift 2
          ;;
        *)
          echo "Usage: titan agent stop [--root PATH]" >&2
          exit 1
          ;;
      esac
    done
    AGENT_ENV_FILE="${root}/agent.env"
    load_config
    resolve_defaults
    stop_agent "${AGENT_SESSION}"
    ;;
  logs)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root)
          root="$2"
          shift 2
          ;;
        --log-dir)
          AGENT_LOG_DIR="$2"
          shift 2
          ;;
        *)
          echo "Usage: titan agent logs [--root PATH]" >&2
          exit 1
          ;;
      esac
    done
    AGENT_ENV_FILE="${root}/agent.env"
    load_config
    resolve_defaults
    latest_log="$(latest_log "${AGENT_LOG_DIR}" "${AGENT_SESSION}")"
    if [[ -z "${latest_log}" ]]; then
      echo "No logs found for session ${AGENT_SESSION}." >&2
      exit 1
    fi
    exec tail -f "${latest_log}"
    ;;
  prompt)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root)
          root="$2"
          shift 2
          ;;
        --log-dir)
          AGENT_LOG_DIR="$2"
          shift 2
          ;;
        *)
          break
          ;;
      esac
    done
    prompt_agent "${root}" "$@"
    ;;
  chat)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root)
          root="$2"
          shift 2
          ;;
        --log-dir)
          AGENT_LOG_DIR="$2"
          shift 2
          ;;
        *)
          break
          ;;
      esac
    done
    prompt_agent "${root}" "$@"
    ;;
  tasks)
    tasks_root="${root:-/ai/projects/titan-local-agent}"
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root) tasks_root="$2"; shift 2 ;;
        *) break ;;
      esac
    done
    agent_tasks "${tasks_root}"
    ;;
  tool)
    agent_tool "$@"
    ;;
  *)
    echo "Unknown subcommand: ${action}" >&2
    print_usage >&2
    exit 1
    ;;
esac
