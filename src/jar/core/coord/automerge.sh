#!/usr/bin/env bash
# scripts/coord/automerge.sh PR WORKTREE FROM: merge a titan-node PR right after its gate passed (#1519), with no message
# to Coordination. gate_run.sh calls it when run with GATE_PR=<number> and the gate PASSED; WORKTREE's HEAD is the
# tree it just gated. On 10-10 the median "GATE PASSED" message to merge was 12 s of Coordination relaying, and the
# p90 was 32 min of waiting behind other work.
#
# It merges only when all of these hold, and says which one didn't otherwise (exit 1, nothing changed):
#   the PR is open, not a draft, based on main, labelled `ready` by its owner, and not labelled hold / needs-zach /
#   do-not-merge; its head on GitHub is still FROM (the head the gate started from) or already the gated head. The
#   gated head is pushed with --force-with-lease=FROM, so a push after the gate began is never overwritten.
# The merge is squash with --match-head-commit, so a push after the gate can't slip in; it confirms MERGED.
# Never for ASE or a Railway-deploying repo (AUTOMERGE_REPO is titan-node only), never a deploy.
set -u
pr=${1:?usage: automerge.sh PR WORKTREE FROM} wt=${2:?usage: automerge.sh PR WORKTREE FROM} from=${3:?usage: automerge.sh PR WORKTREE FROM}
R=zachshallbetter/titan-node
D=$(cd "$(dirname "$0")" && pwd)   # before any cd: the helpers live next to this script
NAP=${AUTOMERGE_NAP:-3}
refuse() { echo "automerge #$pr: not merged: $1"; exit 1; }
info=$(gh pr view "$pr" -R "$R" --json state,isDraft,labels,headRefName,headRefOid,baseRefName \
  -q '[.state, (.isDraft|tostring), ([.labels[].name]|join(",")), .headRefName, .headRefOid, .baseRefName]|join("|")') \
  || refuse "couldn't read the PR"
IFS='|' read -r state draft labels br head base <<<"$info"   # never a tab: read collapses empty tab fields
[ "$state" = OPEN ] || refuse "it is ${state:-unknown}"
[ "$draft" = false ] || refuse "it is a draft"
[ "$base" = main ] || refuse "its base is $base, not main"
case ",$labels," in *,ready,*) ;; *) refuse "no 'ready' label (its owner adds it to let a passed gate merge it)" ;; esac
for l in hold needs-zach do-not-merge; do case ",$labels," in *",$l,"*) refuse "labelled $l" ;; esac; done
cd "$wt" || refuse "no worktree $wt"
mine=$(git rev-parse HEAD) || refuse "no HEAD in $wt"
if [ "$head" != "$mine" ]; then
  [ "$head" = "$from" ] || refuse "someone pushed ${head:0:8} after the gate started from ${from:0:8}; gate again"
  git push -q --force-with-lease="refs/heads/$br:$from" origin "HEAD:refs/heads/$br" || refuse "couldn't push the gated head"
fi
h=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  h=$(gh pr view "$pr" -R "$R" --json headRefOid -q .headRefOid) && [ "$h" = "$mine" ] && break; sleep "$NAP"
done
[ "$h" = "$mine" ] || refuse "the PR head ${h:0:8} isn't the gated ${mine:0:8}"
# No tool attribution reaches main or stays on the PR (Zach 10-10: no tool is ever a collaborator): the squash body is
# the branch's messages without co-author/"Generated with" lines, and the PR body loses them too.
body=$(mktemp); trap 'rm -f "$body"' EXIT
bash "$D/squash_body.sh" > "$body" || refuse "couldn't build the squash message"
pb=$(gh pr view "$pr" -R "$R" --json body -q .body 2>/dev/null) || pb=""
if [ -n "$pb" ] && [ "$(printf '%s\n' "$pb" | bash "$D/squash_body.sh" --clean)" != "$pb" ]; then
  printf '%s\n' "$pb" | bash "$D/squash_body.sh" --clean | gh pr edit "$pr" -R "$R" --body-file - >/dev/null 2>&1 \
    || echo "automerge #$pr: note: couldn't clean the PR body"
fi
for _ in 1 2 3 4 5; do
  gh pr merge "$pr" -R "$R" --squash --body-file "$body" --match-head-commit "$mine" >/dev/null 2>&1 && break; sleep "$NAP"
done
for _ in 1 2 3 4 5 6 7 8 9 10; do
  st=$(gh pr view "$pr" -R "$R" --json state,mergeCommit -q '.state+" "+(.mergeCommit.oid//""|.[0:8])')
  [ "${st%% *}" = MERGED ] && { echo "automerge #$pr: MERGED ${st#* } (gated ${mine:0:8})"; exit 0; }; sleep "$NAP"
done
refuse "gh pr merge didn't land (state: ${st:-unknown})"
