#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
elif [[ -n "${0:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
else
  SCRIPT_DIR="$(pwd)"
fi

if [[ "$(basename "${SCRIPT_DIR}")" == "scripts" ]]; then
  TITAN_NODE_ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  TITAN_NODE_ROOT_DIR="${SCRIPT_DIR}"
fi

# Global Config
TITAN_HOST="${TITAN_HOST:-titan}"
TITAN_HOST_IP="${TITAN_HOST_IP:-10.0.0.170}"
TITAN_HOST_DNS="${TITAN_HOST_DNS:-}"
TITAN_NODE_HOSTNAME="${TITAN_NODE_HOSTNAME:-titan-node}"
TITAN_SSH_CONFIG="${TITAN_SSH_CONFIG:-$HOME/.ssh/config}"
TITAN_SSH_USER="${TITAN_SSH_USER:-zach}"
TITAN_SSH_PORT="${TITAN_SSH_PORT:-22}"

# Directories & Files
RUNTIME_REGISTRY_FILE="${RUNTIME_REGISTRY_FILE:-${SCRIPT_DIR}/runtime-registry.json}"
PROTOCOL_MATRIX_FILE="${PROTOCOL_MATRIX_FILE:-${SCRIPT_DIR}/protocol-matrix.json}"
MANIFESTS_DIR="${MANIFESTS_DIR:-${TITAN_NODE_ROOT_DIR}/manifests}"
PARAMETRIC_CONTRACT_FILE="${PARAMETRIC_CONTRACT_FILE:-${TITAN_NODE_ROOT_DIR}/../Parametric/src/runtime_contract.json}"

TITAN_NODE_REMOTE_ROOT="${TITAN_NODE_REMOTE_ROOT:-/ai/projects/titan-node}"
TITAN_AGENT_REMOTE_ROOT="${TITAN_AGENT_REMOTE_ROOT:-/ai/projects/titan-node}"

# Canonical Host Resolver
resolved_titan_host() {
  if [[ -n "${TITAN_HOST_DNS}" ]]; then
    printf '%s' "${TITAN_HOST_DNS}"
    return 0
  fi

  if [[ -f "${TITAN_SSH_CONFIG}" ]]; then
    local configured_host=""
    configured_host="$(ssh -G -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /^hostname /{print $2; exit}' || true)"
    if [[ -n "${configured_host}" && "${configured_host}" != "${TITAN_HOST}" && "${configured_host}" != "${TITAN_HOST_IP}" ]]; then
      printf '%s' "${configured_host}"
      return 0
    fi
  fi

  printf '%s' "${TITAN_HOST_IP}"
}

# Legacy Function Aliases
bridge_direct_host() { resolved_titan_host; }
bridge_host() { resolved_titan_host; }
resolved_host() { resolved_titan_host; }
resolve_direct_host() { resolved_titan_host; }

# Canonical "are we currently running ON titan-node" predicate. This is the
# single source of truth for host-context detection — titan.sh's
# is_remote_agent_context and runtime_ops.sh's former local
# is_titan_host_context previously reimplemented this independently, held
# together only by two separately-hardcoded default-string literals
# (TITAN_HOST, TITAN_NODE_HOSTNAME). A synthetic TITAN_NODE_HOSTNAME=""
# test could make both return true simultaneously. Callers needing the
# negation (e.g. "is this a remote hop") should invoke
# `! is_titan_host_context` rather than reimplementing the check.
is_titan_host_context() {
  local host_short
  host_short="$(hostname -s 2>/dev/null || hostname)"
  [[ "${host_short}" == "${TITAN_HOST}" || "${host_short}" == "${TITAN_NODE_HOSTNAME}" || "${TITAN_REMOTE_EXEC:-0}" == "1" ]]
}
