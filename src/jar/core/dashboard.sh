#!/usr/bin/env bash
set -euo pipefail

AI_ROOT="${AI_ROOT:-/ai}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--json" ]]; then
  python3 - <<'PY' "${SCRIPT_DIR}" "${AI_ROOT}"
import json
import shutil
import subprocess
import sys
from pathlib import Path

script_dir = Path(sys.argv[1])
ai_root = Path(sys.argv[2])

health = subprocess.run(
    ["bash", str(script_dir / "health_check.sh"), "--json"],
    capture_output=True,
    text=True,
    check=True,
)
payload = json.loads(health.stdout)
dashboard = {
    "host": payload.get("hostname"),
    "time": payload.get("time"),
    "health": payload,
    "tmux_sessions": [],
    "top_logs": [],
}

if shutil.which("tmux") is not None:
    tmux = subprocess.run(["tmux", "ls"], capture_output=True, text=True, check=False)
    if tmux.returncode == 0:
        dashboard["tmux_sessions"] = tmux.stdout.strip().splitlines()

logs_dir = ai_root / "logs"
if logs_dir.is_dir():
    dashboard["top_logs"] = [
        p.name
        for p in sorted(
            [p for p in logs_dir.glob("*") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:10]
    ]

print(json.dumps(dashboard, indent=2))
PY
  exit 0
fi

echo "Titan Node Dashboard"
echo "===================="
echo "host: $(hostname)"
echo "time: $(date -Iseconds)"
echo

"$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/health_check.sh"
echo

echo "tmux_sessions:"
if command -v tmux >/dev/null 2>&1 && tmux ls >/dev/null 2>&1; then
  tmux ls | sed 's/^/  /'
else
  echo "  none"
fi

echo
echo "top_logs:"
if [[ -d "${AI_ROOT}/logs" ]]; then
  find "${AI_ROOT}/logs" -maxdepth 1 -type f -printf '%T@ %f\n' 2>/dev/null \
    | sort -nr \
    | head -n 10 \
    | awk '{ $1=""; sub(/^ /, ""); printf "  %s\n", $0 }'
else
  echo "  /ai/logs missing"
fi
