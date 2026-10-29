#!/usr/bin/env bash
# The deploy gate's test plan, in one place (#1559): scripts/run_deploy_gate.sh runs it and scripts/clockshift/canary.sh
# re-runs the same tests with the clock shifted. Sourced, never run.
#
#   gate_plan [checkout]    one "<file>|<pass pattern or empty>|<listed|found>" line per test, in the sequential run's
#                           order: first every "run_test scripts/test_*.py … || rollback" line of the deploy script
#                           (sorted, unique), then every other `# gate: hermetic` test that is not on
#                           scripts/gate_live_tests.txt (the deploy's discovery pass, #350). Files are "scripts/test_*.py",
#                           relative to the checkout (default: the current directory). The separator is "|", never a tab:
#                           read collapses runs of IFS whitespace, so an empty pattern between tabs would shift.
gate_plan() (
  cd "${1:-.}" || exit 2
  local line f b pat
  while IFS= read -r line; do
    f=$(grep -oE 'scripts/test_[a-z0-9_]+\.py' <<<"$line" || true); f=${f%%$'\n'*}   # captured first: head -1 on the pipe can SIGPIPE grep under pipefail
    [ -n "$f" ] || continue
    pat=$(sed -nE 's/^run_test scripts\/test_[a-z0-9_]+\.py "([^"]*)".*/\1/p' <<<"$line")   # its pass pattern, if any
    printf '%s|%s|listed\n' "$f" "$pat"
  done < <(grep -E '^run_test scripts/test_[a-z0-9_]+\.py.*rollback' scripts/deploy_titan_node.sh | sort -u)
  for f in scripts/test_*.py; do
    b=$(basename "$f")
    grep -q "scripts/$b" scripts/deploy_titan_node.sh && continue
    grep -v '^#' scripts/gate_live_tests.txt | awk '{print $1}' | grep -qx "$b" && continue
    grep -q '^# gate: hermetic' "$f" || continue   # opt-in only: an unmarked test never runs in a deploy
    printf '%s||found\n' "$f"
  done
)
