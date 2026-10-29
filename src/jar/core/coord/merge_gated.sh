#!/bin/bash
# scripts/coord/merge_gated.sh PR GATELOG WORKTREE: merge a titan-node PR only if its gate log says GATE PASSED and the PR head is
# the gated head (pushing the rebased head with a lease first). Refuses otherwise. (10-10: #1440 merged on a red gate.)
set -u
pr=$1 log=$2 wt=$3
D=$(cd "$(dirname "$0")" && pwd)   # before any cd: the helpers live next to this script
grep -q "GATE PASSED" "$log" || { echo "REFUSED: $log has no GATE PASSED"; grep -E "FAIL|GATE FAILED" "$log" | head -5; exit 1; }
gated=$(grep -oE "head [0-9a-f]{7,}" "$log" | tail -1 | awk '{print $2}')
cd "$wt" || exit 1
[ "$(git rev-parse --short=7 HEAD)" = "${gated:0:7}" ] || { echo "REFUSED: worktree HEAD isn't the gated head $gated"; exit 1; }
h=$(gh pr view "$pr" --json headRefOid --jq .headRefOid); br=$(gh pr view "$pr" --json headRefName --jq .headRefName)
[ "$h" = "$(git rev-parse HEAD)" ] || git push -q --force-with-lease="refs/heads/$br:$h" origin HEAD:"refs/heads/$br" || exit 1
sleep 3; h=$(gh pr view "$pr" --json headRefOid --jq .headRefOid)
[ "$h" = "$(git rev-parse HEAD)" ] || { echo "REFUSED: PR head $h != gated $(git rev-parse HEAD)"; exit 1; }
body=$(mktemp); trap 'rm -f "$body"' EXIT   # no tool attribution on main (squash_body.sh)
bash "$D/squash_body.sh" > "$body" || { echo "REFUSED: couldn't build the squash message"; exit 1; }
gh pr merge "$pr" --squash --body-file "$body" --match-head-commit "$h" 2>&1 | tail -1; sleep 3
gh pr view "$pr" --json state,mergeCommit --jq '.state+" "+(.mergeCommit.oid//""|.[0:8])'
