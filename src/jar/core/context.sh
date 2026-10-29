#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/titan_env.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The sibling titan.sh first, like doctor.sh/protocol_ops.sh: an installed `titan` on PATH can be
# months stale (on titan, /usr/local/bin/titan from March reported live runtimes as "not live").
if [[ -z "${TITAN_CLI:-}" ]]; then
  if [[ -x "${SCRIPT_DIR}/titan.sh" ]]; then
    TITAN_CLI="${SCRIPT_DIR}/titan.sh"
  else
    TITAN_CLI="$(command -v titan 2>/dev/null || echo titan)"
  fi
fi

usage() {
  cat <<'EOF2'
Usage:
  titan context [--json]
EOF2
}


json_mode=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json)
      json_mode=1
      shift
      ;;
    --help|-h|help)
      usage
      exit 0
      ;;
    *)
      echo "Usage: titan context [--json]" >&2
      exit 1
      ;;
  esac
done

python3 - "${json_mode}" "${SCRIPT_DIR}" "${RUNTIME_REGISTRY_FILE}" "${PROTOCOL_MATRIX_FILE}" "${TITAN_CLI}" "$(bridge_host)" <<'PY'
import json
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

json_mode = sys.argv[1] == "1"
script_dir = Path(sys.argv[2])
runtime_registry_path = Path(sys.argv[3])
protocol_matrix_path = Path(sys.argv[4])
titan_cli = sys.argv[5]
bridge_host = sys.argv[6]

runtime_registry = json.loads(runtime_registry_path.read_text(encoding="utf-8"))
protocol_matrix = json.loads(protocol_matrix_path.read_text(encoding="utf-8"))

warnings = []
runtime_warnings = {
    "posture-runtime": {
        "code": "RUNTIME_AUTH_GATED",
        "message": "health endpoint is auth-gated and returns 401 without coordinator credentials",
        "next_action": "Provide the coordinator/auth token for a full authenticated health check.",
    },
}
for runtime in runtime_registry.get("runtimes", []):
    name = runtime.get("name")
    if name in runtime_warnings and runtime.get("auth", "none") != "none":
        entry = {"surface": name, "kind": "runtime", "severity": "warn"}
        entry.update(runtime_warnings[name])
        warnings.append(entry)
for doc in protocol_matrix.get("proposal_docs", []):
    warnings.append({
        "surface": doc.get("name"),
        "kind": "proposal",
        "severity": "warn",
        "code": "PROPOSAL_SURFACE_NOT_LIVE",
        "message": f"{Path(doc.get('doc_path', '')).name} is documented but not live",
        "next_action": "Design or implement the runtime before expecting endpoint-level checks.",
    })

context = {
    "titan_host": bridge_host,
    "runtime_access_mode": "titan_bridge",
    "current_time_local": datetime.now().astimezone().isoformat(timespec="seconds"),
    "current_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "warnings": warnings,
    "registry": {
        "runtimes": runtime_registry.get("runtimes", []),
    },
    "tool_surface": {
        "agent_commands": [
            "titan agent prompt --router parametric",
            "titan agent chat --router parametric",
            "titan resource snapshot --json",
            "titan resource pick",
            "titan doctor",
            "titan protocol report --json",
        ],
        "bridge_commands": [
        ],
        "runtime_commands": [
            "titan runtime list",
            "titan runtime status <name>",
            "titan runtime deploy <name>",
        ],
    },
}

try:
    runtime_list = subprocess.check_output([titan_cli, "runtime", "list", "--json"], text=True)
    context["live_runtime_list"] = json.loads(runtime_list)
except Exception as exc:
    context["live_runtime_list_error"] = str(exc)

if json_mode:
    print(json.dumps(context, indent=2, sort_keys=True))
else:
    print(f"titan_host={context['titan_host']}")
    print(f"runtime_access_mode={context['runtime_access_mode']}")
    print(f"current_time_local={context['current_time_local']}")
    print(f"current_time_utc={context['current_time_utc']}")
    print(f"warning_count={len(context['warnings'])}")
    if context['warnings']:
        warning_surfaces = ", ".join(sorted({item.get("surface", "unknown") for item in context["warnings"] if isinstance(item, dict)}))
        print(f"warning_surfaces={warning_surfaces}")
    print(f"runtime_count={len(context['registry']['runtimes'])}")
    print("available_commands=")
    for cmd in context["tool_surface"]["agent_commands"]:
        print(f"- {cmd}")
PY
