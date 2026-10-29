#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A deploy gate test's failure is kept (10-05: the rollback said only "test_place_regions.py failed"): the last 60
lines, scrubbed of credentials, codes and secret-looking values, 0600, its path printed; every test line of the deploy
goes through run_test, and the discovery pass keeps its failures too.
Run: python3 scripts/test_deploy_fail_log.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import deploy_fail_log as D  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


tmp = Path(tempfile.mkdtemp(prefix="titan-deployfail-"))
lines = [f"  [PASS] check {i}" for i in range(100)] + [
    "  [FAIL] Zach's arrival fires that row: got=(204, 'scheduled', 0) expected=(204, 'done', 1)",
    "GITHUB_TOKEN=ghp_fictional0123456789abcdef api_key: sk-fictional-xyz",
    "Your verification code is 482913", "40 passed, 1 failed"]
out = tmp / "deploy-fail-x.log"
r = subprocess.run([sys.executable, str(HERE / "deploy_fail_log.py"), "--out", str(out)], input="\n".join(lines),
                   text=True, capture_output=True)
kept = out.read_text().splitlines()
check("written, 0600", (r.returncode, oct(out.stat().st_mode & 0o777)), (0, "0o600"))
check("the last 60 lines, the failing check among them", (len(kept), any("[FAIL] Zach's arrival" in x for x in kept),
                                                          kept[-1]), (60, True, "40 passed, 1 failed"))
check("secret-looking values, codes: removed",
      [w for w in ("ghp_fictional", "sk-fictional", "482913") if any(w in x for x in kept)], [])
check("…their names kept, so it's clear something was there", any("GITHUB_TOKEN=[removed]" in x for x in kept), True)

dep = (HERE / "deploy_titan_node.sh").read_text()
check("every explicit test line goes through run_test (none discards its output)",
      ("\npython3 scripts/test_" in dep, dep.count("\nrun_test scripts/test_") > 100), (False, True))
check("the helpers are defined in the tests' subshell, before the first test",
      dep.index("\n(\n") < dep.index("keep_fail() {") < dep.index("run_test() {") < dep.index("\nrun_test scripts/test_"), True)
check("the discovery pass keeps its failures too", dep.count('keep_fail "$b" "$out"'), 2)
check("logs go under /ai/logs, named by stamp and test, and the path is printed",
      ('log="/ai/logs/deploy-fail-$(date -u +%Y%m%d-%H%M%S)-${name%.py}.log"' in dep, 'its output is in $log' in dep), (True, True))
gate = (HERE / "gate_plan.sh").read_text()   # the plan run_deploy_gate.sh runs (#1559)
check("run_deploy_gate reads the same lines", "grep -E '^run_test scripts/test_" in gate and "gate_plan" in (HERE / "run_deploy_gate.sh").read_text(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
