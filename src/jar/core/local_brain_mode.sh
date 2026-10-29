#!/usr/bin/env bash
# Pin or unpin the household chat brain to the on-box Qwen3-8B.
#
# When Gemini is unreachable (quota, outage, key), chat already falls back to
# local — but every turn still waits on a failing Gemini call first. This
# helper sets AGENT_TOOL_BRAIN=local in agent.env so turns go straight to
# :8010, restarts titan-agent-api, warms the local prompt cache, and smokes.
#
# Usage (on titan, or: ssh titan 'bash /ai/projects/titan-node/scripts/local_brain_mode.sh …'):
#   local_brain_mode.sh status
#   local_brain_mode.sh on       # AGENT_TOOL_BRAIN=local
#   local_brain_mode.sh off      # AGENT_TOOL_BRAIN=auto
#   local_brain_mode.sh on --no-restart   # rewrite env only (next API restart picks it up)
#
# Never prints agent.env or secrets. Refuses to run off-host unless TITAN_AGENT_ROOT is set
# to a writable agent root that already contains agent.env.
set -euo pipefail

ROOT="${TITAN_NODE_ROOT:-/ai/projects/titan-node}"
AGENT_ROOT="${TITAN_AGENT_ROOT:-/ai/projects/titan-local-agent}"
ENV_FILE="${AGENT_ROOT}/agent.env"
MODE="${1:-status}"
NO_RESTART=0
for a in "${@:2}"; do
  case "$a" in
    --no-restart) NO_RESTART=1 ;;
    *) echo "unknown flag: $a" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "local_brain_mode: missing $ENV_FILE (run on titan, or set TITAN_AGENT_ROOT)" >&2
  exit 1
fi

current_brain() {
  # Parse AGENT_TOOL_BRAIN without sourcing (sourcing would expand secrets into this shell).
  local line
  line="$(grep -E '^[[:space:]]*AGENT_TOOL_BRAIN=' "$ENV_FILE" | tail -n1 || true)"
  if [[ -z "$line" ]]; then
    echo "unset"
    return
  fi
  line="${line#AGENT_TOOL_BRAIN=}"
  line="${line#\"}"
  line="${line%\"}"
  line="${line#\'}"
  line="${line%\'}"
  echo "$line"
}

set_brain() {
  local want="$1"
  local tmp
  tmp="$(mktemp "${ENV_FILE}.tmp.XXXXXX")"
  # shellcheck disable=SC2016
  if grep -qE '^[[:space:]]*AGENT_TOOL_BRAIN=' "$ENV_FILE"; then
    sed -E "s|^[[:space:]]*AGENT_TOOL_BRAIN=.*|AGENT_TOOL_BRAIN='${want}'|" "$ENV_FILE" >"$tmp"
  else
    cat "$ENV_FILE" >"$tmp"
    printf "\nAGENT_TOOL_BRAIN='%s'\n" "$want" >>"$tmp"
  fi
  chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

smoke_local() {
  # shellcheck disable=SC1090
  set -a
  # Intentionally source on-host only; do not echo.
  . "$ENV_FILE"
  set +a
  export AGENT_TOOL_BRAIN=local
  python3 "$ROOT/scripts/warm_local.py"
  # Local-only canary slice: do not require Gemini while pinned local.
  python3 "$ROOT/scripts/agent_canary.py" --only local_model agent_api --no-alert
  out="$(bash "$ROOT/scripts/titan.sh" agent prompt "Reply with exactly: local-brain-ok" 2>/dev/null | tail -n1 || true)"
  if [[ "$out" != *local-brain-ok* ]]; then
    echo "local_brain_mode: smoke prompt unexpected: ${out:0:120}" >&2
    return 1
  fi
  echo "local_brain_mode: smoke ok ($out)"
}

case "$MODE" in
  status)
    echo "AGENT_TOOL_BRAIN=$(current_brain)"
    echo "env_file=$ENV_FILE"
    if systemctl is-active --quiet titan-agent-api 2>/dev/null; then
      echo "titan-agent-api=active"
    else
      echo "titan-agent-api=$(systemctl is-active titan-agent-api 2>/dev/null || echo unknown)"
    fi
    if systemctl is-active --quiet titan-local-agent 2>/dev/null; then
      echo "titan-local-agent=active"
    else
      echo "titan-local-agent=$(systemctl is-active titan-local-agent 2>/dev/null || echo unknown)"
    fi
    curl -sS -m 3 -o /dev/null -w "agent_api_health_http=%{http_code}\n" http://127.0.0.1:7770/health || true
    curl -sS -m 3 -o /dev/null -w "local_model_http=%{http_code}\n" http://127.0.0.1:8010/health || true
    ;;
  on)
    echo "local_brain_mode: setting AGENT_TOOL_BRAIN=local (was $(current_brain))"
    set_brain local
    if [[ "$NO_RESTART" -eq 0 ]]; then
      sudo systemctl restart titan-agent-api
      sleep 2
    else
      echo "local_brain_mode: skipped restart (--no-restart); restart titan-agent-api to apply"
    fi
    smoke_local
    echo "local_brain_mode: on"
    ;;
  off)
    echo "local_brain_mode: setting AGENT_TOOL_BRAIN=auto (was $(current_brain))"
    set_brain auto
    if [[ "$NO_RESTART" -eq 0 ]]; then
      sudo systemctl restart titan-agent-api
      sleep 2
    else
      echo "local_brain_mode: skipped restart (--no-restart); restart titan-agent-api to apply"
    fi
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
    python3 "$ROOT/scripts/warm_local.py" || true
    python3 "$ROOT/scripts/agent_canary.py" --no-alert || true
    echo "local_brain_mode: off (auto)"
    ;;
  *)
    echo "usage: $0 status|on|off [--no-restart]" >&2
    exit 2
    ;;
esac
