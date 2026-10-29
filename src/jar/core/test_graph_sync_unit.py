#!/usr/bin/env python3
# gate: hermetic   (a temp systemd dir and a fake systemctl; nothing installed, nothing started)
"""titan-graph-sync (#1447): the 10-minute graph sync's guard and install, and its units.

guard: skips (exit 1, which systemd's ExecCondition treats as "skip", not a failure) while either nightly eval unit is
active, activating or reloading, and lets the run go otherwise. install: copies the units only when they differ,
daemon-reloads only then, enables the timer when it isn't enabled and active, never starts or restarts the service,
and says "current" when there's nothing to do; a failed copy is an error (the deploy keeps going and warns). The units:
--since every 10 minutes, the guard as ExecCondition, agent.env, never during an eval.
Run: python3 scripts/test_graph_sync_unit.py
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "graph_sync_unit.sh"
UNITS = HERE.parent / "server-setup"
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


T = Path(tempfile.mkdtemp(prefix="titan-graph-unit-"))
SYSD = T / "systemd"
SYSD.mkdir()
LOG = T / "calls.log"
STATE = T / "state"
FAKE = T / "systemctl"
FAKE.write_text(f"""#!/bin/bash
echo "$*" >> {LOG}
case "$1" in
  show) u="${{@: -1}}"; cat "{STATE}/$u" 2>/dev/null || echo inactive ;;
  is-enabled|is-active) [ -f "{STATE}/titan-graph-sync.timer.$1" ] ;;
  enable) touch "{STATE}/titan-graph-sync.timer.is-enabled" "{STATE}/titan-graph-sync.timer.is-active" ;;
  *) : ;;
esac
""")
FAKE.chmod(0o755)
STATE.mkdir()


def run(*a, sudo=""):
    env = {**os.environ, "SYSTEMCTL": str(FAKE), "SUDO": sudo, "SYSTEMD_DIR": str(SYSD)}
    return subprocess.run(["bash", str(SCRIPT), *a], capture_output=True, text=True, env=env, timeout=30)


def calls():
    out = LOG.read_text().splitlines() if LOG.exists() else []
    LOG.write_text("")
    return [c for c in out if not c.startswith(("show", "is-"))]


print("── guard: never during a nightly eval ──")
for unit in ("titan-household-eval.service", "titan-model-eval.service"):
    for st, skip in (("active", True), ("activating", True), ("reloading", True), ("inactive", False), ("failed", False)):
        (STATE / unit).write_text(st + "\n")
        r = run("guard")
        check(f"{unit.split('.')[0]} {st}: {'skipped (exit 1)' if skip else 'runs (exit 0)'}", r.returncode, 1 if skip else 0)
    (STATE / unit).write_text("inactive\n")
(STATE / "titan-model-eval.service").write_text("activating\n")
check("…a skip says why, by unit name", run("guard").stdout.strip(), "graph sync skipped: titan-model-eval is running")
(STATE / "titan-model-eval.service").write_text("inactive\n")

print("── install ──")
r = run("install")
check("first time: both units copied, daemon-reload, the timer enabled --now", (r.returncode, calls()),
      (0, ["daemon-reload", "enable --now titan-graph-sync.timer"]))
check("…the copies are the repo's units", all((SYSD / u).read_text() == (UNITS / u).read_text()
                                              for u in ("titan-graph-sync.service", "titan-graph-sync.timer")), True)
check("…0644", oct((SYSD / "titan-graph-sync.service").stat().st_mode & 0o777), "0o644")
r = run("install")
check("again, nothing changed: \"current\", no systemctl change at all", (r.returncode, r.stdout.strip(), calls()),
      (0, "graph sync timer current", []))
(SYSD / "titan-graph-sync.service").write_text("[Service]\nExecStart=/bin/true\n")
r = run("install")
check("a changed unit: copied and daemon-reloaded, never restarted or started", (r.returncode, calls()), (0, ["daemon-reload"]))
check("…and no call ever restarts or starts the service",
      any(("restart" in c or c.startswith("start")) for c in (LOG.read_text().splitlines() if LOG.exists() else [])), False)
(SYSD / "titan-graph-sync.timer").unlink()
r = run("install", sudo="false")
check("a copy that fails: an error with the unit's name (the deploy warns and keeps going)",
      (r.returncode, "couldn't install titan-graph-sync.timer" in r.stderr), (1, True))
check("an unknown verb: usage", run("frobnicate").returncode, 2)

print("── the units ──")
svc = (UNITS / "titan-graph-sync.service").read_text()
tmr = (UNITS / "titan-graph-sync.timer").read_text()
check("the service: oneshot, as zach, agent.env, the guard as ExecCondition, graph_sync --since",
      all(x in svc for x in ("Type=oneshot", "User=zach", "EnvironmentFile=/ai/projects/titan-local-agent/agent.env",
                              "ExecCondition=/bin/bash /ai/projects/titan-node/scripts/graph_sync_unit.sh guard",
                              "scripts/graph_sync.py --since")), True)
check("…never --full on a timer (--since already rebuilds a missing or wiped graph)", "--full" in svc.split("ExecStart")[1], False)
check("the timer: every 10 minutes after the last run", ("OnUnitActiveSec=10min" in tmr, "WantedBy=timers.target" in tmr), (True, True))
deploy = (HERE / "deploy_titan_node.sh").read_text()
hook = [l for l in deploy.splitlines() if l.startswith("bash scripts/graph_sync_unit.sh install")]
check("the deploy installs it after the canary, a warning (never a rollback) if it can't",
      (len(hook), "|| echo" in hook[0] if hook else False,
       deploy.index(hook[0]) > deploy.index("python3 scripts/agent_canary.py") if hook else False), (1, True, True))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
