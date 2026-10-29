#!/usr/bin/env bash
# Run the deploy gate's tests locally, exactly as scripts/deploy_titan_node.sh judges them, before opening a PR.
#
#   bash scripts/run_deploy_gate.sh [checkout]      (default: this checkout)
#   GATE_JOBS=1 bash scripts/run_deploy_gate.sh     (one test at a time, the sequential run; the default is OPS_GATE_JOBS)
#
# Every "run_test scripts/test_*.py … || rollback" line of the deploy script runs with the same throwaway
# stores the deploy exports (never titan's real ones). A line that checks for " 0 failed" in the output is
# judged that way; the others by exit code. Prints "ok <file>" or "FAIL <file>" with the failing lines, and
# exits 1 when any failed. Hermetic tests only: nothing here touches the live agent (see the live list in
# titan-node#350, and never sweep every test_*.py by hand: test_household_agent.py drives the real agent).
#
# GATE_JOBS > 1 (titan's gates take ~8 min one test at a time): each test gets its own throwaway store directory
# (the same exports, a fresh TEST_STATE), so two tests never share a store; the verdict lines print in the same
# order as a sequential run. A test listed in scripts/gate_serial_tests.txt runs alone, after the others.
set -uo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/ops_defaults.sh"   # OPS_GATE_JOBS: the one default (#1518)
R="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$R" || exit 2
JOBS="${GATE_JOBS:-$OPS_GATE_JOBS}"
[[ "$JOBS" =~ ^[1-9][0-9]?$ ]] || JOBS=1
# One temp root per gate run (10-10: titan's /tmp held 53 GB of test leftovers, +4.8 GB/day). Every test gets
# TMPDIR through gate_env.sh's allowlist, so each mkdtemp lands in here, and the whole run's temp goes when it ends.
# It stays under the system temp dir: tests treat paths under /ai as live data, and /ai/tmp's setgid bit would change
# the modes tests assert (both failed the first gate of this, 10-10). Per-run removal keeps / from filling.
GATE_TMP_ROOT=${GATE_TMP_ROOT:-${TMPDIR:-/tmp}/titan-gate-tmp}
mkdir -p "$GATE_TMP_ROOT" 2>/dev/null
find "$GATE_TMP_ROOT" -mindepth 1 -maxdepth 1 -name 'run.*' -mmin +1440 -exec rm -rf {} + 2>/dev/null   # a killed run's
RUN_TMP="$(mktemp -d "$GATE_TMP_ROOT/run.XXXXXX")"
export TMPDIR="$RUN_TMP"
TEST_STATE="$(mktemp -d)"
RES="$(mktemp -d)"
trap 'rm -rf "$RUN_TMP"' EXIT
EXPORTS="$(sed -n '/^export DAILY_LOG_DIR=/,/[^\\]$/p' scripts/deploy_titan_node.sh)"
# The deploy's own exports (a multi-line `export … \` block), so the stores are the same throwaway ones.
eval "$EXPORTS"
# The same allowlisted environment the deploy's tests get (gate_env.sh, #996), not this shell's.
. scripts/gate_env.sh
gate_env_build

# The plan: one "<file>|<pass pattern or empty>|<listed|found>" line per test, in the sequential run's order
# (scripts/gate_plan.sh, shared with the clock canary, #1559).
. scripts/gate_plan.sh
PLAN="$RES/plan"
gate_plan . > "$PLAN"

keep_fail_log() {   # GATE_KEEP_FAIL=1 (the deploy, #1473): a failing test's last 60 lines, scrubbed, 0600, as the
  # sequential deploy's keep_fail writes them (deploy_fail_log.py), and the path printed. Never the environment.
  [ -n "${GATE_KEEP_FAIL:-}" ] || return 0
  local name log
  name=$(basename "$1")
  log="${GATE_FAIL_LOG_DIR:-/ai/logs}/deploy-fail-$(date -u +%Y%m%d-%H%M%S)-${name%.py}.log"
  printf '%s\n' "$2" | python3 "$R/scripts/deploy_fail_log.py" --out "$log" && echo "!!! $name: its output is in $log"
}

now() {   # seconds with a fraction: bash 5's clock, else perl's, else whole seconds (the Mac's bash 3.2 has neither builtin)
  if [ -n "${EPOCHREALTIME:-}" ]; then echo "${EPOCHREALTIME/,/.}"
  else perl -MTime::HiRes=time -e 'printf "%.3f\n", time' 2>/dev/null || date +%s; fi
}

judge() {   # judge <n> <file> <pattern> <listed|found>: run one test and write its verdict lines to $RES/<n>
  local n=$1 f=$2 pat=$3 how=$4 out rc ok t0 tag=""
  [ "$how" = found ] && tag=" (found)"
  printf '%s\n' "$f" > "$RES/$n.f"
  t0=$(now)
  out=$(gate_python "$f" 2>&1); rc=$?
  printf '%s\n' "$(awk -v a="$t0" -v b="$(now)" 'BEGIN{printf "%.2f", b - a}')" > "$RES/$n.t"   # #1514: this test's seconds
  if [ "$how" = listed ]; then
    if [ -n "$pat" ]; then grep -q -- "$pat" <<<"$out" && ok=1 || ok=0; else [ $rc -eq 0 ] && ok=1 || ok=0; fi
  else
    [ $rc -eq 0 ] && ! grep -qE "(^|[^0-9])[1-9][0-9]* failed" <<<"$out" && ok=1 || ok=0
  fi
  if [ $ok -eq 1 ]; then echo "ok   $f$tag"; else echo "FAIL $f$tag"; grep -E 'FAIL|Error|Traceback' <<<"$out" | head -6
    keep_fail_log "$f" "$out"; fi > "$RES/$n"
}

judge_alone() {   # judge in a subshell with its own throwaway stores (GATE_JOBS > 1)
  ( TEST_STATE="$(mktemp -d)"; trap 'rm -rf "$TEST_STATE"' EXIT
    eval "$EXPORTS"; gate_env_build; judge "$@" )
}

# --- #1514: affected tests first. GATE_FIRST=<file of test names, one per line> (a PR gate: gate_run.sh computes it from the
# diff with scripts/affected_tests.py). Those plan tests run first and the gate stops at the first failure among them;
# when they pass, the rest of the plan runs. Every plan test runs exactly once either way, so the verdict and the count
# are the full gate's. Names the plan doesn't hold are ignored: this never adds a test (nothing live or quarantined).
# Unset (the deploy): one phase, the plan as it is.
reorder_plan() {   # reorder_plan <plan> <first-names-file>: writes "$PLAN.first" and "$PLAN.rest", the plan's order kept in each
  awk -F'|' 'NR == FNR { sub(/^.*\//, "", $1); if ($1 != "") want[$1] = 1; next }
             { f = $1; sub(/^.*\//, "", f); if (f in want) print > first; else print > rest }' first="$1.first" rest="$1.rest" \
    "$2" "$1"
  : >> "$1.first"; : >> "$1.rest"
}

n=0
run_phase() {   # run_phase <plan file> [stop]: judge its tests (JOBS at a time), print their verdicts in plan order; 1 when one FAILed.
  # "stop": one at a time, the first FAIL ends the phase (at several at a time, the tests already started finish).
  local start=$n f pat how row k serial later
  if [ "$JOBS" -le 1 ]; then
    while IFS='|' read -r f pat how; do n=$((n + 1)); judge "$n" "$f" "$pat" "$how"; cat "$RES/$n"
      [ -n "${2:-}" ] && grep -q '^FAIL' "$RES/$n" && break; done < "$1"
  else
    serial=$(grep -v '^#' scripts/gate_serial_tests.txt 2>/dev/null | awk '{print $1}')
    later=()
    while IFS='|' read -r f pat how; do
      n=$((n + 1))
      if grep -qx "$(basename "$f")" <<<"$serial"; then later+=("$n|$f|$pat|$how"); continue; fi
      while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do wait -n 2>/dev/null || sleep 0.2; done
      judge_alone "$n" "$f" "$pat" "$how" &
    done < "$1"
    wait
    for row in "${later[@]+"${later[@]}"}"; do IFS='|' read -r k f pat how <<<"$row"; judge_alone "$k" "$f" "$pat" "$how"; done
    for k in $(seq $((start + 1)) "$n"); do cat "$RES/$k" 2>/dev/null || echo "FAIL (no verdict for test #$k)"; done
  fi
  for k in $(seq $((start + 1)) "$n"); do grep -q '^FAIL' "$RES/$k" 2>/dev/null && return 1; done
  return 0
}

report_times() {   # #1514: one "time <seconds> <file>" line per test that ran, then the 10 slowest
  local k f
  for k in $(seq 1 "$n"); do
    [ -f "$RES/$k.t" ] || continue
    f=$(sed -n '1p' "$RES/$k.f" 2>/dev/null)
    printf 'time %s %s\n' "$(cat "$RES/$k.t")" "$f"
  done > "$RES/times"
  cat "$RES/times"
  echo "slowest 10 (seconds):"
  sort -k2,2 -rn "$RES/times" | head -10 | awk '{printf "  %8s  %s\n", $2, $3}'
}

rc=0
if [ -n "${GATE_FIRST:-}" ] && [ -s "$GATE_FIRST" ]; then
  reorder_plan "$PLAN" "$GATE_FIRST"
  echo "affected first: $(wc -l < "$PLAN.first" | tr -d ' ') of $(wc -l < "$PLAN" | tr -d ' ') tests"
  if run_phase "$PLAN.first" stop; then
    run_phase "$PLAN.rest" || rc=1
  else
    rc=1; echo "affected tests failed: stopped before the rest of the plan"
  fi
else
  run_phase "$PLAN" || rc=1
fi
report_times
exit $rc
