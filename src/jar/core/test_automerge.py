#!/usr/bin/env python3
# gate: hermetic
"""coord/automerge.sh (#1519): a passed gate merges its PR only when the owner labelled it `ready`, it is open, not a
draft, based on main, not on hold, and its head is the gated head (pushed with a lease first when the gate's rebase
moved it). A stand-in `gh` on PATH answers from a temp state file and records the merge; the git remote is a temp bare
repo. Nothing reaches GitHub. Run: python3 scripts/test_automerge.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "coord" / "automerge.sh"
T = Path(tempfile.mkdtemp(prefix="automerge-"))
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
GIT = ["git", "-c", "user.name=Robin", "-c", "user.email=robin@example.invalid", "-c", "init.defaultBranch=main"]

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
git("add", "-A", cwd=wt)
git("commit", "-qm", "base", cwd=wt)
git("push", "-q", "origin", "HEAD:main", cwd=wt)
git("checkout", "-qb", "topic", cwd=wt)
(wt / "b.txt").write_text("b\n")
git("add", "-A", cwd=wt)
git("commit", "-qm", "topic change\n\nWhy it changed.\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", cwd=wt)
git("push", "-q", "origin", "topic", cwd=wt)
pushed = git("rev-parse", "HEAD", cwd=wt).stdout.strip()

STATE = T / "pr.json"
BIN = T / "bin"
BIN.mkdir()
(BIN / "gh").write_text(f'''#!/usr/bin/env python3
import json, sys, subprocess
st = json.load(open({str(STATE)!r}))
a = sys.argv[1:]
log = open({str(T / "gh.log")!r}, "a"); log.write(" ".join(a) + "\\n"); log.close()
if a[:2] == ["pr", "edit"]:
    st["body"] = sys.stdin.read() if "-" in a else ""; json.dump(st, open({str(STATE)!r}, "w")); sys.exit(0)
if a[:2] == ["pr", "merge"]:
    if "--match-head-commit" in a and a[a.index("--match-head-commit") + 1] != st["head"]:
        sys.exit(1)
    if "--body-file" in a:
        open({str(T / "squash.txt")!r}, "w").write(open(a[a.index("--body-file") + 1]).read())
    st["state"] = "MERGED"; json.dump(st, open({str(STATE)!r}, "w")); sys.exit(0)
if a[:2] == ["pr", "view"]:
    q = a[a.index("-q") + 1] if "-q" in a else ""
    # the head follows the remote branch, as GitHub's does after a push
    head = subprocess.run(["git", "--git-dir", {str(origin)!r}, "rev-parse", "refs/heads/" + st["branch"]],
                          capture_output=True, text=True).stdout.strip() or st["head"]
    st["head"] = head; json.dump(st, open({str(STATE)!r}, "w"))
    if q == ".body":
        print(st.get("body", "")); sys.exit(0)
    if "isDraft" in q:
        print("|".join([st["state"], str(st["draft"]).lower(), ",".join(st["labels"]), st["branch"], head, st["base"]]))
    elif "mergeCommit" in q:
        print(st["state"] + " " + ("abcdef12" if st["state"] == "MERGED" else ""))
    elif "headRefOid" in q:
        print(head)
    sys.exit(0)
sys.exit(2)
''')
(BIN / "gh").chmod(0o755)
RUN_ENV = {**ENV, "PATH": f"{BIN}:{ENV['PATH']}", "AUTOMERGE_NAP": "0"}


def pr(**kw):
    st = {"state": "OPEN", "draft": False, "labels": ["ready"], "branch": "topic", "head": pushed, "base": "main", **kw}
    STATE.write_text(json.dumps(st))
    (T / "gh.log").write_text("")


def run():
    r = subprocess.run(["bash", str(SCRIPT), "1600", str(wt), pushed], env=RUN_ENV, capture_output=True, text=True, timeout=120)
    merged = json.loads(STATE.read_text())["state"] == "MERGED"
    return r.returncode, r.stdout.strip(), merged


print("── refusals: nothing merged, the reason said ──")
for kw, why in (({"labels": []}, "no 'ready' label"), ({"draft": True}, "it is a draft"),
                ({"labels": ["ready", "hold"]}, "labelled hold"), ({"labels": ["ready", "needs-zach"]}, "labelled needs-zach"),
                ({"base": "release"}, "its base is release"), ({"state": "CLOSED"}, "it is CLOSED")):
    pr(**kw)
    rc, out, merged = run()
    check(f"{why}: refused", (rc, why in out, merged), (1, True, False))

print("── ready and gated ──")
pr()
rc, out, merged = run()
check("labelled ready, head = the gated head: merged, saying so", (rc, merged, "MERGED abcdef12" in out), (0, True, True))
check("…squash, pinned to the gated head", any("--squash" in ln and f"--match-head-commit {pushed}" in ln
                                               for ln in (T / "gh.log").read_text().splitlines()), True)

squash = (T / "squash.txt").read_text() if (T / "squash.txt").exists() else ""
check("…its squash message keeps the commit's words and drops the tool's co-author line (Zach 10-10)",
      ("topic change" in squash, "Why it changed." in squash, "claude" in squash.lower(), "anthropic" in squash.lower()),
      (True, True, False, False))
pr(body="Adds b.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")
rc, out, merged = run()
body = json.loads(STATE.read_text()).get("body", "")
check("…a PR body with a tool's 'Generated with' line is cleaned, the rest kept", (merged, "Adds b." in body,
      "claude" in body.lower()), (True, True, False))
sb = HERE / "coord" / "squash_body.sh"
cleaned = subprocess.run(["bash", str(sb), "--clean"], input="Fix.\n\nCo-authored-by: Claude <noreply@anthropic.com>\n"
                         "Co-authored-by: Robin <robin@example.invalid>\n", capture_output=True, text=True).stdout
check("squash_body --clean: drops only the tool's co-author line, keeps a person's",
      ("Claude" in cleaned, "Robin" in cleaned, "Fix." in cleaned), (False, True, True))
mg = (HERE / "coord" / "merge_gated.sh").read_text()
check("merge_gated.sh merges with the cleaned body too", '--body-file "$body"' in mg and "squash_body.sh" in mg, True)

print("── the gate's rebase moved the branch ──")
(wt / "c.txt").write_text("c\n")   # what gate_run's rebase does: a new local head the PR doesn't have yet
git("add", "-A", cwd=wt)
git("commit", "-qm", "rebased", cwd=wt)
gated = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
pr()
rc, out, merged = run()
check("the gated head is pushed with a lease, then merged at it", (rc, merged, git("--git-dir", str(origin), "rev-parse",
      "refs/heads/topic").stdout.strip() == gated), (0, True, True))
someone = T / "other"
git("clone", "-q", "-b", "topic", str(origin), str(someone))
(someone / "d.txt").write_text("d\n")
git("add", "-A", cwd=someone)
git("commit", "-qm", "a push after the gate", cwd=someone)
git("push", "-q", "origin", "topic", cwd=someone)
(wt / "e.txt").write_text("e\n")
git("add", "-A", cwd=wt)
git("commit", "-qm", "gated again", cwd=wt)
pr(head=pushed)   # the PR still shows the old head: the lease names it, and the remote moved past it
rc, out, merged = run()
check("…a push after the gate began: refused, nothing merged, their push kept", (rc, merged, "someone pushed" in out,
      git("--git-dir", str(origin), "rev-parse", "refs/heads/topic").stdout.strip() != git("rev-parse", "HEAD", cwd=wt).stdout.strip()),
      (1, False, True, True))

print("── wiring ──")
gr = (HERE / "gate_run.sh").read_text()
check("gate_run.sh merges only on a pass with GATE_PR, after giving the lane back",
      ('[ $rc -eq 0 ] && [ -n "${GATE_PR:-}" ]' in gr, gr.index("release; trap - EXIT") < gr.index('/coord/automerge.sh" "$GATE_PR"')),
      (True, True))
check("titan-node only (no repo argument to reach ASE or Railway repos)", "R=zachshallbetter/titan-node" in SCRIPT.read_text(),
      True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
