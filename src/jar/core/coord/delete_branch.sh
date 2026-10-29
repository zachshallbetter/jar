#!/usr/bin/env bash
# scripts/coord/delete_branch.sh BRANCH [REPO_DIR]: delete a merged branch, locally and on origin, only when that is
# provably safe (#1520). 10-10: a cleanup deleted `memory-quality` because its name matched a long-merged PR (#266);
# that auto-closed the open draft #1522 using the same name, and its owner had to restore it from pull/1522/head.
#
# It refuses (exit 1, nothing deleted) when:
#   - any OPEN pull request uses BRANCH as its head (whatever was merged under that name before);
#   - origin's BRANCH head isn't the head of a MERGED pull request of that name (squash merges leave no ancestry, so
#     "merged" is the PR's own head SHA, never the name and never `git branch --merged`);
#   - a worktree has BRANCH checked out (remove the worktree first; this never touches one);
#   - the local BRANCH has commits origin's doesn't (unpushed work).
# A branch that only exists locally, with no PR, is refused too: say what it was, and delete it by hand.
set -u
br=${1:?usage: delete_branch.sh BRANCH [REPO_DIR]}
cd "${2:-.}" || exit 2
R=${DELETE_BRANCH_REPO:-zachshallbetter/titan-node}
refuse() { echo "delete_branch $br: refused: $1"; exit 1; }
open=$(gh pr list -R "$R" --state open --head "$br" --json number -q '[.[].number]|join(",")') || refuse "couldn't list PRs"
[ -z "$open" ] || refuse "open PR #$open uses this branch name"
remote=$(git ls-remote origin "refs/heads/$br" | awk '{print $1}')
[ -z "$remote" ] || git fetch -q origin "refs/heads/$br" 2>/dev/null || refuse "couldn't fetch origin's branch"
merged=$(gh pr list -R "$R" --state merged --head "$br" --json headRefOid -q '[.[].headRefOid]|join(" ")') || refuse "couldn't list PRs"
if [ -n "$remote" ]; then
  case " $merged " in *" $remote "*) ;; *) refuse "origin's head ${remote:0:8} isn't a merged PR's head (merged heads: ${merged:-none})" ;; esac
fi
if git worktree list --porcelain | grep -qx "branch refs/heads/$br"; then refuse "a worktree has it checked out"; fi
if git rev-parse -q --verify "refs/heads/$br" >/dev/null; then
  base=${remote:-$(sed 's/ .*//' <<<"$merged")}
  [ -n "$base" ] || refuse "local only, and no merged PR of this name"
  git cat-file -e "$base^{commit}" 2>/dev/null || refuse "can't see ${base:0:8} here to compare (git fetch first)"
  [ -z "$(git rev-list "refs/heads/$br" --not "$base" 2>/dev/null)" ] || refuse "the local branch has commits that were never pushed or merged"
  git branch -D "$br" >/dev/null || refuse "git branch -D failed"
  echo "delete_branch $br: deleted the local branch"
fi
if [ -n "$remote" ]; then
  git push -q origin --delete "$br" || refuse "couldn't delete origin's branch"
  echo "delete_branch $br: deleted origin's branch (it was merged at ${remote:0:8})"
fi
