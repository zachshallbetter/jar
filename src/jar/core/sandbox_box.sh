#!/usr/bin/env bash
# A sandbox "box": titan's agent API on a spare port, running a given copy of scripts/, against copies of the live
# stores, so an A/B or a bisect measures the same context as live without touching anything real (10-03: every A/B
# re-derived this in /ai/tmp; #770: a box without persona.md ran with a different context than live).
#
#   sandbox_box.sh up <port> [--profile live|box] [--code DIR] [--jev on|off] [--cloud on|off] [--fe-graph on|off] [--fixture] [--first-run] [--no-watchdog] [--hot C]
#   sandbox_box.sh down <port> [--keep]
#   sandbox_box.sh restart <port>
#   sandbox_box.sh status <port>
#
# up      checks the port and its stub port (port+10352) are free; copies every connectors store and world file
#         (sqlite backup API, WAL-safe) and EVERY memory file (*.db via backup, persona.md and the rest as files) into
#         /ai/tmp/box-<port>; writes manifest.json (what was copied, sizes, the code's rev, the switches); starts a fake
#         Home Assistant and the API under bwrap with every live dir read-only; waits for /health.
#         --code DIR   a scripts/ copy to run (default: `git archive HEAD:scripts` of the deploy checkout)
#         --profile    live (default): Jev and Gemini as agent.env has them. box: no Jev, no Gemini, brain local
#                      (the same as --jev off --cloud off). Every profile: the HA stub with a dummy token, never the
#                      real one; discovery, task autonomy, alerts, APNs and GitHub off.
#         --jev on     (default, like live) Jev's turn gates and router as agent.env has them; off: no Jev at all
#         --cloud on   (default, like live) Gemini as agent.env has it; off: no Gemini key, brain pinned local
#         --fe-graph   AGENT_FE_GRAPH for this box (#1399's situation tool; default off, as live): the A/B's one switch.
#                      Every box reads its own copy of graph.db (GRAPH_DB), never the live one.
#         --fixture    the golden household (#1399 P4, box_fixture.py) instead of copies of the live stores: BOX_FIXTURE=1,
#                      dead runtime ports, /ai/datasets, /ai/logs and the agent dir hidden (tmpfs). Replies may be kept.
#         --first-run  a box out of the carton (#411): an empty household store (no people but the seeded owner, no
#                      devices) and BOX_SETUP=1 with its key in /ai/tmp/box-<port>/box-setup; the setup page is
#                      http://127.0.0.1:<port>/setup#k=<that key>. Its HA stub starts fresh (not onboarded, no token
#                      file) and every TITAN_*_URL points at a dead port: nothing reaches the live runtimes.
#         watchdog     on by default: two GPU readings >= --hot (88 C) in a row take the box down
# down    stops the box by its recorded PIDs (never by pattern), checks the port is free, and deletes the box dir
#         (it holds copies of household data) unless --keep.
# Point an eval at it with TURN_EVAL_API=http://127.0.0.1:<port>. Its turn log: /ai/tmp/box-<port>/turns.jsonl.
set -uo pipefail

AGENT_ENV=${AGENT_ENV:-/ai/projects/titan-local-agent/agent.env}
CHECKOUT=${TITAN_CHECKOUT:-/ai/projects/titan-node}
LIVE=${CONNECTORS_LIVE:-/ai/datasets/connectors}
MEMORY_LIVE=${MEMORY_LIVE:-$HOME/.config/jellybean/memory}
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/ops_ports.sh"   # OPS_AGENT_API_PORT (#1518)

usage() { sed -n '6,9p' "$0" | sed 's/^# \{0,3\}//'; exit 2; }
[ $# -ge 2 ] || usage
cmd=$1 port=$2; shift 2
[[ "$port" =~ ^[0-9]+$ ]] && [ "$port" -ge 1024 ] && [ "$port" != "$OPS_AGENT_API_PORT" ] || { echo "refusing port $port ($OPS_AGENT_API_PORT is live)" >&2; exit 2; }
stub=$((port + 10352))
B=/ai/tmp/box-$port

port_busy() { ss -ltn | grep -qE ":$1 "; }

down() {
  local keep=$1
  for f in watchdog box-py box stub; do
    [ -s "$B/$f.pid" ] || continue
    p=$(cat "$B/$f.pid")
    kill "$p" 2>/dev/null
  done
  sleep 3
  for f in watchdog box-py box stub; do
    [ -s "$B/$f.pid" ] || continue
    p=$(cat "$B/$f.pid")
    kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null
  done
  sleep 1
  if port_busy "$port" || port_busy "$stub"; then echo "WARNING: port $port or $stub still in use" >&2; return 1; fi
  [ "$keep" = 1 ] || rm -rf "${B:?}"
  echo "box $port down$([ "$keep" = 1 ] && echo " (kept $B)")"
}

case "$cmd" in
  status)
    if [ -f "$B/manifest.json" ]; then cat "$B/manifest.json"; echo; fi
    port_busy "$port" && echo "port $port: listening" || echo "port $port: free"
    exit 0 ;;
  down)
    keep=0; [ "${1:-}" = --keep ] && keep=1
    [ -d "$B" ] || { echo "no box at $B" >&2; exit 1; }
    down "$keep"; exit $? ;;
  restart)
    # the API only, over the same box dir and stores (the HA stub keeps running): stop it by its recorded PIDs, wait
    # for the port, launch it again from launch.sh with agent.env sourced as `up` did, wait for /health
    [ -s "$B/launch.sh" ] || { echo "no launch.sh in $B (a box from before restart existed?)" >&2; exit 1; }
    for f in box-py box; do [ -s "$B/$f.pid" ] && kill "$(cat "$B/$f.pid")" 2>/dev/null; done
    for _ in $(seq 1 20); do port_busy "$port" || break; sleep 1; done
    for f in box-py box; do [ -s "$B/$f.pid" ] && kill -0 "$(cat "$B/$f.pid")" 2>/dev/null && kill -9 "$(cat "$B/$f.pid")" 2>/dev/null; done
    port_busy "$port" && { echo "ABORT: port $port still in use" >&2; exit 1; }
    set -a; . "$AGENT_ENV"; set +a
    bash "$B/launch.sh" < /dev/null >> "$B/api.log" 2>&1 & echo $! > "$B/box.pid"
    for _ in $(seq 1 60); do curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null && break; sleep 1; done
    ss -ltnp | grep ":$port " | grep -o "pid=[0-9]*" | head -1 | cut -d= -f2 > "$B/box-py.pid"
    curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null || { echo "ABORT: the box API didn't come back" >&2; exit 1; }
    echo "box $port restarted"; exit 0 ;;
  up) ;;
  *) usage ;;
esac

code="" jev=on cloud=on fe_graph=off watchdog=1 hot=88 profile=live first_run=0 fixture=0
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) profile=$2; shift 2
               case "$profile" in live) ;; box) jev=off cloud=off ;; *) usage ;; esac ;;
    --code) code=$2; shift 2 ;;
    --jev) jev=$2; shift 2 ;;
    --cloud) cloud=$2; shift 2 ;;
    --fe-graph) fe_graph=$2; shift 2 ;;
    --first-run) first_run=1; shift ;;
    --fixture) fixture=1; shift ;;
    --no-watchdog) watchdog=0; shift ;;
    --hot) hot=$2; shift 2 ;;
    *) usage ;;
  esac
done
[[ "$jev" =~ ^(on|off)$ && "$cloud" =~ ^(on|off)$ && "$fe_graph" =~ ^(on|off)$ ]] || usage
if port_busy "$port" || port_busy "$stub"; then echo "ABORT: port $port or $stub already in use" >&2; exit 1; fi
[ -e "$B" ] && { echo "ABORT: $B exists (sandbox_box.sh down $port first)" >&2; exit 1; }

umask 077
mkdir -p "$B"/{stores,world,mem,daily} && chmod 700 "$B"
if [ -n "$code" ]; then
  cp -a "$code/." "$B/code/" 2>/dev/null || { mkdir -p "$B/code" && cp -a "$code/." "$B/code/"; }
  rev="given:$code"
else
  mkdir -p "$B/code" && git -C "$CHECKOUT" archive HEAD:scripts | tar -x -C "$B/code"
  rev=$(git -C "$CHECKOUT" rev-parse --short HEAD)
fi
[ -f "$B/code/agent_api_server.py" ] || { echo "ABORT: no agent_api_server.py in the code" >&2; rm -rf "${B:?}"; exit 1; }

# A --jev/--cloud that departs from the profile's own switches makes it "custom" in the manifest.
{ [ "$profile" = live ] && [ "$jev$cloud" != onon ]; } || { [ "$profile" = box ] && [ "$jev$cloud" != offoff ]; } && profile=custom
if [ "$fixture" = 1 ]; then
  # #1399 P4: the golden household, never a copy of a live store (box_fixture.py); its world dir where the box reads it
  python3 "$HERE/box_fixture_load.py" "$B/stores" > "$B/fixture.json" \
    && cp -a "$B/stores/world/." "$B/world/" && rm -rf "${B:?}/stores/world" \
    && printf '{"fixture": true, "port": %s, "code": "%s", "profile": "%s"}\n' "$port" "$rev" "$profile" > "$B/manifest.json" \
    || { echo "ABORT: loading the fixture failed" >&2; rm -rf "${B:?}"; exit 1; }
else
  python3 "$HERE/sandbox_copy.py" "$B" "$LIVE" "$MEMORY_LIVE" "$port" "$rev" "$jev" "$cloud" "$profile"
fi
[ -f "$B/manifest.json" ] || { echo "ABORT: copying the stores failed" >&2; rm -rf "${B:?}"; exit 1; }

set -a; . "$AGENT_ENV"; set +a
cp "$HERE/sandbox_ha_stub.py" "$B/ha_stub.py"
# Every background process is detached from the caller's stdin/stdout, or `up | tail` (and ssh) never return.
# --first-run: an HA out of the box too (#411's "Connect the house" makes its owner and token), so no token file yet
stub_mode=(); [ "$first_run" = 1 ] && stub_mode=(--fresh)
python3 "$B/ha_stub.py" "$stub" "${stub_mode[@]}" < /dev/null > "$B/ha-stub.log" 2>&1 & echo $! > "$B/stub.pid"
[ "$first_run" = 1 ] || echo "stub-token-not-real" > "$B/ha-token"

unset_env=(-u TITAN_ALERT_NTFY_URL -u TITAN_ALERT_WEBHOOK)
extra=()
if [ "$jev" = off ]; then
  unset_env+=(-u JEV_API_KEY -u JEV_AUTH -u JEV_BASE_URL -u JEV_COORDINATOR_KEY_FILE -u JEV_COORDINATOR_ID)
  extra+=(AGENT_JEV_ROUTER=0 AGENT_JEV_TURN_GATES=0)
fi
if [ "$cloud" = off ]; then
  unset_env+=(-u GEMINI_API_KEY)
  extra+=(AGENT_TOOL_BRAIN=local)
fi
S=$B/stores
household_db="$S/household.db"
# #1399: the box's own graph (sandbox_copy copies every connectors store, graph.db included), and its one A/B switch
extra+=(GRAPH_DB="$S/graph.db" AGENT_FE_GRAPH=$([ "$fe_graph" = on ] && echo 1 || echo 0))
if [ "$fe_graph" = on ]; then echo "fe_graph=on" > "$B/switches-extra"; fi
hide=()
if [ "$fixture" = 1 ]; then
  # Only the fixture is reachable: BOX_FIXTURE (box_fixture.py: eval_as may name its people), the fixture's own stores
  # for every store titan reads, no runtime but a dead port, and the live data dirs not even visible (tmpfs over them),
  # so a module that falls back to a live default path finds nothing.
  extra+=(BOX_FIXTURE=1 AGENT_FENCES=1 AGENT_TZ="America/Los_Angeles" LINKS_DB="$S/links.db" LINK_SALT_FILE="$S/salt"
          MAIL_SERVER_DB="$S/mail_server.db" REFERENCES_DB="$S/references.db" MAIL_SUGGESTIONS_DB="$S/sugg.db"
          MAIL_SERVER=0)
  # (Box's review of #1463: the live ~/.config/jellybean memory dir is hidden too, as every box's MEMORY_DB is pinned in
  # the launch below (#1464); mail off, since its accounts dir is visible.)
  for r in RETRIEVAL FETCH RECORD DOCUMENT EVENT WORKFLOW EPISTEMIC INGESTION; do extra+=("TITAN_${r}_URL=http://127.0.0.1:9"); done
  hide=(--tmpfs /ai/datasets --tmpfs /ai/logs --tmpfs /ai/projects/titan-local-agent --tmpfs "$HOME/.config/jellybean")
fi
if [ "$first_run" = 1 ]; then
  household_db="$S/household-first-run.db"; mkdir -p "$B/box-setup"
  extra+=(BOX_SETUP=1 BOX_SETUP_DIR="$B/box-setup" TITAN_PAIR_URLS="http://127.0.0.1:$port" BOX_HA_URL="http://127.0.0.1:$stub")
  # A box out of the carton has no runtimes of its own yet: never the live ones (a dead port, as the deploy gate does)
  for r in RETRIEVAL FETCH RECORD DOCUMENT EVENT WORKFLOW EPISTEMIC INGESTION; do extra+=("TITAN_${r}_URL=http://127.0.0.1:9"); done
  # its backup keys and backups are its own, in the box dir (the defaults are titan's /ai/config, read-only in here)
  extra+=(BOX_BACKUP_PUB="$B/box-backup.pub.json" BOX_BACKUP_SIGN_KEY="$B/box-backup.sign.key"
          BOX_BACKUP_DIR="$B/box-backups" BOX_BACKUP_STAGE="$B/box-backup-stage")
fi
launch=(env "${unset_env[@]}" "${extra[@]}" \
  HA_URL="http://127.0.0.1:$stub" HA_TOKEN_FILE="$B/ha-token" GITHUB_TOKEN_FILE="$B/no-gh" APNS_KEY_FILE= APNS_KEY_ID= APNS_TEAM_ID= \
  AGENT_TASK_AUTONOMY=0 AGENT_TASK_AUTONOMY_OWNERS= TITAN_DISCOVERY=0 \
  AGENT_TURN_LOG="$B/turns.jsonl" AGENT_MEMORY_DIR="$B/mem" MEMORY_DB="$B/mem/titan_memory.db" CONNECTORS_DIR="$S" \
  DAILY_LOG_DIR="$B/daily" \
  HOUSEHOLD_DB="$household_db" KB_ACL_DB="$S/kb_acl.db" PLACES_DB="$S/places.db" ROUTINES_DB="$S/routines.db" \
  TODOS_DB="$S/todos.db" WORLD_DB="$S/world.db" WORLD_DIR="$B/world" FENCES_DB="$S/fences.db" TASKS_DB="$S/tasks.db" \
  GRANTS_DB="$S/grants.db" GRANTS_AUDIT="$B/grants-audit.jsonl" HELD_ACTIONS_DB="$S/held_actions.db" ROOMS_DB="$S/rooms.db" \
  CLAUDE_CHATS_DB="$S/claude_chats.db" MAIL_BILLS_DB="$S/mail_bills.db" REFLECTION_DB="$S/reflection.db" \
  GITHUB_STARS_DB="$S/github_stars.db" \
  CONNECTORS_LEDGER="$S/ledger.db" CONTINUITY_DB="$S/continuity.db" TASK_LOG_DB="$S/task_log.db" \
  HOUSEHOLD_PREFS="$S/household.json" PROACTIVE_DB="$S/proactive-empty.db" SCHEDULED_DB="$S/scheduled-empty.db" \
  PUSH_DB="$S/push-empty.db" REVIEW_DB="$S/review.db" DEVICE_HEALTH_STATE="$B/device_health.json" \
  bwrap --dev-bind / / --ro-bind /ai/datasets /ai/datasets --ro-bind /ai/logs /ai/logs --ro-bind /ai/config /ai/config \
    --ro-bind /ai/projects /ai/projects --ro-bind /ai/secrets /ai/secrets --ro-bind "$HOME/.config" "$HOME/.config" \
    ${hide[@]+"${hide[@]}"} --chdir "$B/code" python3 agent_api_server.py --host 127.0.0.1 --port "$port" --no-discovery)
# The exact launch, kept so `restart` brings the same API back over the same stores (#411's day-one check restarts
# between "remember" and "recall"). Paths and switches only: secrets come from sourcing agent.env at launch.
{ echo '#!/bin/bash'; printf '%q ' "${launch[@]}"; echo; } > "$B/launch.sh"
chmod 700 "$B/launch.sh"
bash "$B/launch.sh" < /dev/null >> "$B/api.log" 2>&1 &
echo $! > "$B/box.pid"
for _ in $(seq 1 60); do curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null && break; sleep 1; done
# bwrap doesn't pass a kill on to the server: keep the listener's own pid too.
ss -ltnp | grep ":$port " | grep -o "pid=[0-9]*" | head -1 | cut -d= -f2 > "$B/box-py.pid"
if ! curl -sf -m 2 "http://127.0.0.1:$port/health" >/dev/null || [ ! -s "$B/box-py.pid" ]; then
  echo "ABORT: the box API didn't start" >&2; tail -20 "$B/api.log" >&2; down 0; exit 1
fi

if [ "$watchdog" = 1 ]; then
  ( hot_n=0
    while [ -s "$B/box-py.pid" ] && kill -0 "$(cat "$B/box-py.pid")" 2>/dev/null; do
      t=$(nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader 2>/dev/null | sort -n | tail -1)
      echo "$(date -u +%H:%M:%S) ${t:-?}" >> "$B/gpu-temp.log"
      if [ -n "$t" ] && [ "$t" -ge "$hot" ]; then hot_n=$((hot_n + 1)); else hot_n=0; fi
      if [ "$hot_n" -ge 2 ]; then echo "GPU HOT $t: stopping the box" >> "$B/gpu-temp.log"; "$HERE/sandbox_box.sh" down "$port" --keep; break; fi
      sleep 60
    done ) < /dev/null > /dev/null 2>&1 &
  echo $! > "$B/watchdog.pid"
fi
echo "box $port up: profile $profile, code $rev, jev $jev, cloud $cloud, stub $stub, turn log $B/turns.jsonl"
