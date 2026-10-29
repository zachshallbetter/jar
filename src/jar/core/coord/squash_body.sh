#!/usr/bin/env bash
# scripts/coord/squash_body.sh [BASE]: print the squash-merge body for the branch checked out here, i.e. the messages of
# BASE..HEAD (BASE defaults to the merge-base with origin/main) with every coding-tool attribution line removed (the
# co-author trailer and "Generated with" line such tools add). Zach, 10-10: no tool is ever a collaborator on his
# projects. The repos squash with COMMIT_MESSAGES, so without this one trailer on a branch commit lands on main.
# automerge.sh and merge_gated.sh pass this as --body-file.
#   scripts/coord/squash_body.sh --clean < text   cleans any text the same way (a PR body).
set -u
clean() { grep -viE '^[[:space:]]*co-authored-by:.*(claude|anthropic)|generated with \[?claude code' | cat -s; }
if [ "${1:-}" = --clean ]; then clean; exit 0; fi
base=${1:-$(git merge-base HEAD origin/main)} || exit 1
git log --reverse --format='* %B' "$base..HEAD" | clean
