#!/usr/bin/env bash
# titan-clock-canary (#1515, #1570): the hermetic gate, once a day, with the wall clock moved. A test that passes today
# and fails at +8 is a clock time bomb: it would turn main red by itself within a week (10-10: test_consolidate #1512;
# test_miss_proposals would have gone red 10-14). This finds them a week early, while nothing is on fire. The variants
# find the bombs that whole days miss: the hour a clock change repeats or skips, and the household's evening.
#
#   canary.sh run [all|LABEL...]
#                       run every hermetic gate test (the plan scripts/run_deploy_gate.sh builds: the deploy script's
#                       listed tests, then every other `# gate: hermetic` test not on gate_live_tests.txt) once per
#                       clock plan through scripts/clockshift/run_one.sh. The plans (label: clock):
#                         plus8        +8 days
#                         dst-repeat   2026-11-01T08:30:00Z  (01:30 PDT, a pass through the repeated 01:00 hour)
#                         dst-skip     2027-03-14T10:30:00Z  (03:30 PDT, just after the skipped 02:00-03:00)
#                         evening      2027-06-15T06:30:00Z  (23:30 PDT on 06-14: UTC date and household date differ)
#                       No argument: plus8 and one variant, the variant rotating by day of year (each variant every 3rd
#                       night); CLOCK_CANARY_PLANS="plus8 evening" pins the list; `all` runs all four. A plan takes about
#                       a gate's worth of tests at CLOCK_CANARY_JOBS (the deploy gate does 450 tests in about 330 s at 4 jobs,
#                       so a plan is roughly 10-15 min at 2 jobs): two plans a night is ~25 min, all four ~45-60 min. All
#                       plans share one CLOCK_CANARY_BUDGET (3300 s); a plan that cannot start inside it is skipped, not cut.
#                       Each plan appends
#                         /ai/logs/clock-canary.jsonl   {ts, label, shift, to, secs, total, ok, failed, fail: [test names]}
#                       (shift is the days, or null for an absolute target; to is the ISO time or null) and alerts on a
#                       change of that plan's failing set only (titan_alert.EdgeAlert: new set, changed, cleared).
#                       Test names only: never output, environment or arguments.
#   canary.sh install   the deploy's step: install server-setup/titan-clock-canary.{service,timer} when they differ,
#                       daemon-reload, enable --now the timer (never runs the service itself)
#
# The plan is scripts/gate_plan.sh's, the one run_deploy_gate.sh runs. CLOCK_CANARY_* variables are for the tests.
set -u
REPO=${CLOCK_CANARY_REPO:-/ai/projects/titan-node}
RUN_ONE=${CLOCK_CANARY_RUN_ONE:-$REPO/scripts/clockshift/run_one.sh}
BUDGET=${CLOCK_CANARY_BUDGET:-3300}
JOBS=${CLOCK_CANARY_JOBS:-2}
LOG=${CLOCK_CANARY_LOG:-/ai/logs/clock-canary.jsonl}
GQ=${CLOCK_CANARY_QUEUE:-/ai/tmp/gate-queue.sh}
[[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || JOBS=2

alert() {   # alert "<space-separated failing names>" TEXT LABEL: edge-triggered per plan; an empty list means clear
  if [ -n "${CLOCK_CANARY_ALERT:-}" ]; then "$CLOCK_CANARY_ALERT" "$1" "$2" "$3"; return; fi
  CC_NAMES="$1" CC_TEXT="$2" CC_LABEL="$3" python3 - "$REPO/scripts" <<'PY' || true
import os, sys
sys.path.insert(0, sys.argv[1])
from titan_alert import EdgeAlert
names = os.environ["CC_NAMES"].split()
label = os.environ["CC_LABEL"]
suffix = "" if label == "plus8" else "-" + label   # plus8 keeps its old alert name
EdgeAlert("clock-canary" + suffix, "Titan clock canary (%s)" % label).update({n: "fails at %s" % label for n in names})
PY
}

plan() {   # the test file names, one per line: the deploy gate's own plan (scripts/gate_plan.sh, #1559)
  ( . "$(dirname "$0")/../gate_plan.sh" && gate_plan "$REPO" ) | cut -d'|' -f1
}

case "${1:-}" in
  run)
    shift   # the plan labels, if any
    if [ -x "$GQ" ] && w=$("$GQ" window 2>/dev/null); then echo "clock canary: $w (skipped)"; exit 0; fi
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/clock-canary.XXXXXX") || exit 1
    trap 'rm -rf "$tmp"' EXIT
    if [ "${1:-}" = all ]; then set -- plus8 dst-repeat dst-skip evening
    elif [ $# = 0 ] && [ -n "${CLOCK_CANARY_PLANS:-}" ]; then set -- $CLOCK_CANARY_PLANS
    elif [ $# = 0 ]; then
      vars=(dst-repeat dst-skip evening)
      day=${CLOCK_CANARY_DAY:-$(date -u +%j)}
      set -- plus8 "${vars[$(( 10#$day % 3 ))]}"
    fi
    plan | sed 's|.*/||' | sort -u > "$tmp/plan"
    total=$(wc -l < "$tmp/plan" | tr -d ' ')
    [ "$total" -gt 0 ] || { echo "clock canary: empty plan (skipped)"; exit 1; }
    for label in "$@"; do
      case "$label" in
        plus8)      clock=8;                    desc="+8 days";                    shiftjson=8;    tojson=null ;;
        dst-repeat) clock=2026-11-01T08:30:00Z; desc="2026-11-01T08:30:00Z";       shiftjson=null; tojson='"2026-11-01T08:30:00Z"' ;;
        dst-skip)   clock=2027-03-14T10:30:00Z; desc="2027-03-14T10:30:00Z";       shiftjson=null; tojson='"2027-03-14T10:30:00Z"' ;;
        evening)    clock=2027-06-15T06:30:00Z; desc="2027-06-15T06:30:00Z";       shiftjson=null; tojson='"2027-06-15T06:30:00Z"' ;;
        *) echo "clock canary: unknown plan $label" >&2; exit 2 ;;
      esac
      left=$(( BUDGET - SECONDS ))
      if [ "$left" -lt 120 ]; then echo "clock canary: $label skipped (the time budget is spent)"; continue; fi
      to=(); command -v timeout >/dev/null && to=(timeout "$left")
      t0=$SECONDS
      # Nice/idle IO come from the unit; each run_one.sh also nices its test.
      ${to[@]+"${to[@]}"} xargs -P "$JOBS" -I{} bash "$RUN_ONE" {} "$clock" "$REPO" < "$tmp/plan" > "$tmp/out.$label" 2>/dev/null
      grep -oE '^PASS [A-Za-z0-9_]+\.py' "$tmp/out.$label" | awk '{print $2}' | sort -u > "$tmp/pass"
      comm -23 "$tmp/plan" "$tmp/pass" > "$tmp/fail"   # no PASS line (failed, crashed or timed out) is a failure
      ok=$(wc -l < "$tmp/pass" | tr -d ' '); nfail=$(wc -l < "$tmp/fail" | tr -d ' ')
      names=$(tr '\n' ' ' < "$tmp/fail" | sed 's/ $//')
      json=$(awk 'BEGIN{printf "["} NR>1{printf ", "} {printf "\"%s\"", $0} END{printf "]"}' "$tmp/fail")
      mkdir -p "$(dirname "$LOG")" 2>/dev/null
      printf '{"ts": "%s", "label": "%s", "shift": %s, "to": %s, "secs": %d, "total": %d, "ok": %d, "failed": %d, "fail": %s}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$label" "$shiftjson" "$tojson" "$(( SECONDS - t0 ))" "$total" "$ok" "$nfail" "$json" >> "$LOG" 2>/dev/null
      if [ "$nfail" = 0 ]; then
        alert "" "" "$label"
        echo "clock canary: $ok/$total pass at $desc"
      else
        alert "$names" "These hermetic tests fail with the clock at $desc ($label): $names. Each would turn main red on its own once that time comes." "$label"
        echo "clock canary: $nfail of $total fail at $desc: $names"
      fi
    done ;;
  install)
    SYSTEMCTL=${SYSTEMCTL:-systemctl}; SUDO=${SUDO-sudo -n}; DIR=${SYSTEMD_DIR:-/etc/systemd/system}
    SRC="$(cd "$(dirname "$0")/../.." && pwd)/server-setup"; changed=0
    for u in titan-clock-canary.service titan-clock-canary.timer; do
      [ -f "$SRC/$u" ] || { echo "clock canary unit: $SRC/$u missing" >&2; exit 1; }
      cmp -s "$SRC/$u" "$DIR/$u" && continue
      $SUDO install -m 0644 "$SRC/$u" "$DIR/$u" || { echo "clock canary unit: couldn't install $u" >&2; exit 1; }
      changed=1
    done
    [ "$changed" = 1 ] && { $SUDO $SYSTEMCTL daemon-reload || exit 1; }
    if ! $SYSTEMCTL is-enabled --quiet titan-clock-canary.timer 2>/dev/null || \
       ! $SYSTEMCTL is-active --quiet titan-clock-canary.timer 2>/dev/null; then
      $SUDO $SYSTEMCTL enable --now titan-clock-canary.timer || { echo "clock canary unit: couldn't enable the timer" >&2; exit 1; }
      changed=1
    fi
    [ "$changed" = 1 ] && echo "clock canary timer installed" || echo "clock canary timer current" ;;
  *) echo "usage: $0 run|install" >&2; exit 2 ;;
esac
