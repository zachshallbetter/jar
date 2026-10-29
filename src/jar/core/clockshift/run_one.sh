#!/usr/bin/env bash
# scripts/clockshift/run_one.sh <test_file> <shift_days|@epoch|ISO time> [repo]: run one hermetic gate test in the deploy gate's
# environment with the wall clock shifted (#1515). Prints PASS/FAIL and the "N failed" line. TZ=UTC like titan.
# A test that passes unshifted but fails at +8 is a clock time bomb: it will turn main red on its own within a week.
set -u
here=$(cd "$(dirname "$0")" && pwd)
t=$(basename "$1"); sh=$2; repo=${3:-$(cd "$here/../.." && pwd)}
cd "$repo" || exit 2
TEST_STATE="$(mktemp -d "${TMPDIR:-/tmp}/clockshift.XXXXXX")"; export TEST_STATE
EXPORTS="$(sed -n '/^export DAILY_LOG_DIR=/,/[^\\]$/p' scripts/deploy_titan_node.sh)"
eval "$EXPORTS"
. scripts/gate_env.sh
export TZ=UTC
gate_env_build
# <shift> is days ahead (8, -1, 0.5), @<epoch>, or an absolute ISO time (2026-11-01T08:30:00Z).
if [[ "$sh" == *T* ]]; then SHIFT_VAR=TIME_SHIFT_TO; else SHIFT_VAR=TIME_SHIFT_DAYS; fi
out=$(env -i "${GATE_ENV[@]}" TZ=UTC PYTHONPATH="$here" "$SHIFT_VAR=$sh" nice -n 15 python3 "scripts/$t" 2>&1); rc=$?
nf=$(grep -oE "(^|[^0-9])[1-9][0-9]* failed" <<<"$out" | head -1)
if [ $rc -eq 0 ] && [ -z "$nf" ]; then echo "PASS $t @$sh"; else echo "FAIL $t @$sh rc=$rc $nf"; fi
rm -rf "$TEST_STATE"
[ $rc -eq 0 ] && [ -z "$nf" ]
