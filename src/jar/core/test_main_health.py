#!/usr/bin/env python3
# gate: hermetic
"""titan-main-health (#1516): main_health.sh against a throwaway origin, checkout, queue, alert and systemd dir.
Green, the same head again (skipped), a red commit (main.red names its failing tests, one alert), `red`, a busy queue
and an eval window (skipped, nothing recorded), green again (main.red cleared), origin/main never moved, and the
deploy's `install` step (units copied once, then "current"). Nothing live: every path is a temp dir, the gate is a stub.
Run: python3 scripts/test_main_health.py"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
SCRIPT = HERE / "main_health.sh"
T = Path(tempfile.mkdtemp(prefix="main-health-"))
ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}   # a hook's GIT_DIR must not leak in
GIT = ["git", "-c", "user.name=Alex", "-c", "user.email=alex@example.invalid", "-c", "init.defaultBranch=main"]

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def sh(*args, cwd=None, env=None):
    return subprocess.run(list(args), cwd=cwd, env=env or ENV, capture_output=True, text=True, timeout=120)


# A throwaway origin whose main carries a stub gate: red while RED_MARK exists in the tree.
work, origin, repo = T / "work", T / "origin.git", T / "checkout"
(work / "scripts").mkdir(parents=True)
(work / "scripts" / "run_deploy_gate.sh").write_text(
    '#!/usr/bin/env bash\necho "ok scripts/test_a.py"\n'
    'if [ -f "$1/RED_MARK" ]; then echo "FAIL scripts/test_b.py"; echo "  [FAIL] b"; exit 1; fi\n'
    'echo "ok scripts/test_b.py"\n')
sh(*GIT, "init", "-q", str(work))
sh(*GIT, "add", "-A", cwd=work)
sh(*GIT, "commit", "-qm", "green", cwd=work)
sh(*GIT, "clone", "-q", "--bare", str(work), str(origin))
sh(*GIT, "clone", "-q", str(origin), str(repo))
before_origin_ref = sh("git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main").stdout.strip()

qdir, alerts = T / "q", T / "alerts.txt"
qdir.mkdir()
fake_gq = T / "gq.sh"
fake_gq.write_text('#!/usr/bin/env bash\necho "$*" >> "%s/calls"\n'
                   'case "$1" in window) [ -f "%s/in-window" ] && { echo "inside the eval window 0325–0350Z nightly"; exit 0; }; exit 1;;'
                   ' *) exit 0;; esac\n' % (qdir, qdir))
fake_alert = T / "alert.sh"
fake_alert.write_text('#!/usr/bin/env bash\necho "key=[$1]" >> "%s"\n' % alerts)
for f in (fake_gq, fake_alert):
    f.chmod(0o755)
RUN_ENV = {**ENV, "MAIN_HEALTH_REPO": str(repo), "MAIN_HEALTH_DIR": str(T / "mh"), "MAIN_HEALTH_LOG": str(T / "mh.jsonl"),
           "MAIN_HEALTH_RED": str(T / "main.red"), "MAIN_HEALTH_QUEUE": str(fake_gq), "MAIN_HEALTH_QUEUE_DIR": str(qdir),
           "MAIN_HEALTH_ALERT": str(fake_alert)}


def run(verb="run"):
    return sh("bash", str(SCRIPT), verb, env=RUN_ENV)


def log_lines():
    p = T / "mh.jsonl"
    return p.read_text().splitlines() if p.exists() else []


def push(red: bool, msg: str):
    mark = work / "RED_MARK"
    if red:
        mark.write_text("x")
    elif mark.exists():
        mark.unlink()
    sh(*GIT, "add", "-A", cwd=work)
    sh(*GIT, "commit", "-qm", msg, cwd=work)
    sh(*GIT, "push", "-q", str(origin), "main", cwd=work)


print("── green, then the same head again ──")
r = run()
check("a green main says so", "green (ok=2)" in r.stdout, True)
check("…one line in the log, rc 0", [('"rc": 0' in x) for x in log_lines()], [True])
check("…no main.red", (T / "main.red").exists(), False)
check("…the alert hears green (an empty key)", alerts.read_text().splitlines() if alerts.exists() else [], ["key=[]"])
check("…the lane was taken and given back", [c.split()[0] for c in (qdir / "calls").read_text().splitlines()
                                            if c.split()[0] in ("wait", "done")], ["wait", "done"])
r = run()
check("the same head within the day is skipped", ("already checked" in r.stdout, len(log_lines())), (True, 1))

print("── a red commit ──")
push(True, "red")
r = run()
head = sh("git", "-C", str(repo), "rev-parse", "--short=8", "refs/main-health/main").stdout.strip()
check("a red main names its failing test", "RED: scripts/test_b.py" in r.stdout, True)
check("…main.red holds the head and the test", (T / "main.red").read_text().strip(), f"{head} scripts/test_b.py")
check("…one alert, keyed by the failing tests", alerts.read_text().splitlines()[-1], "key=[red:scripts/test_b.py]")
check("…`red` prints it for gate_run.sh", run("red").stdout.strip(), f"{head} scripts/test_b.py")
check("…origin/main in the checkout never moved (its own ref only)",
      sh("git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main").stdout.strip(), before_origin_ref)

print("── skipped when busy ──")
push(False, "green again")
(qdir / "gate.queue").write_text("someone\n")
n = len(log_lines())
check("a listed queue skips this hour", ("busy" in run().stdout, len(log_lines())), (True, n))
(qdir / "gate.queue").write_text("")
(qdir / "gate.lock").mkdir()
check("…so does a held lane", ("busy" in run().stdout, len(log_lines())), (True, n))
(qdir / "gate.lock").rmdir()
(qdir / "in-window").write_text("")
check("…and an eval window", ("eval window" in run().stdout, len(log_lines())), (True, n))
(qdir / "in-window").unlink()

print("── green again ──")
r = run()
check("a fixed main clears main.red", ("green" in r.stdout, (T / "main.red").exists()), (True, False))
check("…and the alert hears the recovery", alerts.read_text().splitlines()[-1], "key=[]")
check("…`red` prints nothing", run("red").stdout, "")

print("── the deploy's install step ──")
sysd = T / "systemd"
sysd.mkdir()
fake_ctl = T / "systemctl.sh"
fake_ctl.write_text('#!/usr/bin/env bash\necho "$*" >> "%s/ctl"\ncase "$1" in is-enabled|is-active) [ -f "%s/enabled" ];;'
                    ' enable) touch "%s/enabled";; esac\n' % (T, T, T))
fake_ctl.chmod(0o755)
ienv = {**ENV, "SYSTEMD_DIR": str(sysd), "SUDO": "", "SYSTEMCTL": str(fake_ctl)}
r = sh("bash", str(SCRIPT), "install", env=ienv)
check("install copies both units and enables the timer", (r.stdout.strip(), sorted(p.name for p in sysd.iterdir())),
      ("main health timer installed", ["titan-main-health.service", "titan-main-health.timer"]))
check("…the deploy runs it", "bash scripts/main_health.sh install" in (HERE / "deploy_titan_node.sh").read_text(), True)
r = sh("bash", str(SCRIPT), "install", env=ienv)
check("…a second deploy finds it current", r.stdout.strip(), "main health timer current")
unit = (HERE.parent / "server-setup" / "titan-main-health.service").read_text()
check("the service runs the script as zach, never as root", ("User=zach" in unit, "main_health.sh run" in unit), (True, True))
check("gate_run.sh says when a failure is main's", "main.red" in (HERE / "gate_run.sh").read_text(), True)

shutil.rmtree(T, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
