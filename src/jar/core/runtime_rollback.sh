#!/usr/bin/env bash
# Put a runtime's previous release back (#1023): current <-> previous in /ai/runtimes/bin/<name>, restart, health.
# No rebuild. Usage: bash scripts/runtime_rollback.sh <name> [--dry-run]
set -euo pipefail
NAME="${1:?runtime name}"; DRY="${2:-}"
SSH_ALIAS="${TITAN_SSH_ALIAS:-titan}"
RTBIN="python3 /ai/projects/titan-node/scripts/runtime_bin.py"
if [ "$DRY" = "--dry-run" ]; then
  ssh "$SSH_ALIAS" "$RTBIN rollback '${NAME}' --dry-run"
  exit 0
fi
ssh "$SSH_ALIAS" "$RTBIN rollback '${NAME}' && sudo systemctl restart '${NAME}.service'"
HEALTH=$(ssh "$SSH_ALIAS" "python3 -c 'import json,sys; r=json.load(open(\"/ai/projects/titan-node/scripts/runtime-registry.json\")); print(next((x[\"health_url\"] for x in r[\"runtimes\"]+r.get(\"brokers\",[]) if x[\"name\"]==sys.argv[1]), \"\"))' '${NAME}'")
[ -n "$HEALTH" ] || { echo "no health URL for ${NAME} in the registry; check it by hand" >&2; exit 1; }
for i in $(seq 1 15); do
  if ssh "$SSH_ALIAS" "curl -fsS --max-time 3 '${HEALTH}'" >/dev/null 2>&1; then echo "${NAME}: rolled back, healthy"; exit 0; fi
  sleep 2
done
echo "${NAME}: rolled back but NOT healthy; look at it now" >&2
exit 1
