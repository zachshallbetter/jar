#!/bin/bash
# The titan gate queue (Coordination, 10-07; in the repo since 10-09, #945): who takes a gate lane next, in order.
#   gate_queue.sh wait <name>   blocks until <name> is the first *waiting* name in gate.queue and a lane is free,
#                               then holds it. A name whose waiter isn't running is skipped, never a stall.
#   gate_queue.sh done <name>   releases the lane and drops <name>'s line, only if <name> holds it (safe in any trap)
#   gate_queue.sh drop <name>   takes <name> out of the queue
#                               done and drop leave a tombstone (gate.gone/<name>): a wait for <name> that started
#                               before it leaves the queue instead of ever taking a lane (10-10: a `done` while the name
#                               still waited remotely removed its beat file; the orphaned wait then took lane 1 and held
#                               it 50 min, unbeaten, its PID the shared sshd). A wait started after it is unaffected.
#   gate_queue.sh show          the queue and both lanes, with "*" on names whose waiter is waiting now
#   gate_queue.sh status        one line each: the lanes (holder, minutes held), the waiters (in line order, minutes
#                               waited), and the listed order (holding, waiting, or idle with its idle minutes)
#   gate_queue.sh beat <name>   the caller's heartbeat (#1407): touches gate.beat/<name>. A caller that beats (gate_run.sh,
#                               once before its wait and every minute after, until done) is alive; once its beat is
#                               GATE_QUEUE_BEAT_STALE_MIN (default 5) minutes old its wait leaves the queue and a lane it
#                               holds is cleared, whatever PID the lane names (10-10 01:08Z: an orphaned remote wait held
#                               lane 2 for 12 min, its owner PID the long-lived ControlMaster sshd, so kill -0 passed).
#                               A name that never beats keeps the PID rules below: beating is opt-in.
#   gate_queue.sh install       copies this script to /ai/tmp/gate-queue.sh when it differs (temp file + mv, so a
#                               waiter's bash keeps reading the old file); deploy_titan_node.sh runs it after a deploy
#   A name starting "solo-" (a measurement) must be listed in the queue, and runs alone: it waits for both lanes, and
#   nobody runs beside it.
#   No one takes a lane inside an eval window or LEAD_MIN before one (scripts/eval_windows.conf, #1518), while an eval
#   unit runs, or while a model-eval deploy hold is unexpired.
#   gate_queue.sh window [lead_min]  exits 0 and says which when now is inside a window (deploy.sh and ab_run.sh ask)
# A name not in the queue waits until no listed name is waiting, then takes a lane.
# A listed name with no live waiter and no lane for GATE_QUEUE_STALE_MIN minutes (default 60) leaves the queue, with a
# line saying so (10-09: solo-coord-deploy lines lingered after their deploys). The clock is gate.seen/<name>, touched
# while the name waits or holds, started when it's first seen idle; every wait loop, show and status run the check.
# Same verbs and behaviour as the copy at /ai/tmp/gate-queue.sh; installing this one there is Coordination's step.
# Tests point GATE_QUEUE_DIR at a temp dir and pin GATE_QUEUE_HHMM / GATE_QUEUE_EVAL_UNITS (test_gate_queue.py).
D=${GATE_QUEUE_DIR:-/ai/tmp}
GONE=$D/gate.gone; Q=$D/gate.queue; W=$D/gate.wait; L1=$D/gate.lock; L2=$D/gate.lock.2; HOLD=$D/deploy.hold; SEEN=$D/gate.seen
STALE_MIN=${GATE_QUEUE_STALE_MIN:-60}
BEAT=$D/gate.beat; BEAT_STALE_MIN=${GATE_QUEUE_BEAT_STALE_MIN:-5}
EVAL_UNITS=${GATE_QUEUE_EVAL_UNITS-titan-model-eval titan-household-eval}
NAP=${GATE_QUEUE_SLEEP:-15}
mkdir -p "$W" 2>/dev/null
# waiting: a fresh marker AND a live `gate…queue.sh wait <name>` behind it (10-07 00:29Z: a runner's own touch loop
# kept a marker fresh with no waiter, and everyone queued behind it for 20 min). Only this script's wait loop counts.
waiting() { [ -f "$W/$1" ] && [ $(( $(date +%s) - $(stat -c %Y "$W/$1") )) -lt 60 ] \
  && pgrep -f "gate[-_]queue[.]sh wait $1\$" >/dev/null 2>&1; }
# The eval windows (no gates) come from eval_windows.conf, beside this script in the repo, or the deploy checkout's
# copy when this runs as /ai/tmp/gate-queue.sh (#1518). Without either, the built-in pair below, in UTC and wide enough for the
# model eval on either side of the clock change (test_eval_windows.py keeps it covering the file).
WINDOWS_CONF=${GATE_QUEUE_WINDOWS:-}
[ -n "$WINDOWS_CONF" ] || for c in "$(dirname "$0")/eval_windows.conf" /ai/projects/titan-node/scripts/eval_windows.conf; do
  [ -r "$c" ] && { WINDOWS_CONF=$c; break; }; done
# A line's optional 4th field is a zone; eval_windows.py (beside the conf) turns every line into UTC for today (#1566).
# GATE_QUEUE_DATE (YYYY-MM-DD) pins the day for tests. Without python, zoned lines are read as given (their zone dropped).
windows() { if [ -r "$WINDOWS_CONF" ]; then
    python3 "$(dirname "$WINDOWS_CONF")/eval_windows.py" --conf "$WINDOWS_CONF" ${GATE_QUEUE_DATE:+--date "$GATE_QUEUE_DATE"} 2>/dev/null \
      || grep -E '^[0-9]{4} [0-9]{4}( |$)' "$WINDOWS_CONF" | awk '{print $1, $2, $3}'
  else printf '%s\n' "0325 0350 nightly" "0835 1100 model-eval"; fi; }
lead_min() { local l=""; [ -r "$WINDOWS_CONF" ] && l=$(awk '$1=="LEAD_MIN"{print $2}' "$WINDOWS_CONF"); echo "${l:-12}"; }
hhmm_min() { echo $(( 10#${1:0:2} * 60 + 10#${1:2:2} )); }
# in_clock_window [lead]: now (or GATE_QUEUE_HHMM) is inside a window or `lead` minutes before one; WIN names it
in_clock_window() { local t L a b n s e; t=$(hhmm_min "${GATE_QUEUE_HHMM:-$(date -u +%H%M)}"); L=${1:-$(lead_min)}
  while read -r a b n; do s=$(( $(hhmm_min "$a") - L )); e=$(hhmm_min "$b")
    if [ "$s" -lt 0 ]; then { [ "$t" -ge $(( s + 1440 )) ] || [ "$t" -lt "$e" ]; } && { WIN="$a–${b}Z ${n:-}"; return 0; }
    else [ "$t" -ge "$s" ] && [ "$t" -lt "$e" ] && { WIN="$a–${b}Z ${n:-}"; return 0; }; fi
  done < <(windows); return 1; }
in_window() {
  in_clock_window && return 0
  # …and while an eval unit is still running, even past its window (10-08: the nightly household-eval overran 03:50Z)
  for u in $EVAL_UNITS; do
    case "$(systemctl show -p ActiveState --value $u.service 2>/dev/null)" in active|activating|deactivating) return 0;; esac
  done
  # …and while a model-eval deploy hold is still running (#408's passes can run past 10:00Z)
  [ -f "$HOLD" ] && grep -q "model-eval" "$HOLD" && [ "$(cut -d' ' -f1 "$HOLD")" -gt "$(date +%s)" ]; }
# drop a name from the queue file; two lanes can finish at once, so the edit is serialized (flock) and uses its own temp
qdrop() { [ -f "$Q" ] || return 0; ( flock 9; t=$(mktemp "$Q.XXXXXX"); grep -vx "$1" "$Q" > "$t"; cat "$t" > "$Q"; rm -f "$t" ) 9>"$Q.lock"; }
holds() { [ "$(cut -d' ' -f1 "$L1/owner" 2>/dev/null)" = "$1" ] || [ "$(cut -d' ' -f1 "$L2/owner" 2>/dev/null)" = "$1" ]; }
# listed names idle (no waiter, no lane) for STALE_MIN minutes leave the queue
prune() { [ -f "$Q" ] || return 0; mkdir -p "$SEEN" 2>/dev/null; local now n; now=$(date +%s)
  for n in $(cat "$Q" 2>/dev/null); do
    case "$n" in */*|.*) continue;; esac
    if waiting "$n" || holds "$n" || [ ! -f "$SEEN/$n" ]; then touch "$SEEN/$n"
    elif [ $(( now - $(stat -c %Y "$SEEN/$n") )) -ge $(( STALE_MIN * 60 )) ]; then
      qdrop "$n"; rm -f "$SEEN/$n"; echo "$n dropped: no waiter or lane for ${STALE_MIN} min"
    fi
  done; }
# the heartbeat clock: GATE_QUEUE_NOW pins it (tests); a name with no beat file is never stale (it doesn't beat)
now_s() { echo "${GATE_QUEUE_NOW:-$(date +%s)}"; }
beat_stale() { [ -f "$BEAT/$1" ] && [ $(( $(now_s) - $(stat -c %Y "$BEAT/$1") )) -ge $(( BEAT_STALE_MIN * 60 )) ]; }
# a tombstone for a finished name (old ones pruned after a day: a name reused tomorrow starts clean anyway)
tomb() { mkdir -p "$GONE" 2>/dev/null; touch "$GONE/$1"; find "$GONE" -type f -mmin +1440 -delete 2>/dev/null; }
first_waiting() { while read -r n; do [ -n "$n" ] && waiting "$n" && { echo "$n"; return; }; done < "$Q" 2>/dev/null; }
case "$1" in
  wait)
    [ -n "$2" ] || { echo "usage: gate_queue.sh wait <name>" >&2; exit 2; }
    # Two lanes (10-07: titan has 12 cores and a gate uses ~1; each gate has its own TEST_STATE and ephemeral ports):
    # the first two waiting names each take a free lane, gate.lock or gate.lock.2.
    started=$(date +%s)   # the real clock: tombstones carry real mtimes (GATE_QUEUE_NOW pins only the beat clock)
    while :; do
      # done/drop ran for this name after this wait began: its owner is finished with it, so never take a lane
      if [ -f "$GONE/$2" ] && [ "$(stat -c %Y "$GONE/$2")" -ge "$started" ]; then
        rm -f "$W/$2"; echo "$2: done or dropped while it waited; left the queue at $(date -u +%H:%MZ)"; exit 3
      fi
      # the caller went away (a killed runner; an ssh that dropped): leave the queue rather than take a lane for no one
      if [ "$(ps -o ppid= -p $$ | tr -d ' ')" != "$PPID" ] || ! kill -0 "$PPID" 2>/dev/null; then
        rm -f "$W/$2"; echo "$2: its caller is gone; left the queue at $(date -u +%H:%MZ)"; exit 3
      fi
      # the caller beats and has stopped (a runner killed after its wait reached titan): leave the queue
      if beat_stale "$2"; then
        rm -f "$W/$2" "$BEAT/$2"; echo "$2: its caller's heartbeat stopped; left the queue at $(date -u +%H:%MZ)"; exit 3
      fi
      touch "$W/$2"
      prune
      # a lane left behind with no owner (an older helper's leftover), no gate running and 5+ min old: clear it
      for L in $L1 $L2; do
        # a holder whose caller beats and has stopped (#1407), whatever PID it names and whether or not a gate runs
        on=$(cut -d' ' -f1 "$L/owner" 2>/dev/null)
        if [ -n "$on" ] && beat_stale "$on"; then
          # (its stale beat file stays, harmless: a runner beats before it waits again, and done/drop remove it)
          rm -f "$L/owner"; rmdir "$L" 2>/dev/null && echo "cleared $L: $on's caller stopped its heartbeat"
        fi
        # an owner whose caller died (an orphaned wait took the lane with no one left to run done), no gate running
        op=$(cut -s -d' ' -f2 "$L/owner" 2>/dev/null)
        if [ -n "$op" ] && ! kill -0 "$op" 2>/dev/null && [ "$(pgrep -fc '[r]un_deploy_gate')" = 0 ]; then
          rm -f "$L/owner"; rmdir "$L" 2>/dev/null && echo "cleared $L: its holder's process is gone"
        fi
        if [ -d "$L" ] && [ ! -f "$L/owner" ] && [ $(( $(date +%s) - $(stat -c %Y "$L") )) -gt 300 ] \
           && [ "$(pgrep -fc '[r]un_deploy_gate')" = 0 ]; then rmdir "$L" 2>/dev/null && echo "cleared a stale $L"; fi
      done
      rank=$(i=0; while read -r n; do [ -n "$n" ] && waiting "$n" && { i=$((i+1)); [ "$n" = "$2" ] && { echo $i; break; }; }; done < "$Q" 2>/dev/null)
      [ -z "$rank" ] && ! grep -qx "$2" "$Q" 2>/dev/null && [ -z "$(first_waiting)" ] && rank=1
      # a "solo-" name (a measurement) takes both lanes, first in line and with both free; nobody joins a solo
      case "$(cat $L1/owner $L2/owner 2>/dev/null)" in *solo-*) sleep "$NAP"; continue;; esac
      if [ "${2#solo-}" != "$2" ]; then
        # a solo must be listed (Coordination orders the measurements); an unlisted one waits until it is
        grep -qx "$2" "$Q" 2>/dev/null || { sleep "$NAP"; continue; }
        # solos keep their listed order strictly: an earlier-listed solo goes first even if it isn't waiting yet
        ahead=$(awk -v me="$2" '$0==me{exit} /^solo-/{print}' "$Q" 2>/dev/null)
        [ -n "$ahead" ] && { sleep "$NAP"; continue; }
        if ! in_window && [ "$rank" = 1 ] && mkdir $L1 2>/dev/null; then
          if mkdir $L2 2>/dev/null; then
            echo "$2 $PPID" > $L1/owner; echo "$2 $PPID" > $L2/owner; rm -f "$W/$2"
            echo "$2 holds both lanes at $(date -u +%H:%MZ)"; exit 0
          fi
          rmdir $L1
        fi
        sleep "$NAP"; continue
      fi
      fw=$(first_waiting); [ "${fw#solo-}" != "$fw" ] && { sleep "$NAP"; continue; }   # a solo first in line: wait for it
      if ! in_window && [ -n "$rank" ] && [ "$rank" -le 2 ]; then
        for L in $L1 $L2; do
          if mkdir "$L" 2>/dev/null; then rm -f "$W/$2"; echo "$2 $PPID" > "$L/owner"; echo "$2 holds $L at $(date -u +%H:%MZ)"; exit 0; fi
        done
      fi
      sleep "$NAP"
    done ;;
  done)
    [ -n "$2" ] || { echo "usage: gate_queue.sh done <name>" >&2; exit 2; }
    tomb "$2"; rm -f "$W/$2" "$SEEN/$2" "$BEAT/$2"; released=
    for L in $L1 $L2; do
      o=$(cut -d' ' -f1 "$L/owner" 2>/dev/null)
      # the holder, or a lane with no owner recorded (taken by an older copy of this helper, lane 1 only)
      if [ "$o" = "$2" ] || { [ "$L" = $L1 ] && [ -d "$L" ] && [ -z "$o" ] && [ -z "$released" ]; }; then
        rm -f "$L/owner"; rmdir "$L" 2>/dev/null; released="$released $L"
        [ "${2#solo-}" = "$2" ] && break   # a solo holds both: release both
      fi
    done
    if [ -n "$released" ]; then
      qdrop "$2"
      echo "$2 released $released at $(date -u +%H:%MZ)"
    else
      echo "$2 doesn't hold a lane: nothing released, its place in the queue kept"
    fi ;;
  window)   # #1518: inside an eval window (or its lead) now? exit 0 and name it; `window 0` ignores the lead
    in_clock_window "${2:-}" && { echo "inside the eval window $WIN"; exit 0; }; exit 1;;
  drop)   # take a name out of the queue (never touches a lane)
    [ -n "$2" ] || { echo "usage: gate_queue.sh drop <name>" >&2; exit 2; }
    tomb "$2"; rm -f "$W/$2" "$SEEN/$2" "$BEAT/$2"; qdrop "$2"; echo "$2 dropped" ;;
  beat)   # the caller is alive (#1407); quiet, so a runner can call it every minute
    [ -n "$2" ] || { echo "usage: gate_queue.sh beat <name>" >&2; exit 2; }
    mkdir -p "$BEAT" 2>/dev/null; touch "$BEAT/$2" ;;
  show) prune; lane() { [ -d "$1" ] || { echo -; return; }; o=$(cut -d' ' -f1 "$1/owner" 2>/dev/null); echo "${o:-HELD (no owner; since $(date -u -r "$1" +%H:%MZ))}"; }
        echo "lanes: $(lane $L1) | $(lane $L2)"
        while read -r n; do [ -n "$n" ] && { waiting "$n" && echo "* $n" || echo "  $n"; }; done < "$Q" 2>/dev/null ;;
  install)   # bash reads a script as it runs: a new file moved into place, never the running one written over
    me="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"; T=${GATE_QUEUE_TARGET:-$D/gate-queue.sh}
    if [ "$me" = "$T" ] || cmp -s "$me" "$T"; then echo "gate queue current"; exit 0; fi
    t=$(mktemp "$T.XXXXXX") && cp "$me" "$t" && chmod 755 "$t" && mv -f "$t" "$T" \
      && echo "gate queue installed at $T" || { rm -f "$t"; echo "gate queue: install failed" >&2; exit 1; } ;;
  status) prune; now=$(date +%s)
    mins() { echo $(( ($now - $1) / 60 ))m; }
    lane() { [ -d "$1" ] || { echo -; return; }; o=$(cut -d' ' -f1 "$1/owner" 2>/dev/null)
      b=$( [ -n "$o" ] && [ -f "$BEAT/$o" ] && echo ", beat $(mins "$(stat -c %Y "$BEAT/$o")") ago")
      echo "${o:-HELD (no owner)} ($(mins "$(stat -c %Y "$1")")$b)"; }
    echo "lanes: $(lane $L1) | $(lane $L2)"
    w=""
    for n in $( (cat "$Q" 2>/dev/null; for f in "$W"/*; do [ -e "$f" ] && basename "$f"; done) | awk 'NF && !s[$0]++'); do
      waiting "$n" || continue
      pid=$(pgrep -f "gate[-_]queue[.]sh wait $n\$" | head -1)
      e=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
      w="$w${w:+, }$n (${e:+$((e / 60))m}${e:-?})"
    done
    echo "waiting: ${w:--}"
    l=""
    for n in $(cat "$Q" 2>/dev/null); do
      if holds "$n"; then t="holding"; elif waiting "$n"; then t="waiting"
      else t="idle $( [ -f "$SEEN/$n" ] && mins "$(stat -c %Y "$SEEN/$n")" || echo 0m)"; fi
      l="$l${l:+, }$n ($t)"
    done
    echo "listed: ${l:--}" ;;
  *) echo "usage: gate_queue.sh wait|done|drop|beat <name> | show | status | install" >&2; exit 2 ;;
esac
