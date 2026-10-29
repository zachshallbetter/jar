#!/usr/bin/env python3
# gate: hermetic
"""scripts/gate_worktrees.sh (#1513): the local gate on several worktrees in turn, one line each, with a stub gate
runner and throwaway git repos: a pass and a fail, the failing files named, the head read before the run, a path that
isn't a worktree (rc 2), the exit code the worst of them. Run: python3 scripts/test_gate_worktrees.py"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


T = Path(tempfile.mkdtemp(prefix="gate-worktrees-"))
ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(T), "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "Robin Hale", "GIT_AUTHOR_EMAIL": "robin@example.com", "GIT_COMMITTER_NAME": "Robin Hale",
       "GIT_COMMITTER_EMAIL": "robin@example.com", "TMPDIR": str(T)}


def repo(name: str, outcome: str) -> Path:
    d = T / name
    d.mkdir()
    (d / "outcome").write_text(outcome)
    for a in (["init", "-q"], ["add", "outcome"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True, env=ENV)
    return d


stub = T / "stub_gate.sh"
stub.write_text('if [ "$(cat outcome)" = pass ]; then printf "ok   scripts/test_a.py\\nok   scripts/test_b.py\\n"; exit 0; fi\n'
                'printf "ok   scripts/test_a.py\\nFAIL scripts/test_b.py (found)\\nFAIL scripts/test_c.py (found)\\n"; exit 1\n')
good, bad = repo("good-branch", "pass"), repo("bad-branch", "fail")
logs = T / "logs"
env = {**ENV, "GATE_WORKTREES_RUNNER": str(stub), "GATE_WORKTREES_LOGS": str(logs)}
r = subprocess.run(["bash", str(HERE / "gate_worktrees.sh"), str(good), str(bad), str(T / "nowhere")], capture_output=True,
                   text=True, env=env, timeout=60)
lines = r.stdout.splitlines()
head = subprocess.run(["git", "-C", str(good), "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True,
                      env=ENV).stdout.strip()
check("one line per worktree, in order", [ln.split()[0] for ln in lines], ["good-branch", "bad-branch", "nowhere"])
check("the pass: rc 0, its count, no failures, its head and log",
      lines[0], f"good-branch rc=0 ok=2 FAIL=none head={head} log={logs / 'good-branch.log'}")
check("the fail: rc 1 and each failing file", (lines[1].split()[1], "FAIL=scripts/test_b.py scripts/test_c.py" in lines[1]),
      ("rc=1", True))
check("a path that isn't a worktree: rc 2, said", lines[2].startswith("nowhere rc=2 not a worktree"), True)
check("the exit is the worst: 2", r.returncode, 2)
check("each gate's full log is kept", (logs / "bad-branch.log").read_text().count("FAIL"), 2)
r2 = subprocess.run(["bash", str(HERE / "gate_worktrees.sh"), str(good)], capture_output=True, text=True, env=env, timeout=60)
check("all passed: exit 0", r2.returncode, 0)
r3 = subprocess.run(["bash", str(HERE / "gate_worktrees.sh")], capture_output=True, text=True, env=env, timeout=60)
check("no worktree named: usage, exit 2", (r3.returncode, "usage" in r3.stderr), (2, True))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
