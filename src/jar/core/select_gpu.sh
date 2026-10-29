#!/usr/bin/env bash
set -euo pipefail
# Legacy forwarder: use resource_broker.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$("${SCRIPT_DIR}/resource_broker.sh" pick "$@")"
if [[ "${TARGET}" == gpu:* ]]; then
  echo "${TARGET#gpu:}"
elif [[ "${TARGET}" == cpu ]]; then
  exit 2
else
  exit 2
fi
