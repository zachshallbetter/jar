#!/usr/bin/env python3
# gate: hermetic
"""coord/delete_branch.sh (#1520): a branch is deleted only when no open PR uses its name, origin's head is a merged
PR's own head, no worktree has it, and the local copy has nothing unpushed. 10-10: a name match with a long-merged PR
deleted a branch an open draft used, which auto-closed it. A stand-in `gh` answers from a temp file; the remote is a
temp bare repo. Run: python3 scripts/test_delete_branch.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "coord" / "delete_branch.sh"
T = Path(tempfile.mkdtemp(prefix="delbranch-"))
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT = ["git", "-c", "user.name=Jordan", "-c", "user.email=jordan@example.invalid", "-c", "init.defaultBranch=main"]
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def git(*a, cwd=None):
    return subprocess.run([*GIT, *a], cwd=cwd, env=ENV, capture_output=True, text=True)


origin, wt = T / "origin.git", T / "wt"
git("init", "-q", "--bare", str(origin))
git("clone", "-q", str(origin), str(wt))
(wt / "a.txt").write_text("a\n")
git("add", "-A", cwd=wt); git("commit", "-qm", "base", cwd=wt); git("push", "-q", "origin", "HEAD:main", cwd=wt)


def branch(name: str, extra: str = "x") -> str:
    git("checkout", "-qb", name, "main", cwd=wt)
    (wt / f"{name}.txt").write_text(extra)
    git("add", "-A", cwd=wt); git("commit", "-qm", name, cwd=wt); git("push", "-q", "origin", name, cwd=wt)
    sha = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    git("checkout", "-q", "main", cwd=wt)
    return sha


PRS = T / "prs.json"
BIN = T / "bin"; BIN.mkdir()
(BIN / "gh").write_text(f'''#!/usr/bin/env python3
import json, sys
prs = json.load(open({str(PRS)!r}))
a = sys.argv[1:]
state, head = a[a.index("--state") + 1], a[a.index("--head") + 1]
hits = [p for p in prs if p["branch"] == head and p["state"] == state]
q = a[a.index("-q") + 1]
print(",".join(str(p["n"]) for p in hits) if "number" in q else " ".join(p["sha"] for p in hits))
''')
(BIN / "gh").chmod(0o755)
RUN_ENV = {**ENV, "PATH": f"{BIN}:{ENV['PATH']}"}


def run(name):
    r = subprocess.run(["bash", str(SCRIPT), name, str(wt)], env=RUN_ENV, capture_output=True, text=True, timeout=60)
    on_origin = bool(git("ls-remote", str(origin), f"refs/heads/{name}").stdout.strip())
    local = git("rev-parse", "-q", "--verify", f"refs/heads/{name}", cwd=wt).returncode == 0
    return r.returncode, r.stdout.strip(), on_origin, local


a = branch("memory-quality")
PRS.write_text(json.dumps([{"n": 266, "branch": "memory-quality", "state": "merged", "sha": "f" * 40},
                           {"n": 1522, "branch": "memory-quality", "state": "open", "sha": a}]))
rc, out, on_origin, local = run("memory-quality")
check("10-10's case: an open PR uses the name (an old PR of that name merged): refused, nothing deleted",
      (rc, "open PR #1522" in out, on_origin, local), (1, True, True, True))

b = branch("tidy")
PRS.write_text(json.dumps([{"n": 1600, "branch": "tidy", "state": "merged", "sha": b}]))
rc, out, on_origin, local = run("tidy")
check("merged at exactly origin's head: deleted on origin and locally", (rc, on_origin, local), (0, False, False))

c = branch("later")
PRS.write_text(json.dumps([{"n": 1601, "branch": "later", "state": "merged", "sha": "e" * 40}]))
rc, out, on_origin, _ = run("later")
check("origin's head isn't the merged PR's head (pushed after the merge): refused", (rc, "isn't a merged PR's head" in out,
      on_origin), (1, True, True))

d = branch("unpushed")
PRS.write_text(json.dumps([{"n": 1602, "branch": "unpushed", "state": "merged", "sha": d}]))
git("checkout", "-q", "unpushed", cwd=wt); (wt / "more.txt").write_text("m"); git("add", "-A", cwd=wt)
git("commit", "-qm", "not pushed", cwd=wt); git("checkout", "-q", "main", cwd=wt)
rc, out, on_origin, local = run("unpushed")
check("the local branch has a commit never pushed: refused, both kept", (rc, "never pushed" in out, on_origin, local),
      (1, True, True, True))

e = branch("busy")
PRS.write_text(json.dumps([{"n": 1603, "branch": "busy", "state": "merged", "sha": e}]))
git("worktree", "add", "-q", str(T / "busy-wt"), "busy", cwd=wt)
rc, out, on_origin, local = run("busy")
check("a worktree has it checked out: refused", (rc, "worktree" in out, on_origin, local), (1, True, True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
