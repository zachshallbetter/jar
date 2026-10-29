#!/usr/bin/env bash
# A truth_eval A/B on titan with its own A/A control (titan-node#1144): what #1064's four arms re-derived in /ai/tmp.
#
#   ab_run.sh --a REV|DIR --b REV|DIR --name NAME [--profile live|box] [--b-profile live|box] [--passes 3]
#             [--set all|wants_schedule] [--start HH:MM] [--hold-until HH:MM] [--no-control] [--sequential]
#             [--queue NAME]
#
# --sequential   one box at a time (titan-node#1250, runbook #1254): each pass brings up A, runs iPhone then Mac, takes it
#                down, then B, then C. Retry-dependent cases need it: three boxes on one GPU starve a retry's second call.
# --queue NAME   take titan's gate lanes through /ai/tmp/gate-queue.sh (a solo-… name must be listed there first) before
#                starting, and give them back however the run ends.
# --b-profile    B's profile when it differs from A's (#402's box-vs-live readout: --a REV --b REV --profile live
#                --b-profile box). The control C always runs A's code and profile.
#
# Three boxes from scripts/sandbox_box.sh, each a `git archive` of its rev's scripts/ from the deploy checkout:
# A (before) on 7773, B (after) on 7776, C (A's code again: the control) on 7779. Each pass runs every arm side by side
# for iPhone, then Mac (same load on all three), truth_eval --brain both --jsonl into /ai/tmp/ab-<NAME>/
# <ARM>-<n>-<client>.jsonl (0600). The harness is the deploy checkout's truth_eval, so all arms are checked the same way.
# Waits for --start (UTC), writes the deploy hold (max of what's there and --hold-until), refuses inside the eval windows
# (scripts/eval_windows.conf), and at the end takes every box down, prints the readout (scripts/ab_readout.py: counts
# only, with the A/A band) to readout.txt, deletes the per-turn files (they hold replies), and removes the hold only if
# it's still its own. Detach it: ssh titan 'nohup setsid bash …/ab_run.sh … >/dev/null 2>&1 </dev/null &'
set -uo pipefail
A="" B="" NAME="" PROFILE=live BPROFILE="" PASSES=3 SET=all START="" HOLD="" CONTROL=1 SEQ=0 QUEUE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --a) A=$2; shift 2;; --b) B=$2; shift 2;; --name) NAME=$2; shift 2;; --profile) PROFILE=$2; shift 2;;
    --passes) PASSES=$2; shift 2;; --set) SET=$2; shift 2;; --start) START=$2; shift 2;; --hold-until) HOLD=$2; shift 2;;
    --no-control) CONTROL=0; shift;; --sequential) SEQ=1; shift;; --queue) QUEUE=$2; shift 2;;
    --b-profile) BPROFILE=$2; shift 2;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
[ -n "$A" ] && [ -n "$B" ] && [ -n "$NAME" ] || { echo "usage: ab_run.sh --a REV --b REV --name NAME …" >&2; exit 2; }
[[ "$NAME" =~ ^[a-z0-9-]+$ ]] || { echo "--name: lowercase letters, digits and dashes" >&2; exit 2; }
REPO=${AB_REPO:-/ai/projects/titan-node}
D=${AB_DIR:-/ai/tmp/ab-$NAME}
SB=$REPO/scripts/sandbox_box.sh
TE=$REPO/scripts/truth_eval.py
GQ=${AB_GATE_QUEUE:-/ai/tmp/gate-queue.sh}
HOLDF=${AB_HOLD_FILE:-/ai/tmp/deploy.hold}   # tests point these at temp files (the gate runs on titan)
SETTLE=${AB_SETTLE_S:-20}                    # seconds a fresh box gets before its first question
mkdir -p "$D" && chmod 700 "$D"
LOG=$D/run.log
in_window() { [ "${AB_IGNORE_WINDOWS:-0}" = 1 ] && return 1; bash "$(dirname "$0")/gate_queue.sh" window 0 >/dev/null; }   # #1518
# the next occurrence of HH:MM (UTC) at or after now: tomorrow's when today's has passed (#1144: a bare HHMM compare
# started a 10:15 run at 23:27)
next_at() { local t; t=$(date -u -d "today $1" +%s) || return 1; [ "$t" -lt "$(date -u +%s)" ] && t=$((t + 86400)); echo "$t"; }
if [ -n "$START" ]; then
  start_at=$(next_at "$START") || { echo "--start: HH:MM" >&2; exit 2; }
  echo "waiting for $(date -u -d @"$start_at" +'%m-%d %H:%M')Z" >> "$LOG"
  while [ "$(date -u +%s)" -lt "$start_at" ]; do sleep 20; done
fi
if [ -n "$QUEUE" ]; then
  echo "waiting for the gate queue as $QUEUE" >> "$LOG"
  "$GQ" wait "$QUEUE" >> "$LOG" 2>&1 || { echo "gate queue refused $QUEUE" >> "$LOG"; exit 1; }
fi
mine=()   # the boxes this run brought up: its trap downs only these (10-07: a runner's trap downed another's boxes)
cleanup() {
  for p in "${mine[@]}"; do bash "$SB" down "$p" >> "$LOG" 2>&1; done
  [ -n "$QUEUE" ] && "$GQ" done "$QUEUE" >> "$LOG" 2>&1
  return 0
}
trap cleanup EXIT
if in_window; then echo "refused: inside an eval window $(date -u +%H:%M)Z" >> "$LOG"; exit 1; fi
arms=(A B); [ "$CONTROL" = 1 ] && arms+=(C)
declare -A REV=([A]=$A [B]=$B [C]=$A) PORT=([A]=7773 [B]=7776 [C]=7779) PROF=([A]=$PROFILE [B]=${BPROFILE:-$PROFILE} [C]=$PROFILE)
# each arm's profile, for the readout: a box-profile arm has no Gemini, so its gemini rows aren't a comparison (#402)
printf '{"A": "%s", "B": "%s", "C": "%s"}\n' "${PROF[A]}" "${PROF[B]}" "${PROF[C]}" > "$D/profiles.json"
for arm in "${arms[@]}"; do   # a rev of the deploy checkout, or a directory holding scripts/ (an unmerged branch's archive)
  mkdir -p "$D/code-$arm"
  if [ -d "${REV[$arm]}/scripts" ]; then cp -a "${REV[$arm]}/scripts" "$D/code-$arm/"
  else git -C "$REPO" archive "${REV[$arm]}" scripts | tar -x -C "$D/code-$arm" \
    || { echo "archive ${REV[$arm]} failed" >> "$LOG"; exit 1; }; fi
done
until=$(( $(date -u +%s) + 3 * 3600 ))
if [ -n "$HOLD" ]; then until=$(next_at "$HOLD") || { echo "--hold-until: HH:MM" >&2; exit 2; }; fi
if [ -f "$HOLDF" ]; then read -r cur _ < "$HOLDF" || cur=0; [ "${cur:-0}" -gt "$until" ] && until=$cur; fi
TAG="ab-$NAME"
echo "$until $TAG" > "$HOLDF"
echo "hold until $(date -u -d @"$until" +%H:%M)Z ($TAG); arms ${arms[*]}; A=$A B=$B; profiles A=${PROF[A]} B=${PROF[B]};"\
     " set $SET; $([ "$SEQ" = 1 ] && echo sequential || echo side by side)" >> "$LOG"
up() {   # one arm's box, recorded as this run's own
  bash "$SB" up "${PORT[$1]}" --profile "${PROF[$1]}" --code "$D/code-$1/scripts" >> "$LOG" 2>&1 \
    || { echo "$1 up failed" >> "$LOG"; exit 1; }
  mine+=("${PORT[$1]}")
}
down() {
  bash "$SB" down "${PORT[$1]}" >> "$LOG" 2>&1
  local keep=() p; for p in "${mine[@]}"; do [ "$p" != "${PORT[$1]}" ] && keep+=("$p"); done; mine=("${keep[@]}")
}
evals() {   # evals ARM N CLIENT: one truth_eval for one arm (run in the background by the side-by-side mode)
  local f=$D/$1-$2-$3.jsonl; : > "$f"; chmod 600 "$f"
  TURN_EVAL_API=http://127.0.0.1:${PORT[$1]} timeout 3000 python3 "$TE" --brain both --client "$3" --set "$SET" \
    --jsonl "$f" > /dev/null 2>&1
}
if [ "$SEQ" = 1 ]; then
  for n in $(seq 1 "$PASSES"); do
    for arm in "${arms[@]}"; do
      if in_window; then echo "stopped: an eval window began $(date -u +%H:%M)Z" >> "$LOG"; break 2; fi
      up "$arm"; sleep "$SETTLE"
      for client in iphone mac; do
        echo "pass $n $arm $client start $(date -u +%H:%M:%S)Z" >> "$LOG"
        evals "$arm" "$n" "$client"
      done
      down "$arm"
    done
  done
else
  for arm in "${arms[@]}"; do up "$arm"; done
  sleep "$SETTLE"
  for n in $(seq 1 "$PASSES"); do
    for client in iphone mac; do
      if in_window; then echo "stopped: an eval window began $(date -u +%H:%M)Z" >> "$LOG"; break 2; fi
      echo "pass $n $client start $(date -u +%H:%M:%S)Z" >> "$LOG"
      for arm in "${arms[@]}"; do evals "$arm" "$n" "$client" & done
      wait
      echo "pass $n $client end $(date -u +%H:%M:%S)Z" >> "$LOG"
    done
  done
  for arm in "${arms[@]}"; do down "$arm"; done
fi
python3 "$REPO/scripts/ab_readout.py" "$D" --set "$SET" > "$D/readout.txt" 2>> "$LOG"
rm -f -- "$D"/[ABC]-*.jsonl
rm -rf -- "$D"/code-*
if [ -f "$HOLDF" ] && read -r _ tag < "$HOLDF" && [ "$tag" = "$TAG" ]; then rm -f "$HOLDF"; fi
echo "done $(date -u +%H:%M:%S)Z" >> "$LOG"
