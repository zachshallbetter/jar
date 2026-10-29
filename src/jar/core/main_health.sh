#!/usr/bin/env bash
# titan-main-health (#1516): is origin/main green? Hourly, when the gate queue is idle, titan gates main's newest
# commit itself, so a red main is known within the hour without waiting for someone's PR gate to fail on it.
# (10-10: test_consolidate turned main red at 19:00Z with no merge; the next two PR gates failed on it and one
# session spent its time bisecting merges that weren't the cause.)
#
#   main_health.sh run       fetch main into refs/main-health/main (never touches origin/* or the deploy checkout's
#                            files); skip when this head was checked in the last day, the queue is busy or an eval
#                            window is near; else take a lane, run run_deploy_gate.sh (OPS_GATE_JOBS at a time) on an archive of
#                            it, release the lane, and record the verdict:
#                              /ai/logs/main-health.jsonl   one line per check {ts, head, rc, ok, fail}
#                              /ai/tmp/main.red              "<head> <failing test files…>" while main is red
#                            and alert on change only (red, still red with other tests, green again).
#   main_health.sh red       print main.red, if any (gate_run.sh reads it to say a failure is main's, not the PR's)
#   main_health.sh install   the deploy's step: install server-setup/titan-main-health.{service,timer} when they
#                            differ, daemon-reload, enable --now the timer (never runs the service itself)
#
# A fix PR still merges while main is red: gate_run.sh rebases onto main and runs everything, so a PASS means the PR
# makes main green. Nothing here blocks a merge. MAIN_HEALTH_* variables are for the tests.
set -u
. "$(cd "$(dirname "$0")" && pwd)/ops_defaults.sh"   # OPS_GATE_JOBS (#1518)
REPO=${MAIN_HEALTH_REPO:-/ai/projects/titan-node}
D=${MAIN_HEALTH_DIR:-/ai/tmp/main-health}
LOG=${MAIN_HEALTH_LOG:-/ai/logs/main-health.jsonl}
RED=${MAIN_HEALTH_RED:-/ai/tmp/main.red}
GQ=${MAIN_HEALTH_QUEUE:-/ai/tmp/gate-queue.sh}
QDIR=${MAIN_HEALTH_QUEUE_DIR:-/ai/tmp}
RECHECK_MIN=${MAIN_HEALTH_RECHECK_MIN:-1440}   # the same head is re-gated once a day (a clock bomb needs no commit)
NAME=main-health

alert() {   # alert KEY TEXT: edge-triggered (titan_alert.EdgeAlert); an empty KEY means green
  if [ -n "${MAIN_HEALTH_ALERT:-}" ]; then "$MAIN_HEALTH_ALERT" "$1" "$2"; return; fi
  MH_KEY="$1" MH_TEXT="$2" python3 - "$REPO/scripts" <<'PY' || true
import os, sys
sys.path.insert(0, sys.argv[1])
from titan_alert import EdgeAlert
k = os.environ["MH_KEY"]
EdgeAlert("main-health", "Titan main is red").update({k: os.environ["MH_TEXT"]} if k else {})
PY
}

case "${1:-}" in
  run)
    mkdir -p "$D" || exit 1
    git -C "$REPO" fetch -q --refmap= origin +refs/heads/main:refs/main-health/main 2>/dev/null \
      || { echo "main health: couldn't fetch main (skipped)"; exit 0; }
    head=$(git -C "$REPO" rev-parse --short=8 refs/main-health/main) || exit 1
    if [ "$head" = "$(cat "$D/last-head" 2>/dev/null)" ] && \
       [ $(( $(date +%s) - $(date -r "$D/last-head" +%s 2>/dev/null || echo 0) )) -lt $(( RECHECK_MIN * 60 )) ]; then
      echo "main health: $head already checked"; exit 0
    fi
    if [ -s "$QDIR/gate.queue" ] || [ -d "$QDIR/gate.lock" ] || [ -d "$QDIR/gate.lock.2" ]; then
      echo "main health: the gate queue is busy (skipped; next hour)"; exit 0
    fi
    w=$("$GQ" window 2>/dev/null) && { echo "main health: $w (skipped)"; exit 0; }
    tree=$(mktemp -d "$D/tree.XXXXXX") || exit 1
    "$GQ" beat "$NAME" >/dev/null 2>&1
    # The loop and its sleep write to /dev/null: a sleep holding the caller's captured stdout kept the pipe open ~60 s (#1588).
    # TERM stops the sleep too, so nothing outlives this script.
    ( trap 'kill $nap 2>/dev/null; exit 0' TERM
      while :; do sleep "${GATE_BEAT_S:-60}" & nap=$!; wait $nap || sleep 1; kill -0 $$ 2>/dev/null || exit 0
        "$GQ" beat "$NAME" >/dev/null 2>&1; done ) </dev/null >/dev/null 2>&1 &
    beater=$!
    trap 'kill $beater 2>/dev/null; "$GQ" done "$NAME" >/dev/null 2>&1; rm -rf "$tree"' EXIT
    "$GQ" wait "$NAME" >/dev/null 2>&1 || { echo "main health: no lane (skipped)"; exit 0; }
    git -C "$REPO" archive refs/main-health/main | tar -x -C "$tree" || exit 1
    to=(); command -v timeout >/dev/null && to=(timeout 1800)
    out=$(GATE_JOBS="${GATE_JOBS:-$OPS_GATE_JOBS}" ${to[@]+"${to[@]}"} bash "$tree/scripts/run_deploy_gate.sh" "$tree" 2>&1); rc=$?
    ok=$(grep -c '^ok ' <<<"$out")
    fail=$(grep -oE '^FAIL scripts/test_[A-Za-z0-9_]+\.py' <<<"$out" | awk '{print $2}' | sort -u | tr '\n' ' ' | sed 's/ $//')
    printf '{"ts": "%s", "head": "%s", "rc": %d, "ok": %d, "fail": "%s"}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$head" "$rc" "$ok" "$fail" >> "$LOG" 2>/dev/null
    echo "$head" > "$D/last-head"
    if [ "$rc" = 0 ]; then
      rm -f "$RED"; alert "" ""
      echo "main health: $head green (ok=$ok)"
    else
      [ -n "$fail" ] || fail="(rc $rc: no test named; see $LOG)"
      echo "$head $fail" > "$RED"
      alert "red:$fail" "origin/main $head fails: $fail. A PR gate that fails only on these is main's, not the PR's."
      echo "main health: $head RED: $fail"
    fi ;;
  red) [ -s "$RED" ] && cat "$RED" ;;
  install)
    SYSTEMCTL=${SYSTEMCTL:-systemctl}; SUDO=${SUDO-sudo -n}; DIR=${SYSTEMD_DIR:-/etc/systemd/system}
    SRC="$(cd "$(dirname "$0")/.." && pwd)/server-setup"; changed=0
    for u in titan-main-health.service titan-main-health.timer; do
      [ -f "$SRC/$u" ] || { echo "main health unit: $SRC/$u missing" >&2; exit 1; }
      cmp -s "$SRC/$u" "$DIR/$u" && continue
      $SUDO install -m 0644 "$SRC/$u" "$DIR/$u" || { echo "main health unit: couldn't install $u" >&2; exit 1; }
      changed=1
    done
    [ "$changed" = 1 ] && { $SUDO $SYSTEMCTL daemon-reload || exit 1; }
    if ! $SYSTEMCTL is-enabled --quiet titan-main-health.timer 2>/dev/null || \
       ! $SYSTEMCTL is-active --quiet titan-main-health.timer 2>/dev/null; then
      $SUDO $SYSTEMCTL enable --now titan-main-health.timer || { echo "main health unit: couldn't enable the timer" >&2; exit 1; }
      changed=1
    fi
    [ "$changed" = 1 ] && echo "main health timer installed" || echo "main health timer current" ;;
  *) echo "usage: $0 run|red|install" >&2; exit 2 ;;
esac
