#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${TITAN_CLI:-}" ]]; then
  if [[ -x "${SCRIPT_DIR}/titan.sh" ]]; then
    TITAN_CLI="${SCRIPT_DIR}/titan.sh"
  else
    TITAN_CLI="$(command -v titan 2>/dev/null || true)"
  fi
fi
REGISTRY_FILE="${RUNTIME_REGISTRY_FILE:-${SCRIPT_DIR}/runtime-registry.json}"

check_registry() {
  python3 - "${REGISTRY_FILE}" <<'PY'
import json
import sys
from pathlib import Path

registry = Path(sys.argv[1])
data = json.loads(registry.read_text(encoding="utf-8"))
runtimes = data.get("runtimes", [])
if not runtimes:
    raise SystemExit("runtime registry is empty")
names = set()
ports = set()
for item in runtimes:
    name = item.get("name")
    port = item.get("port")
    if not name:
        raise SystemExit("runtime registry entry missing name")
    if port is None:
        raise SystemExit(f"runtime registry entry missing port for {name}")
    if name in names:
        raise SystemExit(f"duplicate runtime name: {name}")
    if port in ports:
        raise SystemExit(f"duplicate runtime port: {port}")
    names.add(name)
    ports.add(port)
print(f"registry_runtimes={len(runtimes)}")
print(f"registry_ports={len(ports)}")
PY
}

check_runtime_list() {
  python3 - "${REGISTRY_FILE}" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path

registry = Path(sys.argv[1])
data = json.loads(registry.read_text(encoding="utf-8"))
expected = {(item["name"], str(item["port"])) for item in data["runtimes"]}
cli = os.environ.get("TITAN_CLI")
if not cli:
    raise SystemExit("TITAN_CLI is not set")
completed = subprocess.run([cli, "runtime", "list", "--json"], text=True, capture_output=True, timeout=10)
if completed.returncode != 0:
    raise SystemExit(completed.stderr.strip() or completed.stdout.strip() or f"runtime list failed with exit code {completed.returncode}")
payload = json.loads(completed.stdout)
actual = {(item["name"], str(item["port"])) for item in payload}
missing = expected - actual
extra = actual - expected
if missing:
    raise SystemExit(f"runtime list missing entries: {sorted(missing)}")
if extra:
    raise SystemExit(f"runtime list has extra entries: {sorted(extra)}")
print("runtime_list=ok")
PY
}

main() {
  local json_mode="0"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json)
        json_mode="1"
        shift
        ;;
      *)
        echo "Usage: titan doctor [--json]" >&2
        exit 1
        ;;
    esac
  done

  local registry_summary runtime_summary smoke_summary protocol_output protocol_json
  registry_summary="$(check_registry)"
  runtime_summary="$(TITAN_CLI="${TITAN_CLI}" check_runtime_list)"
  smoke_summary="$("${SCRIPT_DIR}/test_agent_bridge.sh" >/dev/null && printf 'agent_smoke=ok')"

  if [[ "${json_mode}" == "1" ]]; then
    protocol_json="$(mktemp)"
    "${SCRIPT_DIR}/protocol_ops.sh" report --json --quick > "${protocol_json}"
    python3 - "${registry_summary}" "${runtime_summary}" "${smoke_summary}" "${protocol_json}" <<'PY'
import json
import sys
from pathlib import Path

def parse_kv(text):
    result = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result

registry = parse_kv(sys.argv[1])
runtime = parse_kv(sys.argv[2])
smoke = parse_kv(sys.argv[3])
protocol = json.loads(Path(sys.argv[4]).read_text())
print(json.dumps({
    "registry": registry,
    "runtime_list": runtime,
    "agent_smoke": smoke,
    "protocol": protocol,
}, indent=2))
PY
    rm -f "${protocol_json}"
    exit 0
  fi

  protocol_output="$("${SCRIPT_DIR}/protocol_ops.sh" check --quick)"

  printf '%s\n' "${registry_summary}"
  printf '%s\n' "${runtime_summary}"
  printf '%s\n' "${smoke_summary}"
  printf '%s\n' "${protocol_output}"
  printf 'doctor_status=ok\n'
}

main "$@"
