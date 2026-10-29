#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${INSTALL_DIR}"
if [[ -d "${INSTALL_DIR}/scripts" ]]; then
  SCRIPTS_DIR="${INSTALL_DIR}/scripts"
else
  SCRIPTS_DIR="${INSTALL_DIR}"
fi
TITAN_HOST="${TITAN_HOST:-titan}"
TITAN_AGENT_REMOTE_ROOT="${TITAN_AGENT_REMOTE_ROOT:-/ai/projects/titan-node}"
TITAN_SSH_CONFIG="${TITAN_SSH_CONFIG:-$HOME/.ssh/config}"
TITAN_NODE_HOSTNAME="${TITAN_NODE_HOSTNAME:-titan-node}"

# Pulls in resolved_titan_host() (real ssh -G -F alias resolution) so remote
# hops can be preflighted below instead of blindly ssh-ing an unresolved alias.
if [[ -f "${SCRIPTS_DIR}/titan_env.sh" ]]; then
  # shellcheck source=./titan_env.sh
  source "${SCRIPTS_DIR}/titan_env.sh"
fi

print_usage() {
  cat <<'EOF'
Usage:
  titan install [--prefix PATH]
  titan status
  titan status --json
  titan dashboard
  titan dashboard --json
  titan health
  titan health --json
  titan pick [--min-free-mb MB] [--max-util PERCENT]
  titan resource <subcommand...>
  titan run [--min-free-mb MB] [--max-util PERCENT] <command...>
  titan tmux <session_name> [--min-free-mb MB] [--max-util PERCENT] <command...>
  titan schedule <session_name> --delay-seconds N [--min-free-mb MB] [--max-util PERCENT] <command...>
  titan cleanup [report|prune] [--logs-days N] [--tmp-days N] [--apply]
  titan kb import FILE [--apply] [--include-secrets]
  titan skillopt baseline SKILL [--candidate FILE] [-v]
  titan skillopt run SKILL [--epochs N] [--smoke]
  titan doctor [--json]
  titan boot [check|apply|install] [--json]
  titan context [--json]
  titan protocol [check|report] [--json]
  titan service <subcommand...>
  titan runtime <subcommand...>
  titan project <subcommand...>
  titan connect <subcommand...>
  titan agent <subcommand...>
  titan gateway [--host HOST] [--port PORT] [--backend-url URL ...]
  titan sessions
  titan attach <session_name>
  titan logs [session_name]
  titan watch
  titan help

Subcommands:
  install    Install titan and the helper scripts into a prefix.
  status     Print compact GPU status via nvidia-smi.
  dashboard  Print a richer node dashboard: health, disks, tmux, recent logs.
  health     Print compact node health checks for commands, services, disks, and GPUs.
  pick       Return the preferred GPU index.
  resource   Inspect or pick the current Titan host resource target.
  run        Run a command immediately on the selected GPU.
  tmux       Launch a long-running command in a named tmux session and log to /ai/logs.
  schedule   Launch a delayed tmux job that chooses a GPU when the delay expires.
  cleanup    Report or prune aged /ai/logs and /ai/tmp entries.
  kb         Load household.yaml into the knowledge base (dry run unless --apply).
  skillopt   Score a household skill on its task bank, or optimize it with SkillOpt (local model only).
  boot       Check or enable reboot/brownout auto-start units (hops to titan from the Mac).
  context    Print a live Titan runtime/tool snapshot for retrieval and operator prompts.
  service    Scaffold and inspect Titan-hosted database and file-storage services.
  runtime    Sync, bootstrap, and launch the Runtimes workspace on Titan.
  project    Manifest-driven project bootstrap, bridge, storage, and doctor helpers.
  connect    Inspect, write, and verify Titan SSH connectivity for away-mode use.
  agent      Initialize, inspect, serve, stop, or tail a Titan-managed local agent workspace.
  gateway    Run the model gateway server.
  sessions   List tmux sessions.
  attach     Attach to an existing tmux session.
  logs       List recent logs, or tail logs for a session name.
  watch      Continuously refresh GPU status with watch.
  help       Show this help text.

Examples:
  titan install
  titan status
  titan status --json
  titan dashboard
  titan dashboard --json
  titan health
  titan health --json
  titan pick --min-free-mb 8000
  titan resource snapshot --json
  titan resource pick --min-free-mb 8000
  titan run --min-free-mb 10000 python train.py --config configs/lora.yaml
  titan tmux lora-run-01 --min-free-mb 10000 python train.py --config configs/lora.yaml
  titan schedule overnight-run --delay-seconds 3600 --min-free-mb 10000 python train.py --config configs/lora.yaml
  titan cleanup report --logs-days 14 --tmp-days 7
  titan doctor
  titan boot check
  titan context --json
  titan protocol check
  titan service init sqlite notes
  titan service init postgres app-db
  titan service init filestore exports
  titan runtime list
  titan runtime host status
  titan runtime deploy text-runtime
  titan project init orchestration-studio --root ~/Projects/Titan/Systems/orchestration-studio
  titan project bridge orchestration-studio
  titan project storage orchestration-studio
  titan project doctor orchestration-studio
  titan connect info
  titan connect config
  titan connect setup --apply
  titan connect check
  titan agent init --root /ai/projects/titan-local-agent
  titan agent status
  titan gateway --host 127.0.0.1 --port 18011 --backend-url http://127.0.0.1:18010/v1
  titan agent serve --session titan-local-agent -- /ai/tools/llama.cpp-b11259/llama-server --host 0.0.0.0 --port 8010 --model /ai/models/titan-agent-shortlist/Qwen3-8B-Q4_K_M.gguf --ctx-size 8192 --n-gpu-layers 99
  titan agent prompt --router parametric "Say hello in one short sentence."
  titan logs lora-run-01
EOF
}

ensure_tmux() {
  if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux is required for this command." >&2
    exit 1
  fi
}

ensure_watch() {
  if ! command -v watch >/dev/null 2>&1; then
    echo "watch is required for this command." >&2
    exit 1
  fi
}

is_remote_agent_context() {
  # True negation of the canonical is_titan_host_context (titan_env.sh, sourced
  # above) — previously this reimplemented the same host/hostname comparison
  # independently, which could drift out of sync with is_titan_host_context
  # (see titan_env.sh's comment on that predicate).
  ! is_titan_host_context
}

quote_args() {
  local quoted=""
  local arg
  for arg in "$@"; do
    printf -v quoted '%s%q ' "${quoted}" "${arg}"
  done
  printf '%s' "${quoted}"
}

preflight_titan_ssh_target() {
  if ! command -v resolved_titan_host >/dev/null 2>&1; then
    return 0
  fi
  local resolved
  resolved="$(resolved_titan_host 2>/dev/null || true)"
  if [[ -z "${resolved}" ]]; then
    echo "titan.sh: could not resolve SSH target for host alias '${TITAN_HOST}' (checked ${TITAN_SSH_CONFIG})." >&2
    exit 1
  fi
  if ! ssh -F "${TITAN_SSH_CONFIG}" -o BatchMode=yes -o ConnectTimeout=5 "${TITAN_HOST}" true 2>/dev/null; then
    echo "titan.sh: SSH preflight failed for '${TITAN_HOST}' (resolves to ${resolved}) via ${TITAN_SSH_CONFIG}." >&2
    echo "  Check connectivity, the ssh alias, and that the key in IdentityFile is loaded." >&2
    exit 1
  fi
}

exec_remote_agent() {
  preflight_titan_ssh_target
  local remote_subcommand="$1"
  shift || true
  local remote_root_q
  local remote_subcommand_q
  printf -v remote_root_q '%q' "${TITAN_AGENT_REMOTE_ROOT}"
  printf -v remote_subcommand_q '%q' "${remote_subcommand}"
  local remote_args
  remote_args="$(quote_args "$@")"
  # Tags a turn's origin in /ai/logs/agent-turns.jsonl (eval/test runs from the Mac stay out of the stats)
  local turn_source
  turn_source="$(printf '%s' "${AGENT_TURN_SOURCE:-user}" | tr -cd 'A-Za-z0-9_-')"
  eval_run="$(printf '%s' "${AGENT_EVAL_RUN:-}" | tr -cd 'A-Za-z0-9_-' | cut -c1-40)"   # #346: the eval's writes are stamped
  ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "cd ${remote_root_q} && TITAN_REMOTE_EXEC=1 AGENT_TURN_SOURCE=${turn_source:-user} AGENT_EVAL_RUN=${eval_run} ./scripts/titan.sh agent ${remote_subcommand_q} ${remote_args}"
}

exec_remote_subcommand() {
  preflight_titan_ssh_target
  local remote_subcommand="$1"
  shift || true
  local remote_root_q
  local remote_subcommand_q
  printf -v remote_root_q '%q' "${TITAN_AGENT_REMOTE_ROOT}"
  printf -v remote_subcommand_q '%q' "${remote_subcommand}"
  local remote_args
  remote_args="$(quote_args "$@")"
  ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "cd ${remote_root_q} && TITAN_REMOTE_EXEC=1 ./scripts/titan.sh ${remote_subcommand_q} ${remote_args}"
}

install_cli() {
  local prefix="/usr/local/bin"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --prefix)
        prefix="$2"
        shift 2
        ;;
      *)
        echo "Usage: titan install [--prefix PATH]" >&2
        exit 1
        ;;
    esac
  done

  local prefix_parent
  prefix_parent="$(dirname "${prefix}")"

  local mkdir_cmd=(mkdir -p)
  local install_cmd=(install -m 0755)
  if [[ "${EUID}" -ne 0 && ! -w "${prefix}" && ! -w "${prefix_parent}" ]]; then
    if command -v sudo >/dev/null 2>&1; then
      mkdir_cmd=(sudo mkdir -p)
      install_cmd=(sudo install -m 0755)
    else
      echo "Prefix is not writable and sudo is unavailable: ${prefix}" >&2
      exit 1
    fi
  fi

  "${mkdir_cmd[@]}" "${prefix}"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/titan.sh" "${prefix}/titan"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/gpu_status.sh" "${prefix}/gpu_status.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/select_gpu.sh" "${prefix}/select_gpu.sh"
      "${install_cmd[@]}" "${SCRIPTS_DIR}/dashboard.sh" "${prefix}/dashboard.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/health_check.sh" "${prefix}/health_check.sh"
    "${install_cmd[@]}" "${SCRIPTS_DIR}/cleanup_ai.sh" "${prefix}/cleanup_ai.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/doctor.sh" "${prefix}/doctor.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/context.sh" "${prefix}/context.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/test_agent_bridge.sh" "${prefix}/test_agent_bridge.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/test_epistemic_runtime_smoke.sh" "${prefix}/test_epistemic_runtime_smoke.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/resource_broker.sh" "${prefix}/resource_broker.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/protocol_ops.sh" "${prefix}/protocol_ops.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/service_templates.sh" "${prefix}/service_templates.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/runtime_ops.sh" "${prefix}/runtime_ops.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/project_ops.sh" "${prefix}/project_ops.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/connect_ops.sh" "${prefix}/connect_ops.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/protocol-matrix.json" "${prefix}/protocol-matrix.json"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/agent_runtime.sh" "${prefix}/agent_runtime.sh"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/agent_runtime_lib.py" "${prefix}/agent_runtime_lib.py"
  "${install_cmd[@]}" "${SCRIPTS_DIR}/local_time.py" "${prefix}/local_time.py"
  local gateway_installed=""
  if [[ -f "${SCRIPTS_DIR}/model_gateway_server.py" ]]; then
    "${install_cmd[@]}" "${SCRIPTS_DIR}/model_gateway_server.py" "${prefix}/model_gateway_server.py"
    gateway_installed=" model_gateway_server.py"
  fi
  if [[ -f "${SCRIPTS_DIR}/test_model_gateway.sh" ]]; then
    "${install_cmd[@]}" "${SCRIPTS_DIR}/test_model_gateway.sh" "${prefix}/test_model_gateway.sh"
  fi
  "${install_cmd[@]}" "${SCRIPTS_DIR}/runtime-registry.json" "${prefix}/runtime-registry.json"

  echo "installed_prefix=${prefix}"
  echo "installed=titan"
  echo "installed=dashboard.sh health_check.sh cleanup_ai.sh doctor.sh context.sh test_agent_bridge.sh test_epistemic_runtime_smoke.sh test_model_gateway.sh resource_broker.sh protocol_ops.sh service_templates.sh runtime_ops.sh  project_ops.sh connect_ops.sh  agent_runtime.sh agent_runtime_lib.py${gateway_installed} runtime-registry.json protocol-matrix.json"
}

subcommand="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "${subcommand}" in
  help|-h|--help)
    print_usage
    ;;
  install)
    install_cli "$@"
    ;;
  status)
    case "${1:-}" in
      --json)
        exec "${SCRIPTS_DIR}/gpu_status.sh" --json
        ;;
      *)
        exec "${SCRIPTS_DIR}/gpu_status.sh" "$@"
        ;;
    esac
    ;;
  dashboard)
    case "${1:-}" in
      --json)
        exec "${SCRIPTS_DIR}/dashboard.sh" --json
        ;;
      *)
        exec "${SCRIPTS_DIR}/dashboard.sh" "$@"
        ;;
    esac
    ;;
  health)
    case "${1:-}" in
      --json)
        exec "${SCRIPTS_DIR}/health_check.sh" --json
        ;;
      *)
        exec "${SCRIPTS_DIR}/health_check.sh" "$@"
        ;;
    esac
    ;;
  pick)
    exec "${SCRIPTS_DIR}/select_gpu.sh" "$@"
    ;;
  resource)
    if is_remote_agent_context; then
      exec_remote_subcommand "${subcommand}" "$@"
      exit $?
    fi
    exec "${SCRIPTS_DIR}/resource_broker.sh" "$@"
    ;;
  run)
    exec "${SCRIPTS_DIR}/resource_broker.sh" run "$@"
    ;;
  tmux)
    exec "${SCRIPTS_DIR}/resource_broker.sh" tmux "$@"
    ;;
  schedule)
    exec "${SCRIPTS_DIR}/resource_broker.sh" schedule "$@"
    ;;
  cleanup)
    exec "${SCRIPTS_DIR}/cleanup_ai.sh" "$@"
    ;;
  kb)
    kb_subcommand="${1:-}"
    shift || true
    case "${kb_subcommand}" in
      import)
        kb_file="${1:-}"
        shift || true
        if [[ -z "${kb_file}" || ! -f "${kb_file}" ]]; then
          echo "usage: titan kb import FILE [--apply] [--include-secrets]" >&2
          exit 2
        fi
        agent_env="${TITAN_AGENT_ENV:-/ai/projects/titan-local-agent/agent.env}"
        if ! is_remote_agent_context; then
          set -a; . "${agent_env}"; set +a
          exec python3 "${SCRIPTS_DIR}/kb_import.py" "${kb_file}" "$@"
        fi
        # From the Mac: the file travels over stdin, never onto the host's disk or a command line.
        preflight_titan_ssh_target
        printf -v remote_root_q '%q' "${TITAN_AGENT_REMOTE_ROOT}"
        printf -v agent_env_q '%q' "${agent_env}"
        exec ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" \
          "cd ${remote_root_q} && set -a && . ${agent_env_q} && set +a && python3 scripts/kb_import.py - $(quote_args "$@")" < "${kb_file}"
        ;;
      *)
        echo "usage: titan kb import FILE [--apply] [--include-secrets]" >&2
        exit 2
        ;;
    esac
    ;;
  skillopt)
    so_sub="${1:-}"; so_skill="${2:-}"
    shift 2 || true
    if [[ -z "${so_skill}" || ! "${so_sub}" =~ ^(baseline|run)$ ]]; then
      echo "usage: titan skillopt baseline|run SKILL [...]  (skills with task banks: scripts/skillopt/tasks/*.jsonl)" >&2
      exit 2
    fi
    so_python="${SKILLOPT_PYTHON:-/ai/datasets/envs/skillopt/bin/python}"
    if [[ "${so_sub}" == "baseline" ]]; then
      so_cmd="python3 scripts/skillopt/harness.py --skill ${so_skill}"
    else
      so_cmd="${so_python} scripts/skillopt/run.py --skill ${so_skill}"
    fi
    if ! is_remote_agent_context; then
      cd "${SCRIPTS_DIR}/.." && exec ${so_cmd} "$@"
    fi
    preflight_titan_ssh_target
    printf -v remote_root_q '%q' "${TITAN_AGENT_REMOTE_ROOT}"
    if [[ "${so_sub}" == "baseline" ]]; then
      exec ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "cd ${remote_root_q} && ${so_cmd} $(quote_args "$@")"
    fi
    # An optimization takes a while: run it detached on titan, logging to /ai/logs.
    so_log="/ai/logs/skillopt-${so_skill}-$(date -u +%Y%m%d-%H%M%SZ).log"
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" \
      "cd ${remote_root_q} && nohup ${so_cmd} $(quote_args "$@") > ${so_log} 2>&1 < /dev/null &"
    echo "skillopt ${so_skill}: running on titan; log ${so_log}; output under /ai/datasets/skillopt/${so_skill}/"
    ;;
  doctor)
    exec "${SCRIPTS_DIR}/doctor.sh" "$@"
    ;;
  boot)
    if is_remote_agent_context; then
      exec_remote_subcommand "${subcommand}" "$@"
      exit $?
    fi
    exec bash "${SCRIPTS_DIR}/boot_resilience.sh" "$@"
    ;;
  context)
    exec "${SCRIPTS_DIR}/context.sh" "$@"
    ;;
  protocol)
    exec "${SCRIPTS_DIR}/protocol_ops.sh" "$@"
    ;;
  service)
    exec "${SCRIPTS_DIR}/service_templates.sh" "$@"
    ;;
  runtime)
    exec "${SCRIPTS_DIR}/runtime_ops.sh" "$@"
    ;;
  project)
    exec "${SCRIPTS_DIR}/project_ops.sh" "$@"
    ;;
  connect)
    exec "${SCRIPTS_DIR}/connect_ops.sh" "$@"
    ;;
  gateway)
    exec python3 "${SCRIPTS_DIR}/model_gateway_server.py" "$@"
    ;;
  agent)
    if is_remote_agent_context; then
      remote_subcommand="${1:-}"
      shift || true
      case "${remote_subcommand}" in
        help|-h|--help|"")
          exec "${SCRIPTS_DIR}/agent_runtime.sh" help
          ;;
        *)
          exec_remote_agent "${remote_subcommand}" "$@"
          ;;
      esac
      exit 0
    fi
    exec "${SCRIPTS_DIR}/agent_runtime.sh" "$@"
    ;;
  sessions)
    ensure_tmux
    exec tmux ls
    ;;
  attach)
    ensure_tmux
    if [[ $# -ne 1 ]]; then
      echo "Usage: titan attach <session_name>" >&2
      exit 1
    fi
    exec tmux attach -t "$1"
    ;;
  logs)
    if [[ $# -gt 1 ]]; then
      echo "Usage: titan logs [session_name]" >&2
      exit 1
    fi
    if [[ $# -eq 0 ]]; then
      exec ls -1t /ai/logs
    fi

    session_name="$1"
    latest_log="$(find /ai/logs -maxdepth 1 -type f -name "${session_name}-*.log" | sort | tail -n 1)"
    if [[ -z "${latest_log}" ]]; then
      echo "No logs found for session ${session_name}." >&2
      exit 1
    fi
    exec tail -f "${latest_log}"
    ;;
  watch)
    ensure_watch
    exec watch -n 1 "${SCRIPTS_DIR}/gpu_status.sh"
    ;;
  perception)
    # Retired 10-03 (#722): perception-runtime and its data are gone. Household perception is #700.
    echo "titan perception was retired (#722); household perception is tracked in #700." >&2
    exit 2
    ;;
  *)
    echo "Unknown subcommand: ${subcommand}" >&2
    print_usage >&2
    exit 1
    ;;
esac
