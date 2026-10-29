#!/usr/bin/env bash
set -euo pipefail
# Legacy forwarder: use resource_broker.sh
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/resource_broker.sh" snapshot "$@"
