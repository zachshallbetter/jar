#!/usr/bin/env bash
# The trees that passed the full deploy gate on titan (#1514: gate once per tree). On 10-10 every merged change ran
# all ~430 tests twice: in its PR gate, and again in the deploy of the same tree. A deploy whose checkout tree passed
# the same full gate on this host recently, under the same python, reuses that pass instead of re-running it.
#
#   gate_passed.sh add TREE TESTS NAME   record a pass (gate_run.sh on titan; main_health.sh): tree, when, how many
#                                        tests were judged, python version, who gated it
#   gate_passed.sh find TREE             print "TESTS<TAB>NAME" of the newest matching pass and exit 0, or exit 1
#
# The tree hash covers everything the gate reads from the repo (the plan in deploy_titan_node.sh, gate_env.sh, the
# serial and live lists, every test); the python version covers the host. GATE_PASSED_FILE / GATE_PASSED_MAX_H
# (default 24 h) are for the tests and the deploy.
F=${GATE_PASSED_FILE:-/ai/logs/gate-passed.tsv}
MAX_H=${GATE_PASSED_MAX_H:-24}
pyv() { python3 -V 2>&1 | awk '{print $2}'; }
case "${1:-}" in
  add)
    [[ "${2:-}" =~ ^[0-9a-f]{40}$ ]] && [[ "${3:-}" =~ ^[1-9][0-9]*$ ]] || { echo "usage: $0 add TREE TESTS NAME" >&2; exit 2; }
    printf '%s\t%s\t%s\t%s\t%s\n' "$2" "$(date +%s)" "$3" "$(pyv)" "${4:-?}" >> "$F" ;;
  find)
    [[ "${2:-}" =~ ^[0-9a-f]{40}$ ]] && [ -f "$F" ] || exit 1
    awk -F'\t' -v t="$2" -v now="$(date +%s)" -v max=$((MAX_H * 3600)) -v py="$(pyv)" \
      '$1 == t && now - $2 < max && $4 == py { hit = $3 "\t" $5 } END { if (hit == "") exit 1; print hit }' "$F" 2>/dev/null ;;
  *) echo "usage: $0 add TREE TESTS NAME | find TREE" >&2; exit 2 ;;
esac
