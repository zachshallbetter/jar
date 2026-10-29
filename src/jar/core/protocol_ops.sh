#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/titan_env.sh"
: "${PARAMETRIC_CONTRACT_FILE:=${TITAN_NODE_ROOT_DIR}/../Parametric/src/runtime_contract.json}"

if [[ -z "${TITAN_CLI:-}" ]]; then
  if [[ -x "${SCRIPT_DIR}/titan.sh" ]]; then
    TITAN_CLI="${SCRIPT_DIR}/titan.sh"
  else
    TITAN_CLI="$(command -v titan 2>/dev/null || true)"
  fi
fi
PARAMETRIC_REPO_ROOT="${PARAMETRIC_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../Parametric" 2>/dev/null && pwd || echo /ai/projects/Parametric)}"

usage() {
  cat <<'EOF'
Usage:
  titan protocol check [--json] [--quick]
  titan protocol report [--json] [--quick]
  titan protocol help

Checks:
  - runtime health against the registry-backed ports
  - proposed platform-plane docs for the Runtimes workspace
  - epistemic route smoke on /route

Output:
  - text mode prints OK/WARN/ERROR lines with codes and next actions
  - --json prints structured findings for tooling
EOF
}

main() {
  local subcommand="${1:-check}"
  shift || true
  case "${subcommand}" in
    help|-h|--help)
      usage
      ;;
    check|report)
      local json_mode="0"
      local quick_mode="0"
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --json)
            json_mode="1"
            shift
            ;;
          --quick)
            quick_mode="1"
            shift
            ;;
          *)
            echo "Usage: titan protocol ${subcommand} [--json] [--quick]" >&2
            exit 1
            ;;
        esac
      done
      python3 - "${json_mode}" "${quick_mode}" "${SCRIPT_DIR}" "${RUNTIME_REGISTRY_FILE}" "${PROTOCOL_MATRIX_FILE}" "${TITAN_CLI}" "$(resolved_titan_host)" "${PARAMETRIC_CONTRACT_FILE}" <<'PY'
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

json_mode = sys.argv[1] == "1"
quick_mode = sys.argv[2] == "1"
script_dir = Path(sys.argv[3])
runtime_registry_path = Path(sys.argv[4])
protocol_matrix_path = Path(sys.argv[5])
titan_cli = sys.argv[6]
titan_host = sys.argv[7]
parametric_contract_file = sys.argv[8]
proposal_docs_root = Path(os.environ.get("PROPOSAL_DOCS_ROOT", "/ai/projects/Runtimes/docs"))

runtime_registry = json.loads(runtime_registry_path.read_text(encoding="utf-8"))
matrix = json.loads(protocol_matrix_path.read_text(encoding="utf-8"))

def finding(surface, kind, severity, code, message, next_action, details=None):
    item = {
        "surface": surface,
        "kind": kind,
        "severity": severity,
        "code": code,
        "message": message,
        "next_action": next_action,
    }
    if details is not None:
        item["details"] = details
    return item

def tail_text(text, limit=20):
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "\n".join(lines[-limit:])

def bridge_host():
    return titan_host

def request_json(method, url, payload=None, timeout=5):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return resp.status, json.loads(raw)

def runtime_port(name):
    for item in runtime_registry.get("runtimes", []):
        if item.get("name") == name:
            port = item.get("port")
            if port is None:
                raise RuntimeError(f"missing port for {name}")
            return int(port)
    raise RuntimeError(f"unknown runtime: {name}")

def runtime_health_path(name):
    for item in runtime_registry.get("runtimes", []):
        if item.get("name") == name:
            health_url = item.get("health_url")
            if not health_url:
                raise RuntimeError(f"missing health_url for {name}")
            return urlsplit(health_url).path or "/"
    raise RuntimeError(f"unknown runtime: {name}")

def runtime_health_url(name):
    return f"http://{bridge_host()}:{runtime_port(name)}{runtime_health_path(name)}"

def runtime_status(name):
    completed = subprocess.run([titan_cli, "runtime", "status", name], text=True, capture_output=True, timeout=5)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            [titan_cli, "runtime", "status", name],
            completed.stdout,
            completed.stderr,
        )
    output = completed.stdout
    values = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values

results = []

for runtime in matrix.get("live_runtimes", []):
    name = runtime["name"]
    try:
        state = runtime_status(name)
    except subprocess.CalledProcessError as exc:
        results.append(finding(
            name,
            "runtime",
            "error",
            "RUNTIME_STATUS_FAILED",
            f"runtime status command failed with exit code {exc.returncode}",
            "Fix the Titan runtime command path or the remote SSH connection, then rerun the protocol sweep.",
            {"returncode": exc.returncode},
        ))
        continue
    except Exception as exc:
        results.append(finding(
            name,
            "runtime",
            "error",
            "RUNTIME_STATUS_UNKNOWN",
            f"runtime status command failed: {exc}",
            "Fix the Titan runtime command path or the remote SSH connection, then rerun the protocol sweep.",
        ))
        continue

    if state.get("runtime_status") != "running":
        results.append(finding(
            name,
            "runtime",
            "error",
            "RUNTIME_STOPPED",
            f"runtime is {state.get('runtime_status', 'unknown')}",
            f"Start the runtime with titan runtime start {name} or redeploy it, then rerun titan protocol check.",
            {"runtime_status": state.get("runtime_status", "unknown"), "runtime_log": state.get("runtime_log"), "runtime_target": state.get("runtime_target")},
        ))
        continue

    health_url = runtime_health_url(name)
    try:
        status, payload = request_json("GET", health_url)
        if status != 200:
            results.append(finding(
                name,
                "runtime",
                "error",
                "RUNTIME_HEALTH_STATUS",
                f"health endpoint returned HTTP {status}",
                "Check the runtime logs, verify the service is running, and confirm the bridge host/port is correct.",
                {"health_url": health_url, "response": payload},
            ))
            continue
        results.append(finding(
            name,
            "runtime",
            "ok",
            "RUNTIME_HEALTH_OK",
            "health endpoint responded successfully",
            "No action required.",
            {"health_url": health_url, "response": payload},
        ))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp is not None else ""
        if exc.code == 401 and runtime.get("auth", "none") != "none":
            results.append(finding(
                name,
                "runtime",
                "private_expected",
                "RUNTIME_AUTH_GATED",
                "health endpoint is auth-gated and returned 401 to the unauthenticated probe",
                "Treat the auth gate as expected for private runtimes; provide the coordinator/auth token only if you need an authenticated health check.",
                {"health_url": health_url, "response": body, "auth": runtime.get("auth", "none")},
            ))
        else:
            results.append(finding(
                name,
                "runtime",
                "error",
                "RUNTIME_HEALTH_HTTP_ERROR",
                f"health endpoint returned HTTP {exc.code}",
                "Start the runtime or fix the endpoint contract, then rerun titan protocol check.",
                {"health_url": health_url, "response": body},
            ))
        continue
    except URLError as exc:
        results.append(finding(
            name,
            "runtime",
            "error",
            "RUNTIME_HEALTH_UNREACHABLE",
            f"health endpoint unreachable: {exc.reason}",
            "Check Titan networking, the runtime process, and any DNS/IP mismatch.",
            {"health_url": health_url},
        ))
        continue
    except Exception as exc:
        results.append(finding(
            name,
            "runtime",
            "error",
            "RUNTIME_HEALTH_UNKNOWN",
            f"health check failed: {exc}",
            "Inspect the runtime and rerun the protocol check.",
            {"health_url": health_url},
        ))
        continue

    if name == "epistemic-runtime":
        route_url = f"http://{bridge_host()}:{runtime_port(name)}/route"
        try:
            status, payload = request_json(
                "POST",
                route_url,
                {"input": "Should I answer or request more fields?"},
            )
            if status != 200:
                results.append(finding(
                    name,
                    "runtime",
                    "error",
                    "EPISTEMIC_ROUTE_STATUS",
                    f"/route returned HTTP {status}",
                    "Check the epistemic runtime contract and the Parametric checkpoint wiring.",
                    {"route_url": route_url, "response": payload},
                ))
            else:
                missing = [key for key in ("truth_type", "retrieval_required", "confidence_score", "action") if key not in payload]
                if missing:
                    results.append(finding(
                        name,
                        "runtime",
                        "error",
                        "EPISTEMIC_ROUTE_MISMATCH",
                        f"/route response missing keys: {', '.join(missing)}",
                        "Update the runtime contract or client expectations so the response schema stays stable.",
                        {"route_url": route_url, "response": payload},
                    ))
                else:
                    allowed_actions = {"answer", "retrieve", "request", "abstain"}
                    action = str(payload.get("action", "")).lower()
                    if action not in allowed_actions:
                        results.append(finding(
                            name,
                            "runtime",
                            "error",
                            "EPISTEMIC_ROUTE_MISMATCH",
                            f"/route returned unsupported action: {action or 'missing'}",
                            "Update the runtime action vocabulary or the Parametric contract so the router action stays explicit.",
                            {"route_url": route_url, "response": payload},
                        ))
                        continue
                    results.append(finding(
                        name,
                        "runtime",
                        "ok",
                        "EPISTEMIC_ROUTE_OK",
                        "/route returned the expected epistemic fields and action",
                        "No action required.",
                        {"route_url": route_url, "response": payload},
                    ))
        except Exception as exc:
            results.append(finding(
                name,
                "runtime",
                "error",
                "EPISTEMIC_ROUTE_FAILED",
                f"/route smoke failed: {exc}",
                "Verify the Parametric checkpoint and the runtime bridge before trusting epistemic routing.",
                {"route_url": route_url},
            ))

parametric_contract_path = Path(parametric_contract_file)
if parametric_contract_path.exists():
    try:
        contract = json.loads(parametric_contract_path.read_text(encoding="utf-8"))
        runtime = contract.get("runtime", {})
        required_fields = set(runtime.get("required_response_fields", []))
        missing_fields = [field for field in ("truth_type", "retrieval_required", "confidence_score", "action") if field not in required_fields]
        errors = []
        if contract.get("name") != "parametric":
            errors.append("name must be parametric")
        if runtime.get("service") != "epistemic-runtime":
            errors.append("runtime.service must be epistemic-runtime")
        if runtime.get("health_path") != "/health":
            errors.append("runtime.health_path must be /health")
        if runtime.get("route_path") != "/route":
            errors.append("runtime.route_path must be /route")
        if runtime.get("bridge_env") != "EPISTEMIC_RUNTIME_URL":
            errors.append("runtime.bridge_env must be EPISTEMIC_RUNTIME_URL")
        if runtime.get("bridge_enabled_env") != "EPISTEMIC_ROUTER_ENABLED":
            errors.append("runtime.bridge_enabled_env must be EPISTEMIC_ROUTER_ENABLED")
        if missing_fields:
            errors.append(f"runtime.required_response_fields missing: {', '.join(missing_fields)}")
        if errors:
            results.append(finding(
                "parametric-contract",
                "contract",
                "error",
                "PARAMETRIC_CONTRACT_MISMATCH",
                "versioned Parametric contract does not match the Titan bridge expectations",
                "Update src/runtime_contract.json in Parametric, commit it, and rerun titan protocol check so Titan and Parametric stay in sync.",
                {"contract_file": str(parametric_contract_path), "errors": errors, "contract": contract},
            ))
        else:
            results.append(finding(
                "parametric-contract",
                "contract",
                "ok",
                "PARAMETRIC_CONTRACT_OK",
                "versioned Parametric contract matches the Titan bridge expectations",
                "No action required.",
                {"contract_file": str(parametric_contract_path), "contract": contract},
            ))
    except Exception as exc:
        results.append(finding(
            "parametric-contract",
            "contract",
            "error",
            "PARAMETRIC_CONTRACT_READ_FAILED",
            f"failed to read or parse Parametric contract: {exc}",
            "Sync the committed Parametric contract into Titan and rerun titan protocol check.",
            {"contract_file": str(parametric_contract_path)},
        ))
else:
    results.append(finding(
        "parametric-contract",
        "contract",
        "warn",
        "PARAMETRIC_CONTRACT_MISSING",
        f"versioned Parametric contract missing: {parametric_contract_path}",
        "Commit src/runtime_contract.json in Parametric and sync the repo into Titan so Titan can pull contract changes through git.",
        {"contract_file": str(parametric_contract_path)},
    ))

for doc in matrix.get("proposal_docs", []):
    doc_path = Path(doc["doc_path"])
    if not doc_path.exists():
        fallback_path = proposal_docs_root / doc_path.name
        if fallback_path.exists():
            doc_path = fallback_path
    if not doc_path.exists():
        results.append(finding(
            doc["name"],
            "proposal",
            "error",
            "PROPOSAL_DOC_MISSING",
            f"proposal doc missing: {doc_path}",
            "Restore the proposed runtime doc or remove the surface from the protocol matrix.",
            {"doc_path": str(doc_path)},
        ))
        continue
    text = doc_path.read_text(encoding="utf-8", errors="replace")
    if "Status: Proposed" not in text and "status: proposed" not in text.lower():
        results.append(finding(
            doc["name"],
            "proposal",
            "warn",
            "PROPOSAL_DOC_STATUS_UNCLEAR",
            "proposal doc does not clearly mark the runtime as proposed",
            "Update the doc header so readers know the surface is not yet live.",
            {"doc_path": str(doc_path)},
        ))
    else:
        results.append(finding(
            doc["name"],
            "proposal",
            "warn",
            "PROPOSAL_SURFACE_NOT_LIVE",
            f"{doc_path.name} is documented but not live",
            "Design or implement the runtime before expecting endpoint-level checks. See docs/protocol-matrix.md for the recommended order and minimum viable API shape.",
            {"doc_path": str(doc_path)},
        ))

if not quick_mode:
    for smoke in matrix.get("consumer_smokes", []):
        name = smoke["name"]
        cwd = Path(smoke["cwd"])
        command = smoke["command"]
        timeout = int(smoke.get("timeout_seconds", 900))
        if not cwd.exists():
            results.append(finding(
                name,
                "consumer",
                "error",
                "CONSUMER_SMOKE_CWD_MISSING",
                f"consumer smoke cwd missing: {cwd}",
                "Restore the repo checkout or update the protocol matrix path.",
                {"cwd": str(cwd), "command": command},
            ))
            continue
        try:
            completed = subprocess.run(
                ["bash", "-lc", command],
                cwd=str(cwd),
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            results.append(finding(
                name,
                "consumer",
                "error",
                "CONSUMER_SMOKE_TIMEOUT",
                f"consumer smoke timed out after {timeout}s",
                "Increase the timeout if the repo smoke is legitimately longer, or fix the hanging step.",
                {"cwd": str(cwd), "command": command},
            ))
            continue
        if completed.returncode != 0:
            results.append(finding(
                name,
                "consumer",
                "error",
                "CONSUMER_SMOKE_FAILED",
                f"consumer smoke failed with exit code {completed.returncode}",
                "Fix the repo-local smoke or bridge profile, then rerun titan protocol check.",
                {
                    "cwd": str(cwd),
                    "command": command,
                    "stdout_tail": tail_text(completed.stdout, 20),
                    "stderr_tail": tail_text(completed.stderr, 20),
                    "returncode": completed.returncode,
                },
            ))
        else:
            stdout_text = completed.stdout or ""
            if "[WARN]" in stdout_text:
                results.append(finding(
                    name,
                    "consumer",
                    "warn",
                    "CONSUMER_SMOKE_WARN",
                    "consumer smoke completed with warnings",
                    "Inspect the warning lines in the smoke output and decide whether the missing dependency is expected for this environment.",
                    {
                        "cwd": str(cwd),
                        "command": command,
                        "stdout_tail": tail_text(stdout_text, 20),
                        "stderr_tail": tail_text(completed.stderr, 20),
                    },
                ))
            else:
                results.append(finding(
                    name,
                    "consumer",
                    "ok",
                    "CONSUMER_SMOKE_OK",
                    "consumer smoke completed successfully",
                    "No action required.",
                    {"cwd": str(cwd), "command": command},
                ))

severity_order = {"ok": 0, "private_expected": 1, "warn": 2, "error": 3}
counts = {"ok": 0, "private_expected": 0, "warn": 0, "error": 0}
for item in results:
    counts[item["severity"]] += 1

overall = "ok"
if counts["error"]:
    overall = "error"
elif counts["warn"]:
    overall = "warn"

payload = {
    "status": overall,
    "counts": counts,
    "results": results,
}

if json_mode:
    print(json.dumps(payload, indent=2, sort_keys=True))
else:
    for item in sorted(results, key=lambda r: (severity_order[r["severity"]], r["kind"], r["surface"])):
        prefix = item["severity"].upper()
        print(f"{prefix} {item['kind']}[{item['surface']}] {item['code']}: {item['message']}")
        print(f"  next: {item['next_action']}")
    print(f"protocol_status={overall}")

if counts["error"]:
    raise SystemExit(1)
PY
      ;;
    *)
      echo "Unknown protocol subcommand: ${subcommand}" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
