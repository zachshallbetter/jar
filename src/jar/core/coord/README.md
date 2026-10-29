# Coordination helpers

Moved out of a session scratchpad on 10-10 so a fresh session has them. #1519 replaces the hand-run parts.

- `merge_gated.sh PR GATELOG WORKTREE`: merges a titan-node PR only when its gate_run.sh log says GATE PASSED and the PR's head is the gated head. If the gate's rebase moved the branch, it pushes the gated head with a lease first.
  - Usage: `bash scripts/gate_run.sh NAME WORKTREE LOG`, then `bash scripts/coord/merge_gated.sh PR LOG WORKTREE`.
- `deploy.sh`: the Coordination deploy. It takes the queue's solo turn and holds both lanes.
  - It refuses in eval windows, under /ai/tmp/deploy.hold, or while model-eval is running.
  - Then it pulls main and runs `titan_preflight.sh` plus `deploy_titan_node.sh` from the Mac. Logs go to `$COORD_LOG_DIR`.
- **Merging a verified stack** (jellybean Swift 6, 10-10): rebase each PR onto main in order, and refuse when its tree differs from the verified tree (`git rev-parse X^{tree}`). Push with `--force-with-lease`, wait for CLEAN on the new head, and merge with `--match-head-commit`. Finally, check that main's tree equals the last verified tree.
- `delete_branch.sh BRANCH [REPO_DIR]` (#1520): deletes a merged branch locally and on origin, but only when all of these hold:
  - no open PR uses the name;
  - origin's head is a merged PR's own head;
  - no worktree has the branch checked out;
  - nothing on it is unpushed.

  Use this instead of `git push --delete`. It exists because of the 10-10 slip that auto-closed #1522.
- `automerge.sh` (#1519): see gate_run.sh's `GATE_PR`.
- `status.sh [--json] [--last N] [--no-titan] [--no-prs]` (#1519, item 3): the status board, run from the Mac. It prints one plain-text board, or with `--json` the same data, written by the scripts' own files rather than by messages. States are queued, running, passed, failed and verified (passed and recorded in `/ai/logs/gate-passed.tsv`). Sections:
  - **Gate queue**: the waiting names in `gate.queue` order, both lanes with their holder and how long held, each holder's heartbeat age (`gate.beat/`), and `deploy.hold`.
  - **Gate results**: the last N gate logs that `gate_run.sh` wrote on this Mac (default globs: `/private/tmp/claude-*/*/*/scratchpad/gate-*.log` and `/tmp/coord-gates/gate-*.log`; override with `STATUS_GATE_LOGS`), with the 10 slowest lines of the newest log that has them, and the last rows of `gate-passed.tsv`.
  - **Open PRs** in titan-node and jellybean (`gh pr list`): ready, draft, needs-zach or open, the head, and whether a gate log for that head says passed, failed, verified or running.
  - **Deploys**: the last lines of `/ai/logs/deploy-gate.jsonl`. **Main health**: `/ai/tmp/main.red` and the last line of `/ai/logs/main-health.jsonl`.
  - **Eval window**: whether now is inside one (titan's `gate-queue.sh window`) and the next one with its no-gates lead, from `eval_windows.py`.
  - **Mac build queue**: the lock owner and the queue in order with priority, read from `/tmp/jb-build.*`. It reads the files and does not call `jb-queue.sh show`, which touches the queue file and sweeps stale locks.

  Times are in the household zone with its label, UTC in brackets. It is read-only: one ssh call of `cat`, `ls`, `stat`, `tail`, `date` and the queue's `window` verb (no lane, no beat, no write, no process args or env), and `gh ... list`. A source that cannot be read prints `unavailable` and the rest of the board still prints. Overrides for tests: `STATUS_SSH`, `STATUS_GH`, `STATUS_REMOTE_TMP`, `STATUS_REMOTE_LOGS`, `STATUS_REMOTE_QUEUE`, `STATUS_GATE_LOGS`, `STATUS_REPOS`, `STATUS_JB_QUEUE_DIR`, `STATUS_NOW`; `scripts/test_coord_status.py` is the hermetic test.
- **Eval windows** (`scripts/eval_windows.conf`) are UTC unless a line names a zone as its 4th field (#1573); `scripts/eval_windows.py` converts a zoned line to UTC for the day asked about. See the **Clocks** section of [`docs/runbook.md`](../../docs/runbook.md#clocks) for the windows in UTC and Pacific on both sides of 2026-11-01.

## Guards

`scripts/test_shell_hygiene.py` (#1520, a hermetic gate test) scans every `*.sh` under `scripts/` (including `coord/` and `clockshift/`) for the slips we keep repeating:
- `| grep -m1` or `| head -1` inside `$(...)` under `set -o pipefail`: SIGPIPE fails the pipe on a match. Capture first, then cut.
- `$(tail ...)` or `$(cat ...)` of a file under `set -e` + pipefail with no `|| true` or existence check: a missing optional file once ended a deploy unrolled-back.
- Pattern kills: `pkill`, `killall`, `kill $(pgrep -f ...)`, `xargs kill`. Kill your own PIDs.
- `rm -rf` with an unquoted glob, a path that starts with a bare variable (use `"${VAR:?}"`), or any `/ai/tmp/*` glob.
- Printing process args or env: `ps -ef`, `ps aux`, `pgrep -af`, `/proc/*/cmdline`, `/proc/*/environ`, `systemctl show -p Environment`.
- A variable named `path` in a script that may run under zsh (no bash shebang, or meant to be sourced): zsh ties it to `PATH`.

A justified line goes in `scripts/shell_hygiene_allow.txt` as `file | rule | text on the line | reason`. An entry that no longer matches a line fails the test, so the list cannot rot.
