#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage:
  titan resource snapshot [--json]
  titan resource pick [--min-free-mb MB] [--max-util PERCENT] [--json]
  titan resource run [--min-free-mb MB] [--max-util PERCENT] <command...>
  titan resource tmux <session_name> [--min-free-mb MB] [--max-util PERCENT] <command...>
  titan resource schedule <session_name> --delay-seconds N [--min-free-mb MB] [--max-util PERCENT] <command...>
  titan resource help

The resource broker is the canonical Titan host inventory and selection surface.
It tracks GPU availability, disk headroom, and a simple policy for choosing the
best open compute target on the node.
EOF
}

snapshot_json() {
  python3 - <<'PY'
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path

def query_gpus():
    if shutil.which("nvidia-smi") is None:
        return []
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,name,memory.free,memory.total,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    # A failing nvidia-smi (e.g. driver/library mismatch after an unrebooted
    # driver upgrade) used to raise CalledProcessError and crash every caller,
    # including `titan.sh status --json`. Report the fault as data instead.
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return {"error": "nvidia-smi failed",
                "returncode": proc.returncode,
                "detail": detail[0] if detail else ""}
    out = proc.stdout.strip()
    gpus = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        gpus.append({
            "index": int(parts[0]),
            "name": parts[1],
            "memory_free_mb": int(parts[2]),
            "memory_total_mb": int(parts[3]),
            "utilization_pct": int(parts[4]),
        })
    return gpus

def disk_usage(path):
    try:
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "total_mb": int(usage.total / (1024 * 1024)),
            "used_mb": int(usage.used / (1024 * 1024)),
            "free_mb": int(usage.free / (1024 * 1024)),
        }
    except Exception:
        return None

snapshot = {
    "host": socket.gethostname(),
    "cpu_count": os.cpu_count() or 0,
    "gpus": query_gpus(),
    "disk": disk_usage("/ai") or disk_usage("/"),
    "timestamp": __import__("datetime").datetime.utcnow().isoformat(timespec="seconds") + "Z",
}
print(json.dumps(snapshot, indent=2, sort_keys=True))
PY
}

snapshot_text() {
  local snapshot_json
  snapshot_json="$("$0" snapshot --json)"
  python3 - "${snapshot_json}" <<'PY'
import json
import sys

snapshot = json.loads(sys.argv[1])
print(f"host={snapshot['host']}")
print(f"cpu_count={snapshot['cpu_count']}")
gpus = snapshot["gpus"]
if isinstance(gpus, dict) and "error" in gpus:
    print(f"gpu_count=unavailable ({gpus['error']}: {gpus.get('detail', '')})")
else:
    print(f"gpu_count={len(gpus)}")
disk = snapshot.get("disk")
if disk:
    print(f"disk_path={disk['path']}")
    print(f"disk_free_mb={disk['free_mb']}")
PY
}

# Prints one of:
#   gpu:N        a GPU met the policy
#   cpu          no GPU met the policy, or this host genuinely has no GPU
#   unavailable  nvidia-smi is installed but failing (driver fault)
#
# The third case used to be indistinguishable from the second: a failing
# nvidia-smi produced empty input, awk found no candidate, and the function
# printed "cpu" with exit 0. GPU jobs then ran on CPU silently. Callers that
# asked for a GPU must treat "unavailable" as an error.
pick_target() {
  local min_free_mb="${1:-0}"
  local max_util="${2:-100}"
  if command -v nvidia-smi >/dev/null 2>&1; then
    local smi_out smi_rc=0
    smi_out="$(nvidia-smi --query-gpu=index,memory.free,utilization.gpu --format=csv,noheader,nounits 2>&1)" || smi_rc=$?
    if (( smi_rc != 0 )); then
      echo "resource-broker: nvidia-smi failed (exit ${smi_rc}): ${smi_out%%$'\n'*}" >&2
      printf 'unavailable\n'
      return 3
    fi
    local gpu_idx
    if gpu_idx="$(
      printf '%s\n' "${smi_out}" |
      awk -F', ' -v min_free="$min_free_mb" -v max_util="$max_util" '
        BEGIN { best_idx = -1; best_mem = -1; best_util = 101 }
        {
          idx = $1 + 0; mem = $2 + 0; util = $3 + 0
          if (mem < min_free) next
          if (util > max_util) next
          if (mem > best_mem || (mem == best_mem && util < best_util)) {
            best_idx = idx; best_mem = mem; best_util = util
          }
        }
        END {
          if (best_idx < 0) exit 2
          print best_idx
        }
      '
    )"; then
      printf 'gpu:%s\n' "${gpu_idx}"
      return
    fi
  fi
  printf 'cpu\n'
}

# Abort when the caller asked for a GPU but the node cannot provide one.
# TITAN_ALLOW_CPU_FALLBACK=1 opts back into the old silent-CPU behaviour.
require_usable_target() {
  local target="$1" min_free_mb="${2:-0}"
  if [[ "${TITAN_ALLOW_CPU_FALLBACK:-0}" == "1" ]]; then
    return 0
  fi
  if [[ "${target}" == "unavailable" ]]; then
    cat >&2 <<'EOF'
resource-broker: refusing to launch — the GPU driver is not usable.
  nvidia-smi is installed but failing. A common cause is a driver package
  upgrade without a reboot (check /var/run/reboot-required and compare
  `cat /proc/driver/nvidia/version` against the NVML library version).
  Re-run with TITAN_ALLOW_CPU_FALLBACK=1 to run on CPU anyway.
EOF
    exit 3
  fi
  if [[ "${target}" == "cpu" && "${min_free_mb}" -gt 0 ]]; then
    cat >&2 <<EOF
resource-broker: refusing to launch — no GPU has ${min_free_mb} MB free.
  Re-run with TITAN_ALLOW_CPU_FALLBACK=1 to run on CPU anyway.
EOF
    exit 3
  fi
  return 0
}

main() {
  local subcommand="${1:-snapshot}"
  shift || true
  case "${subcommand}" in
    help|-h|--help)
      usage
      ;;
    snapshot)
      local json_mode="0"
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --json)
            json_mode="1"
            shift
            ;;
          *)
            echo "Usage: titan resource snapshot [--json]" >&2
            exit 1
            ;;
        esac
      done
      if [[ "${json_mode}" == "1" ]]; then
        snapshot_json
      else
        snapshot_text
      fi
      ;;
    pick)
      local min_free_mb=0
      local max_util=100
      local json_mode="0"
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --min-free-mb)
            min_free_mb="$2"
            shift 2
            ;;
          --max-util)
            max_util="$2"
            shift 2
            ;;
          --json)
            json_mode="1"
            shift
            ;;
          *)
            echo "Usage: titan resource pick [--min-free-mb MB] [--max-util PERCENT] [--json]" >&2
            exit 1
            ;;
        esac
      done
      local target
      # `pick` reports; it never aborts. Callers that need a GPU use run/tmux,
      # which enforce the policy via require_usable_target.
      target="$(pick_target "${min_free_mb}" "${max_util}")" || true
      if [[ "${json_mode}" == "1" ]]; then
        python3 - "${target}" "${min_free_mb}" "${max_util}" <<'PY'
import json
import sys
print(json.dumps({"target": sys.argv[1], "min_free_mb": int(sys.argv[2]), "max_util": int(sys.argv[3])}, indent=2))
PY
      else
        printf '%s\n' "${target}"
      fi
      ;;
    run)
      local min_free_mb=0
      local max_util=100
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --min-free-mb) min_free_mb="$2"; shift 2 ;;
          --max-util) max_util="$2"; shift 2 ;;
          --) shift; break ;;
          *) break ;;
        esac
      done
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan resource run [--min-free-mb MB] [--max-util PERCENT] <command...>" >&2
        exit 1
      fi
      local target gpu=""
      target="$(pick_target "${min_free_mb}" "${max_util}")" || true
      require_usable_target "${target}" "${min_free_mb}"
      if [[ "${target}" == gpu:* ]]; then
        gpu="${target#gpu:}"
        echo "Using GPU ${gpu}" >&2
      fi
      export CUDA_VISIBLE_DEVICES="${gpu}"
      export TITAN_NODE_GPU="${gpu}"
      exec "$@"
      ;;
    tmux)
      if [[ $# -lt 2 ]]; then
        echo "Usage: titan resource tmux <session_name> [--min-free-mb MB] [--max-util PERCENT] <command...>" >&2
        exit 1
      fi
      local session_name="$1"
      shift
      local min_free_mb=0
      local max_util=100
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --min-free-mb) min_free_mb="$2"; shift 2 ;;
          --max-util) max_util="$2"; shift 2 ;;
          --) shift; break ;;
          *) break ;;
        esac
      done
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan resource tmux <session_name> [--min-free-mb MB] [--max-util PERCENT] <command...>" >&2
        exit 1
      fi
      mkdir -p /ai/logs
      local target gpu="" stamp log_file command_str
      target="$(pick_target "${min_free_mb}" "${max_util}")" || true
      require_usable_target "${target}" "${min_free_mb}"
      if [[ "${target}" == gpu:* ]]; then
        gpu="${target#gpu:}"
      fi
      stamp="$(date -u +%Y%m%d-%H%M%SZ)"
      log_file="/ai/logs/${session_name}-${stamp}.log"
      command_str="$(printf '%q ' "$@")"
      command tmux new-session -d -s "${session_name}" \
        "export CUDA_VISIBLE_DEVICES=${gpu}; export TITAN_NODE_GPU=${gpu}; echo \"[$(date -Is)] session=${session_name} gpu=${gpu}\" | tee -a ${log_file}; ${command_str} 2>&1 | tee -a ${log_file}"
      echo "session=${session_name}"
      echo "gpu=${gpu}"
      echo "log=${log_file}"
      ;;
    schedule)
      if [[ $# -lt 2 ]]; then
        echo "Usage: titan resource schedule <session_name> --delay-seconds N [--min-free-mb MB] [--max-util PERCENT] <command...>" >&2
        exit 1
      fi
      local session_name="$1"
      shift
      local delay_seconds=0
      local min_free_mb=0
      local max_util=100
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --delay-seconds) delay_seconds="$2"; shift 2 ;;
          --min-free-mb) min_free_mb="$2"; shift 2 ;;
          --max-util) max_util="$2"; shift 2 ;;
          --) shift; break ;;
          *) break ;;
        esac
      done
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan resource schedule <session_name> --delay-seconds N [--min-free-mb MB] [--max-util PERCENT] <command...>" >&2
        exit 1
      fi
      local command_str
      command_str="$(printf '%q ' "$@")"
      command tmux new-session -d -s "${session_name}" \
        "echo 'Sleeping ${delay_seconds} seconds before choosing GPU...'; sleep ${delay_seconds}; exec \"${SCRIPT_DIR}/resource_broker.sh\" tmux \"${session_name}\" --min-free-mb \"${min_free_mb}\" --max-util \"${max_util}\" ${command_str}"
      echo "scheduled_session=${session_name}"
      echo "delay_seconds=${delay_seconds}"
      ;;
    *)
      echo "Usage: titan resource <snapshot|pick|run|tmux|schedule|help>" >&2
      exit 1
      ;;
  esac
}

main "$@"
