#!/bin/bash
# Gate a worktree through titan's gate queue, from the Mac (#945): wait for NAME's turn, rebase onto origin/main,
# ship the committed tree to titan, run run_deploy_gate.sh there, release the lane.
#
#   bash scripts/gate_run.sh NAME [WORKTREE] [LOG]     (WORKTREE: default this checkout; LOG: default stdout)
#
# Exit 0 when the gate passed, 1 when it failed or didn't run (rebase conflict, uncommitted changes), 2 on usage.
# - Only gate_queue.sh's own wait loop touches the queue's markers; the eval windows are the queue's job, so this
#   never waits on a clock itself (10-08: a runner's "hold until 10:00Z" loop stalled the queue after midnight).
# - It refuses a dirty worktree before waiting, since the rebase at its turn would fail and waste the slot.
# - After a rebase that moved the branch, push with --force-with-lease; this never pushes.
# - It beats (#1407): once before its wait, then every GATE_BEAT_S (60) seconds until it exits, so a wait or lane
#   it leaves behind (this runner killed after its wait reached titan) is cleared within the queue's beat limit
#   (5 min) instead of being held by a shared sshd's PID. An older queue without `beat` just ignores it.
# GATE_HOST=local runs the titan side in this shell (the tests); GATE_QUEUE is the queue script on that host.
# GATE_JOBS (default OPS_GATE_JOBS, ops_defaults.sh): tests at a time on titan (run_deploy_gate.sh); 10-10: 507 s one at a time, 194 s at 4, same verdicts.
# #1514: the gate log also carries one "time <seconds> <file>" line per test and the 10 slowest at the end.
# GATE_PR=<number> (#1519): when the gate passes, merge that PR (coord/automerge.sh: open, not draft, labelled ready).
. "$(cd "$(dirname "$0")" && pwd)/ops_defaults.sh"
NAME=$1 WT=${2:-$(cd "$(dirname "$0")/.." && pwd)} LOG=$3
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "usage: gate_run.sh NAME [WORKTREE] [LOG]  (NAME: lowercase, digits, dashes)" >&2; exit 2; }
HOST=${GATE_HOST:-titan}
GQ=${GATE_QUEUE:-/ai/tmp/gate-queue.sh}
TMP=${GATE_TMP:-/ai/tmp}
G=$TMP/gate-$NAME
[ -n "$LOG" ] && exec >> "$LOG" 2>&1
on_host() { if [ "$HOST" = local ]; then bash -c "$1"; else ssh -o ServerAliveInterval=30 "$HOST" "$1"; fi; }
cd "$WT" 2>/dev/null && git rev-parse --git-dir >/dev/null 2>&1 || { echo "$NAME: $WT is not a git checkout" >&2; exit 2; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "$NAME: uncommitted changes in $WT; commit first (the rebase at its turn would refuse), not queued"; exit 1
fi
echo "[$(date -u +%H:%MZ)] $NAME: waiting for its turn"
on_host "$GQ beat $NAME" </dev/null >/dev/null 2>&1   # before the wait, so an old beat file from a crashed run never reads stale
# The beat loop must outlive its own sleep: `while sleep N` ended silently when anything killed the sleep, and on 10-11
# five queued gates lost their beat that way and left the queue after 5 minutes. It now stops only when this runner
# is gone. The beats read nothing (</dev/null), so a background ssh never shares stdin with the waiting ssh below.
# Its stdout and stderr go to /dev/null (a sleep holding a captured pipe kept it open, #1588) and TERM stops the sleep too.
( trap 'kill $nap 2>/dev/null; exit 0' TERM
  while :; do sleep "${GATE_BEAT_S:-60}" & nap=$!; wait $nap || sleep 1; kill -0 $$ 2>/dev/null || exit 0
    on_host "$GQ beat $NAME" </dev/null >/dev/null 2>&1; done ) </dev/null >/dev/null 2>&1 &
BEATER=$!
trap 'kill $BEATER 2>/dev/null' EXIT
on_host "$GQ wait $NAME" || { echo "$NAME: the queue refused or the wait failed"; exit 1; }
release() { kill $BEATER 2>/dev/null; on_host "$GQ done $NAME; rm -rf ${G:?} ${G:?}.log"; }
trap release EXIT
before=$(git rev-parse HEAD)
git fetch -q origin && git rebase -q origin/main || { git rebase --abort 2>/dev/null; echo "$NAME: REBASE CONFLICT, not gated"; exit 1; }
[ "$(git rev-parse HEAD)" != "$before" ] && echo "$NAME: rebased onto origin/main; push with --force-with-lease"
echo "$NAME: head $(git log --oneline -1)"
# docs/api-routes.md is generated: a rebase onto a main with new routes leaves it stale, which failed whole gates
# (10-10: box-1452r, then #1452 again). Regenerate it here as one commit, gate that, and say so: the owner pushes
# the rebased head with --force-with-lease as after any rebase. If it still isn't current, stop before the tests.
if [ -f scripts/api_routes.py ] && ! python3 scripts/api_routes.py --check >/dev/null 2>&1; then
  python3 scripts/api_routes.py >/dev/null 2>&1 && git add docs/api-routes.md \
    && git -c user.name="$(git log -1 --format=%an)" -c user.email="$(git log -1 --format=%ae)" \
         commit -q -m "docs/api-routes.md regenerated after the rebase" \
    && echo "$NAME: docs/api-routes.md regenerated after the rebase (one commit); push with --force-with-lease"
  if ! python3 scripts/api_routes.py --check >/dev/null 2>&1; then
    echo "$NAME: docs/api-routes.md is still out of date: run python3 scripts/api_routes.py, commit, push; not gated"
    exit 1
  fi
  echo "$NAME: head $(git log --oneline -1)"
fi
TREE=$(git rev-parse 'HEAD^{tree}')   # #1514: a pass is recorded by tree, so a deploy of this same tree can reuse it
on_host "rm -rf ${G:?} && mkdir -p $G" && git archive HEAD | on_host "tar -x -C $G" || { echo "$NAME: couldn't ship the tree"; exit 1; }
# #1514: the tests this diff can affect run first, and the gate stops at the first failure among them (run_deploy_gate.sh
# GATE_FIRST); the rest of the plan runs after, so the verdict and the count are the full gate's. Chosen on the Mac by
# scripts/affected_tests.py from the diff against origin/main; if that fails or finds none, the plan runs as it is.
FIRST=""
CHANGED=$(git diff --name-only origin/main...HEAD 2>/dev/null)
if [ -n "$CHANGED" ] && [ -f scripts/affected_tests.py ]; then
  FIRST=$(printf '%s\n' "$CHANGED" | tr '\n' '\0' | xargs -0 python3 scripts/affected_tests.py 2>/dev/null)
fi
if [ -n "$FIRST" ] && printf '%s\n' "$FIRST" | on_host "cat > $G/gate-first"; then
  echo "$NAME: affected first: $(printf '%s\n' "$FIRST" | wc -l | tr -d ' ') tests"; GF="GATE_FIRST=$G/gate-first "
else GF=""; fi
on_host "cd $G && ${GF}GATE_JOBS=${GATE_JOBS:-$OPS_GATE_JOBS} timeout 1800 bash scripts/run_deploy_gate.sh > $G.log 2>&1; rc=\$?; echo exit=\$rc; echo ok=\$(grep -c '^ok' $G.log);
  [ \$rc -eq 0 ] && [ -f scripts/gate_passed.sh ] && bash scripts/gate_passed.sh add $TREE \$(grep -c '^ok' $G.log) $NAME;
  sed -n '/^slowest 10/,+10p' $G.log;   # #1514's per-test times live only in $G.log on titan, removed after the run
  grep -E '^FAIL' $G.log | cut -c1-200 | head -20; [ \$rc -ne 0 ] && grep -vE '^(ok|FAIL|time )' $G.log | cut -c1-200 | head -12; exit \$rc"
rc=$?
echo "[$(date -u +%H:%MZ)] $NAME: GATE $([ $rc -eq 0 ] && echo PASSED || echo FAILED)"
# #1516: when titan's hourly main check has main red, say so, so a failure in those tests isn't blamed on this PR
if [ $rc -ne 0 ]; then red=$(on_host "cat /ai/tmp/main.red 2>/dev/null"); [ -n "$red" ] && \
  echo "$NAME: note: origin/main is red on its own (main-health: ${red}); a failure only in those tests is main's"; fi
# #1519: GATE_PR=<number> merges the PR right here when it passed and its owner labelled it `ready` (coord/automerge.sh
# says why not otherwise); the lane is released first, so a merge never holds the queue.
if [ $rc -eq 0 ] && [ -n "${GATE_PR:-}" ]; then
  release; trap - EXIT
  bash "$(cd "$(dirname "$0")" && pwd)/coord/automerge.sh" "$GATE_PR" "$WT" "$before" || true
fi
[ $rc -eq 0 ]
