#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/titan_env.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'EOF'
Usage:
  titan project init <project_name> [--root PATH] [--manifest PATH]
  titan project bridge <project_name> [--mode tunnel|direct] [--manifest PATH]
  titan project storage <project_name> [--manifest PATH]
  titan project doctor <project_name> [--manifest PATH]
  titan project help

Examples:
  titan project init orchestration-studio --root /Users/zachshallbetter/Projects/Sparx/Orchestration\ Studio
  titan project bridge orchestration-studio
  titan project bridge orchestration-studio --mode direct
  titan project storage orchestration-studio
  titan project doctor orchestration-studio
EOF
}

default_manifest_path() {
  printf '%s/%s.json' "${MANIFESTS_DIR}" "$1"
}


project_init() {
  local project="$1"
  local root="$(pwd)"
  local manifest_path="${2:-}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root)
        root="$2"
        shift 2
        ;;
      --manifest)
        manifest_path="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done

  if [[ -z "${manifest_path}" ]]; then
    manifest_path="$(default_manifest_path "${project}")"
  fi

  local example_manifest="${MANIFESTS_DIR}/project-bootstrap.example.json"
  if [[ ! -f "${example_manifest}" ]]; then
    echo "example manifest missing: ${example_manifest}" >&2
    exit 1
  fi
  if [[ -e "${manifest_path}" ]]; then
    echo "manifest already exists: ${manifest_path}" >&2
    exit 1
  fi

  mkdir -p "$(dirname "${manifest_path}")" "${SCRIPT_DIR}/docs/projects"
  python3 - "${example_manifest}" "${manifest_path}" "${project}" "${root}" <<'PY'
import json
import sys
from pathlib import Path

template = Path(sys.argv[1])
target = Path(sys.argv[2])
project_name = sys.argv[3]
project_root = sys.argv[4]
data = json.loads(template.read_text(encoding="utf-8"))
data["project"]["name"] = project_name
data["project"]["bridge_profile"] = project_name
data["project"]["root"] = project_root
if "shared_env" in data.get("bridge", {}):
    for item in data["bridge"]["shared_env"]:
        if item.get("name") == "COORDINATOR_ID":
            item["value"] = project_name
target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY

  local docs_path="${SCRIPT_DIR}/docs/projects/${project}.md"
  python3 - "${manifest_path}" "${docs_path}" <<'PY'
import json
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
doc = Path(sys.argv[2])
data = json.loads(manifest.read_text(encoding="utf-8"))
project = data["project"]
bridge = data.get("bridge", {})
storage = data.get("storage", {})
live = data.get("catalog", {}).get("live", [])
scaffolded = data.get("catalog", {}).get("scaffolded", [])
spec_only = data.get("catalog", {}).get("spec_only", [])

lines = [
    f"# {project['name']}",
    "",
    "Manifest-driven Titan bootstrap summary.",
    "",
    "## Project",
    "",
    f"- root: `{project['root']}`",
    f"- bridge profile: `{project['bridge_profile']}`",
    f"- kind: `{project.get('kind', 'app')}`",
    "",
    "## Runtime coverage",
    "",
    f"- live: `{len(live)}`",
    f"- scaffolded: `{len(scaffolded)}`",
    f"- spec_only: `{len(spec_only)}`",
    "",
    "## Commands",
    "",
    "```bash",
    f"eval \"$(titan project bridge {project['name']} --manifest {manifest})\"",
    f"titan project storage {project['name']} --manifest {manifest}",
    f"titan project doctor {project['name']} --manifest {manifest}",
    "```",
    "",
    "## Storage",
    "",
]

services = storage.get("services", [])
if services:
    for service in services:
        lines.append(f"- `{service['name']}` ({service['kind']}): {service.get('purpose', '')}")
else:
    lines.append("- none")

doc.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

  echo "manifest=${manifest_path}"
  echo "project_name=${project}"
  echo "project_root=${root}"
  echo "project_doc=${docs_path}"
}

project_bridge() {
  local project="$1"
  local mode="tunnel"
  local manifest_path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --mode)
        mode="$2"
        shift 2
        ;;
      --manifest)
        manifest_path="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done

  if [[ -z "${manifest_path}" ]]; then
    manifest_path="$(default_manifest_path "${project}")"
  fi
  if [[ ! -f "${manifest_path}" ]]; then
    echo "manifest not found: ${manifest_path}" >&2
    exit 1
  fi

  local host
  if [[ "${mode}" == "direct" ]]; then
    host="$(resolve_direct_host)"
  else
    host="127.0.0.1"
  fi

  python3 - "${manifest_path}" "${RUNTIME_REGISTRY_FILE}" "${mode}" "${host}" "${project}" "${manifest_path}" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
registry_path = Path(sys.argv[2])
mode = sys.argv[3]
host = sys.argv[4]
project_name = sys.argv[5]
manifest_str = sys.argv[6]

manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
registry = json.loads(registry_path.read_text(encoding="utf-8"))
registry_by_name = {item["name"]: item for item in registry.get("runtimes", []) if item.get("name")}
project = manifest["project"]
bridge = manifest.get("bridge", {})
runtimes = bridge.get("runtimes", [])
if not runtimes:
    raise SystemExit(f"bridge runtime list is empty in {manifest_path}")

print(f"# {project['name']} Bridge Profile ({mode})")
if mode == "tunnel":
    print("# Run this tunnel in a background terminal:")
    mappings = []
    for item in runtimes:
        runtime = registry_by_name.get(item["name"])
        if runtime is None:
            raise SystemExit(f"unknown runtime in bridge manifest: {item['name']}")
        local_port = item.get("tunnel_port")
        if local_port is None:
            raise SystemExit(f"missing tunnel_port for {item['name']}")
        mappings.append(f"{local_port}:127.0.0.1:{runtime['port']}")
    print("ssh -N" + "".join(f" -L {mapping}" for mapping in mappings) + f" {project.get('titan_host', 'titan')}")
    print()

print("export TITAN_RUNTIME_ACCESS_MODE=titan_bridge")
print(f"export TITAN_BRIDGE_MODE={mode}")
print(f"export TITAN_BRIDGE_HOST={host}")
print(f"export PROJECT_NAME={project_name}")
print(f"export PROJECT_MANIFEST_PATH={manifest_str}")

for shared in bridge.get("shared_env", []):
    name = shared.get("name")
    value = shared.get("value")
    if not name:
        raise SystemExit("shared_env entry missing name")
    if value is None:
        raise SystemExit(f"shared_env entry missing value for {name}")
    print(f"export {name}={value!r}")

for item in runtimes:
    name = item.get("name")
    env = item.get("env")
    local_port = item.get("tunnel_port")
    if not name or not env:
        raise SystemExit("bridge runtime entry missing name or env")
    runtime = registry_by_name.get(name)
    if runtime is None:
        raise SystemExit(f"bridge runtime not registered: {name}")
    remote_port = runtime.get("port")
    if remote_port is None:
        raise SystemExit(f"bridge runtime missing port: {name}")
    if local_port is None:
        raise SystemExit(f"bridge runtime missing tunnel_port: {name}")
    url = f"http://127.0.0.1:{local_port}" if mode == "tunnel" else f"http://{host}:{remote_port}"
    print(f"export {env}={url!r}")
PY
}

project_storage() {
  local project="$1"
  local manifest_path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --manifest)
        manifest_path="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done

  if [[ -z "${manifest_path}" ]]; then
    manifest_path="$(default_manifest_path "${project}")"
  fi
  if [[ ! -f "${manifest_path}" ]]; then
    echo "manifest not found: ${manifest_path}" >&2
    exit 1
  fi

  while IFS=$'\t' read -r kind name runtime purpose; do
    [[ -z "${kind}" || -z "${name}" ]] && continue
    echo "bootstrap_storage=${kind}:${name}"
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "bash -lc $(printf '%q' "cd ${TITAN_NODE_REMOTE_ROOT} && ./scripts/titan.sh service init ${kind} ${name}")"
    ssh -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" "bash -lc $(printf '%q' "cd ${TITAN_NODE_REMOTE_ROOT} && ./scripts/titan.sh service status ${name}")"
  done < <(python3 - "${manifest_path}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
services = manifest.get("storage", {}).get("services", [])
if not services:
    raise SystemExit("storage service list is empty")
for item in services:
    name = item.get("name")
    kind = item.get("kind")
    if not name or not kind:
        raise SystemExit("storage service entry missing name or kind")
    print(f"{kind}\t{name}\t{item.get('runtime', '')}\t{item.get('purpose', '')}")
PY
  )
}

project_doctor() {
  local project="$1"
  local manifest_path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --manifest)
        manifest_path="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done

  if [[ -z "${manifest_path}" ]]; then
    manifest_path="$(default_manifest_path "${project}")"
  fi
  if [[ ! -f "${manifest_path}" ]]; then
    echo "manifest not found: ${manifest_path}" >&2
    exit 1
  fi

  python3 - "${manifest_path}" "${RUNTIME_REGISTRY_FILE}" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
registry_path = Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
registry = json.loads(registry_path.read_text(encoding="utf-8"))
registry_names = {item["name"] for item in registry.get("runtimes", []) if item.get("name")}

project = manifest.get("project", {})
bridge = manifest.get("bridge", {})
storage = manifest.get("storage", {})
catalog = manifest.get("catalog", {})
live = catalog.get("live", [])
scaffolded = catalog.get("scaffolded", [])
spec_only = catalog.get("spec_only", [])
runtimes = bridge.get("runtimes", [])
services = storage.get("services", [])

if not project.get("name"):
    raise SystemExit("project.name is required")
if not project.get("root"):
    raise SystemExit("project.root is required")
if not runtimes:
    raise SystemExit("bridge.runtimes is required")
if not services:
    raise SystemExit("storage.services is required")

runtime_names = set()
for item in live:
    name = item.get("name")
    if not name:
        raise SystemExit("catalog.live entry missing name")
    if name in runtime_names:
        raise SystemExit(f"duplicate runtime name in catalog: {name}")
    runtime_names.add(name)
    if name not in registry_names:
        raise SystemExit(f"live runtime missing from runtime registry: {name}")

bridge_names = set()
for item in runtimes:
    name = item.get("name")
    env = item.get("env")
    if not name or not env:
        raise SystemExit("bridge runtime entry missing name or env")
    if name in bridge_names:
        raise SystemExit(f"duplicate bridge runtime name: {name}")
    bridge_names.add(name)
    if name not in registry_names:
        raise SystemExit(f"bridge runtime missing from runtime registry: {name}")

for item in services:
    if not item.get("name"):
        raise SystemExit("storage service missing name")
    if not item.get("kind"):
        raise SystemExit(f"storage service missing kind: {item.get('name')}")

print(f"project_name={project['name']}")
print(f"project_root={project['root']}")
print(f"live_runtimes={len(live)}")
print(f"scaffolded_runtimes={len(scaffolded)}")
print(f"spec_only_runtimes={len(spec_only)}")
print(f"bridge_runtimes={len(runtimes)}")
print(f"storage_services={len(services)}")
print("doctor_status=ok")
PY
}

main() {
  local subcommand="${1:-help}"
  shift || true

  case "${subcommand}" in
    help|-h|--help)
      usage
      ;;
    init)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan project init <project_name> [--root PATH] [--manifest PATH]" >&2
        exit 1
      fi
      local project="$1"
      shift || true
      project_init "${project}" "$@"
      ;;
    bridge)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan project bridge <project_name> [--mode tunnel|direct] [--manifest PATH]" >&2
        exit 1
      fi
      local project="$1"
      shift || true
      local mode="tunnel"
      local manifest_path=""
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --mode)
            mode="$2"
            shift 2
            ;;
          --manifest)
            manifest_path="$2"
            shift 2
            ;;
          *)
            echo "Usage: titan project bridge <project_name> [--mode tunnel|direct] [--manifest PATH]" >&2
            exit 1
            ;;
        esac
      done
      if [[ -n "${manifest_path}" ]]; then
        project_bridge "${project}" --mode "${mode}" --manifest "${manifest_path}"
      else
        project_bridge "${project}" --mode "${mode}"
      fi
      ;;
    storage)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan project storage <project_name> [--manifest PATH]" >&2
        exit 1
      fi
      local project="$1"
      shift || true
      if [[ $# -gt 0 ]]; then
        project_storage "${project}" "$@"
      else
        project_storage "${project}"
      fi
      ;;
    doctor)
      if [[ $# -lt 1 ]]; then
        echo "Usage: titan project doctor <project_name> [--manifest PATH]" >&2
        exit 1
      fi
      local project="$1"
      shift || true
      if [[ $# -gt 0 ]]; then
        project_doctor "${project}" "$@"
      else
        project_doctor "${project}"
      fi
      ;;
    *)
      echo "Unknown project subcommand: ${subcommand}" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
