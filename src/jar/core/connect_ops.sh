#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/titan_env.sh"
TITAN_CONNECT_MARKER_BEGIN="# BEGIN TITAN NODE MANAGED BLOCK"
TITAN_CONNECT_MARKER_END="# END TITAN NODE MANAGED BLOCK"

usage() {
  cat <<'EOF'
Usage:
  titan connect
  titan connect info
  titan connect config [--apply]
  titan connect setup [--apply]
  titan connect check [--full]
  titan connect help

Commands:
  info     Print the resolved Titan host, SSH alias, and recommended access mode.
  config   Print the SSH config block for the Titan alias.
  setup    Write the SSH config block into ~/.ssh/config when --apply is set.
  check    Verify SSH reachability and remote Titan checkout availability.

Notes:
  - VPN or mesh networking is preferred while away.
  - If VPN is unavailable, use a stable DNS name when you have one.
  - Use DDNS only when the public IP changes and you cannot avoid it.
EOF
}


resolved_host_source() {
  if [[ -n "${TITAN_HOST_DNS}" ]]; then
    printf 'dns'
    return 0
  fi

  if [[ -f "${TITAN_SSH_CONFIG}" ]]; then
    local configured_host=""
    configured_host="$(ssh -G -F "${TITAN_SSH_CONFIG}" "${TITAN_HOST}" 2>/dev/null | awk 'BEGIN{IGNORECASE=1} /^hostname /{print $2; exit}' || true)"
    if [[ -n "${configured_host}" && "${configured_host}" != "${TITAN_HOST}" && "${configured_host}" != "${TITAN_HOST_IP}" ]]; then
      printf 'ssh-config'
      return 0
    fi
  fi

  printf 'ip'
}

resolved_identity_file() {
  if [[ -n "${TITAN_SSH_IDENTITY_FILE:-}" ]]; then
    printf '%s' "${TITAN_SSH_IDENTITY_FILE}"
    return 0
  fi

  local candidate
  for candidate in \
    "$HOME/.ssh/github_auth_mac" \
    "$HOME/.ssh/id_ed25519" \
    "$HOME/.ssh/github_ed25519" \
    "$HOME/.ssh/id_rsa"; do
    if [[ -f "${candidate}" ]]; then
      printf '%s' "${candidate}"
      return 0
    fi
  done

  printf '%s' "$HOME/.ssh/id_ed25519"
}

recommendation() {
  printf 'vpn-or-mesh-first;dns-second;ddns-last-resort'
}

render_config_block() {
  cat <<EOF
${TITAN_CONNECT_MARKER_BEGIN}
# Titan away-mode SSH alias.
# Preferred connectivity order:
#  1) VPN or mesh hostname
#  2) stable DNS name
#  3) DDNS hostname
#  4) static LAN IP only when you are on the local network
Host ${TITAN_HOST}
    HostName $(resolved_host)
    User ${TITAN_SSH_USER}
    Port ${TITAN_SSH_PORT}
    IdentityFile $(resolved_identity_file)
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 120
    TCPKeepAlive yes
    ConnectTimeout 10
${TITAN_CONNECT_MARKER_END}
EOF
}

print_info() {
  cat <<EOF
titan_ssh_alias=${TITAN_HOST}
titan_ssh_config=${TITAN_SSH_CONFIG}
titan_ssh_user=${TITAN_SSH_USER}
titan_ssh_port=${TITAN_SSH_PORT}
titan_ssh_identity_file=$(resolved_identity_file)
titan_host_source=$(resolved_host_source)
titan_host_target=$(resolved_host)
titan_remote_root=${TITAN_AGENT_REMOTE_ROOT}
titan_remote_access_recommendation=$(recommendation)
EOF
}

write_config() {
  local apply_mode="0"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --apply)
        apply_mode="1"
        shift
        ;;
      *)
        echo "Usage: titan connect setup [--apply]" >&2
        exit 1
        ;;
    esac
  done

  if [[ "${apply_mode}" != "1" ]]; then
    render_config_block
    return 0
  fi

  local config_dir
  config_dir="$(dirname "${TITAN_SSH_CONFIG}")"
  mkdir -p "${config_dir}"

  local tmp_file
  tmp_file="$(mktemp)"

  if [[ -f "${TITAN_SSH_CONFIG}" ]]; then
    python3 - "${TITAN_SSH_CONFIG}" "${tmp_file}" "${TITAN_CONNECT_MARKER_BEGIN}" "${TITAN_CONNECT_MARKER_END}" "${TITAN_HOST}" "$(resolved_host)" "${TITAN_SSH_USER}" "${TITAN_SSH_PORT}" "$(resolved_identity_file)" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1])
dest = Path(sys.argv[2])
begin = sys.argv[3]
end = sys.argv[4]
host = sys.argv[5]
hostname = sys.argv[6]
user = sys.argv[7]
port = sys.argv[8]
identity_file = sys.argv[9]
block = f"""{begin}
# Titan away-mode SSH alias.
# Preferred connectivity order:
#  1) VPN or mesh hostname
#  2) stable DNS name
#  3) DDNS hostname
#  4) static LAN IP only when you are on the local network
Host {host}
    HostName {hostname}
    User {user}
    Port {port}
    IdentityFile {identity_file}
    IdentitiesOnly yes
    ServerAliveInterval 30
    ServerAliveCountMax 120
    TCPKeepAlive yes
    ConnectTimeout 10
{end}
"""
text = source.read_text(encoding="utf-8")
if begin in text and end in text:
    prefix = text.split(begin, 1)[0].rstrip("\n")
    suffix = text.split(end, 1)[1].lstrip("\n")
    merged = prefix + "\n\n" + block + suffix
else:
    merged = text.rstrip("\n") + "\n\n" + block
dest.write_text(merged, encoding="utf-8")
PY
    mv "${tmp_file}" "${TITAN_SSH_CONFIG}"
  else
    render_config_block > "${tmp_file}"
    mv "${tmp_file}" "${TITAN_SSH_CONFIG}"
  fi

  chmod 600 "${TITAN_SSH_CONFIG}"
  printf 'ssh_config_written=%s\n' "${TITAN_SSH_CONFIG}"
}

check_connectivity() {
  local full_mode="0"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --full)
        full_mode="1"
        shift
        ;;
      *)
        echo "Usage: titan connect check [--full]" >&2
        exit 1
      ;;
    esac
  done

  local ssh_base_target
  local ssh_args=()
  if [[ -f "${TITAN_SSH_CONFIG}" ]]; then
    ssh_args=(
      -F "${TITAN_SSH_CONFIG}"
      -o BatchMode=yes
      -o ConnectTimeout=10
    )
    ssh_base_target="${TITAN_HOST}"
  else
    ssh_args=(
      -o BatchMode=yes
      -o ConnectTimeout=10
      -p "${TITAN_SSH_PORT}"
    )
    ssh_base_target="${TITAN_SSH_USER}@$(resolved_host)"
  fi

  ssh "${ssh_args[@]}" \
    "${ssh_base_target}" \
    "bash -s" -- "${TITAN_AGENT_REMOTE_ROOT}" "${full_mode}" <<'EOF'
set -euo pipefail

remote_root="$1"
full_mode="$2"
printf 'ssh_ok host=%s user=%s\n' "$(hostname -f 2>/dev/null || hostname)" "$(whoami)"
if [[ -d "${remote_root}" ]]; then
  printf 'remote_root=present path=%s\n' "${remote_root}"
  if [[ "${full_mode}" == "1" && -x "${remote_root}/scripts/titan.sh" ]]; then
    cd "${remote_root}"
    if doctor_output="$(./scripts/titan.sh doctor --json 2>&1)"; then
      printf 'remote_doctor=ok\n'
      printf '%s\n' "${doctor_output}" | tail -n 20
    else
      doctor_rc=$?
      printf 'remote_doctor=failed rc=%s\n' "${doctor_rc}"
      printf '%s\n' "${doctor_output}" | tail -n 20 || true
      exit "${doctor_rc}"
    fi
  elif [[ -x "${remote_root}/scripts/titan.sh" ]]; then
    printf 'remote_doctor=skipped\n'
  else
    printf 'remote_doctor=missing_repo\n'
  fi
else
  printf 'remote_root=missing path=%s\n' "${remote_root}"
fi
EOF
}

main() {
  local subcommand="${1:-info}"
  if [[ $# -gt 0 ]]; then
    shift
  fi

  case "${subcommand}" in
    help|-h|--help)
      usage
      ;;
    ""|info)
      print_info
      ;;
    config)
      write_config "$@"
      ;;
    setup)
      write_config "$@"
      ;;
    check)
      check_connectivity "$@"
      ;;
    *)
      echo "Unknown connect subcommand: ${subcommand}" >&2
      usage >&2
      exit 1
      ;;
  esac
}

main "$@"
