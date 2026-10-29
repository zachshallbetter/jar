#!/usr/bin/env python3
# gate: hermetic
"""ab_run.sh's run shapes (titan-node#1144, #402, #1250): side by side (all arms up, evals in parallel) and --sequential
(one box at a time, A B C per pass), --queue taking and always giving back the gate lanes, --b-profile for box vs live,
and the trap downing only the boxes this run brought up. Stubs for sandbox_box.sh, truth_eval.py, ab_readout.py and
the gate queue; the deploy hold is a temp file. Needs GNU date (titan); skipped elsewhere.
Run: python3 scripts/test_ab_run.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.600} expected={expected!r:.600}"))


if subprocess.run(["date", "-u", "-d", "@0", "+%s"], capture_output=True).returncode != 0:
    print("GNU date not available (not titan): skipped\n\n0 passed, 0 failed")
    sys.exit(0)

HERE = Path(__file__).resolve().parent


def make(tmp: Path, up_fails: str = "") -> dict[str, str]:
    repo = tmp / "repo" / "scripts"
    repo.mkdir(parents=True)
    calls = tmp / "calls.log"
    (repo / "sandbox_box.sh").write_text(
        f'#!/bin/bash\necho "box $*" >> {calls}\n'
        + (f'[ "$1" = up ] && [ "$2" = "{up_fails}" ] && exit 1\n' if up_fails else "") + "exit 0\n")
    (repo / "truth_eval.py").write_text(
        f"import os, sys\nopen({str(calls)!r}, 'a').write('eval ' + os.environ['TURN_EVAL_API'].rsplit(':', 1)[1] + ' '"
        " + sys.argv[sys.argv.index('--client') + 1] + '\\n')\n")
    (repo / "ab_readout.py").write_text("print('readout')\n")
    q = tmp / "gq.sh"
    q.write_text(f'#!/bin/bash\necho "queue $*" >> {calls}\n')
    q.chmod(0o755)
    code = tmp / "code"
    (code / "scripts").mkdir(parents=True)
    return {"AB_REPO": str(tmp / "repo"), "AB_DIR": str(tmp / "ab"), "AB_GATE_QUEUE": str(q),
            "AB_HOLD_FILE": str(tmp / "deploy.hold"), "AB_IGNORE_WINDOWS": "1", "AB_SETTLE_S": "0", "PATH": os.environ["PATH"],
            "CODE": str(code), "CALLS": str(calls)}


def run(env: dict[str, str], *args: str) -> list[str]:
    e = {k: v for k, v in env.items() if k not in ("CODE", "CALLS")}
    subprocess.run(["bash", str(HERE / "ab_run.sh"), "--a", env["CODE"], "--b", env["CODE"], "--name", "t", "--passes", "2",
                    *args], env=e, capture_output=True, timeout=600)
    p = Path(env["CALLS"])
    return [l for l in p.read_text().splitlines()] if p.exists() else []


src = (HERE / "ab_run.sh").read_text()
check("a fresh box still gets 20 s to settle by default (the test sets 0)", "SETTLE=${AB_SETTLE_S:-20}" in src, True)

print("── side by side: all three up, evals in parallel, all down ──")
with tempfile.TemporaryDirectory() as t:
    env = make(Path(t))
    calls = run(env)
    ups = [c for c in calls if c.startswith("box up")]
    downs = [c for c in calls if c.startswith("box down")]
    check("three boxes up before any eval, three down after", (len(ups), len(downs), calls.index(ups[-1]) < calls.index(
          next(c for c in calls if c.startswith("eval")))), (3, 3, True))
    check("2 passes × 2 clients × 3 arms evals", sum(c.startswith("eval") for c in calls), 12)
    check("no queue without --queue; the hold file cleared", (any(c.startswith("queue") for c in calls),
          Path(env["AB_HOLD_FILE"]).exists()), (False, False))

print("── --sequential: one box at a time, A B C per pass ──")
with tempfile.TemporaryDirectory() as t:
    env = make(Path(t))
    calls = [c for c in run(env, "--sequential") if not c.startswith("queue")]
    order = [c.split()[2] if c.startswith("box") else c.split()[1] for c in calls]
    check("up A, eval A iphone+mac, down A, then B, then C, twice",
          order, ["7773", "7773", "7773", "7773", "7776", "7776", "7776", "7776", "7779", "7779", "7779", "7779"] * 2)
    up_now, most = 0, 0
    for c in calls:
        up_now += 1 if c.startswith("box up") else -1 if c.startswith("box down") else 0
        most = max(most, up_now)
    check("never more than one box up", most, 1)

print("── --queue and --b-profile ──")
with tempfile.TemporaryDirectory() as t:
    env = make(Path(t))
    calls = run(env, "--sequential", "--queue", "solo-ab-t", "--profile", "live", "--b-profile", "box", "--no-control")
    check("waits for the lane first and gives it back last", (calls[0], calls[-1]), ("queue wait solo-ab-t", "queue done solo-ab-t"))
    check("B runs the box profile, A live", sorted({c.split()[2] + " " + c.split()[4] for c in calls if c.startswith("box up")}),
          ["7773 live", "7776 box"])

print("── a failed bring-up: only its own boxes go down, and the lane is given back ──")
with tempfile.TemporaryDirectory() as t:
    env = make(Path(t), up_fails="7776")
    calls = run(env, "--queue", "solo-ab-t")
    check("A was up, B failed: A downed, B and C never downed (not this run's), lane given back",
          ([c for c in calls if c.startswith("box down")], calls[-1]), (["box down 7773"], "queue done solo-ab-t"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
