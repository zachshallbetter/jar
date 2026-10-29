#!/usr/bin/env bash
set -euo pipefail

AI_ROOT="${AI_ROOT:-/ai}"

if [[ "${1:-}" == "--json" ]]; then
  python3 - <<'PY' "${AI_ROOT}"
import json
import shutil
import subprocess
import sys
from pathlib import Path

ai_root = Path(sys.argv[1])

def command_present(name: str) -> bool:
    return shutil.which(name) is not None

def service_state(name: str) -> str:
    if shutil.which("systemctl") is None:
        return "systemctl-missing"
    unit_lookup = subprocess.run(
        ["systemctl", "list-unit-files", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if unit_lookup.returncode != 0:
        return "not-installed"
    active = subprocess.run(
        ["systemctl", "is-active", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return (active.stdout or active.stderr).strip() or "unknown"

payload = {
    "hostname": subprocess.run(["hostname"], capture_output=True, text=True, check=True).stdout.strip(),
    "time": subprocess.run(["date", "-Iseconds"], capture_output=True, text=True, check=True).stdout.strip(),
    "uptime": subprocess.run(["uptime"], capture_output=True, text=True, check=True).stdout.strip(),
    "commands": {
        "nvidia-smi": command_present("nvidia-smi"),
        "tmux": command_present("tmux"),
        "python3": command_present("python3"),
        "jq": command_present("jq"),
    },
    "services": {
        "ssh": service_state("ssh"),
        "titan-gpu-fan-policy.service": service_state("titan-gpu-fan-policy.service"),
        "titan-gpu-init.service": service_state("titan-gpu-init.service"),
    },
    "paths": {},
    "disk_usage": [],
    "gpus": [],
    "recent_logs": [],
}

for path in [ai_root, ai_root / "envs", ai_root / "logs", ai_root / "tmp", ai_root / "projects"]:
    payload["paths"][str(path)] = path.is_dir()

for mount in [Path("/"), ai_root]:
    result = subprocess.run(["df", "-Pk", str(mount)], capture_output=True, text=True, check=False)
    lines = result.stdout.strip().splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        payload["disk_usage"].append(
            {
                "mount": parts[5],
                "used_kb": int(parts[2]),
                "avail_kb": int(parts[3]),
                "use_percent": parts[4],
            }
        )

logs_dir = ai_root / "logs"
if logs_dir.is_dir():
    recent = sorted(
        [p for p in logs_dir.glob("*") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:5]
    payload["recent_logs"] = [p.name for p in recent]

if command_present("nvidia-smi"):
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 7:
            continue
        payload["gpus"].append(
            {
                "index": int(parts[0]),
                "name": parts[1],
                "temperature_c": int(parts[2]),
                "utilization_gpu_percent": int(parts[3]),
                "memory_used_mb": int(parts[4]),
                "memory_total_mb": int(parts[5]),
                "power_draw_w": float(parts[6]),
            }
        )

print(json.dumps(payload, indent=2))
PY
  exit 0
fi

command_status() {
  local name="$1"
  if command -v "${name}" >/dev/null 2>&1; then
    echo "command[${name}]=present"
  else
    echo "command[${name}]=missing"
  fi
}

service_status() {
  local name="$1"
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "service[${name}]=systemctl-missing"
    return
  fi

  if systemctl list-unit-files "${name}" >/dev/null 2>&1; then
    echo "service[${name}]=$(systemctl is-active "${name}" 2>/dev/null || true)"
    return
  fi

  echo "service[${name}]=not-installed"
}

echo "hostname=$(hostname)"
echo "time=$(date -Iseconds)"
echo "uptime=$(uptime | sed 's/^ *//')"

command_status nvidia-smi
command_status tmux
command_status python3
command_status jq

service_status ssh
service_status titan-gpu-fan-policy.service
service_status titan-gpu-init.service

for path in "${AI_ROOT}" "${AI_ROOT}/envs" "${AI_ROOT}/logs" "${AI_ROOT}/tmp" "${AI_ROOT}/projects"; do
  if [[ -d "${path}" ]]; then
    echo "path[${path}]=present"
  else
    echo "path[${path}]=missing"
  fi
done

if command -v nvidia-smi >/dev/null 2>&1; then
  echo "gpu_status:"
  "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/gpu_status.sh"
fi

echo "disk_usage:"
df -Pk / 2>/dev/null | awk 'NR==1 { next } { printf "  mount=%s used_kb=%s avail_kb=%s use_percent=%s\n", $6, $3, $4, $5 }'
if [[ -d "${AI_ROOT}" ]]; then
  df -Pk "${AI_ROOT}" 2>/dev/null | awk 'NR==1 { next } { printf "  mount=%s used_kb=%s avail_kb=%s use_percent=%s\n", $6, $3, $4, $5 }'
fi

if [[ -d "${AI_ROOT}/logs" ]]; then
  echo "recent_logs:"
  find "${AI_ROOT}/logs" -maxdepth 1 -type f -printf '%T@ %f\n' 2>/dev/null \
    | sort -nr \
    | head -n 5 \
    | awk '{ $1=""; sub(/^ /, ""); printf "  %s\n", $0 }'
else
  echo "recent_logs=unavailable"
fi
