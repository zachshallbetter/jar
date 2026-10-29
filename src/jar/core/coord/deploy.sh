#!/bin/bash
# Coordination deploy (moved from a session scratchpad, 10-10): take the solo-coord-deploy turn in titan's gate queue
# (holding both lanes, with a heartbeat), refuse in an eval window, under a deploy hold or during model-eval, then pull
# main and run preflight + deploy from this Mac checkout. Logs go to $COORD_LOG_DIR (default /tmp/coord-deploy).
REPO=${TITAN_NODE_REPO:-$HOME/Projects/titan-node}
LOGS=${COORD_LOG_DIR:-/tmp/coord-deploy}; mkdir -p "$LOGS"
cd "$REPO" || exit 1
ssh titan '/ai/tmp/gate-queue.sh beat solo-coord-deploy' >/dev/null 2>&1
# stdout/stderr to /dev/null and TERM stops the sleep too (#1588); GATE_BEAT_S tunes the interval.
( trap 'kill $nap 2>/dev/null; exit 0' TERM
  while :; do sleep "${GATE_BEAT_S:-60}" & nap=$!; wait $nap || sleep 1; kill -0 $$ 2>/dev/null || exit 0
    ssh titan '/ai/tmp/gate-queue.sh beat solo-coord-deploy' </dev/null >/dev/null 2>&1; done ) </dev/null >/dev/null 2>&1 &
BEATER=$!
trap 'kill $BEATER 2>/dev/null; ssh titan "/ai/tmp/gate-queue.sh done solo-coord-deploy" >/dev/null 2>&1' EXIT
ssh titan 'echo solo-coord-deploy >> /ai/tmp/gate.queue; /ai/tmp/gate-queue.sh wait solo-coord-deploy' || { echo "queue wait failed"; exit 1; }
# #1518: the one list of windows, asked of this checkout's queue script (the installed copy may predate `window`)
w=$(GATE_QUEUE_DIR=/nonexistent-gq bash scripts/gate_queue.sh window 2>&1); rc=$?
[ $rc -eq 0 ] && { echo "$w, abort"; exit 1; }
[ $rc -eq 1 ] || { echo "couldn't check the eval windows (rc $rc: $w), abort"; exit 1; }
ssh titan 'ls /ai/tmp/deploy.hold 2>/dev/null && echo HOLD; systemctl show -p ActiveState --value titan-model-eval.service' | grep -qE 'HOLD|activating|^active' && { echo "busy/hold, abort"; exit 1; }
git pull -q --ff-only && echo "deploying $(git rev-parse --short HEAD)"
bash scripts/titan_preflight.sh > "$LOGS/preflight.log" 2>&1; echo "preflight rc=$?"; tail -3 "$LOGS/preflight.log"
bash scripts/deploy_titan_node.sh > "$LOGS/deploy.log" 2>&1; echo "deploy rc=$?"; tail -8 "$LOGS/deploy.log"
echo DEPLOYDONE
