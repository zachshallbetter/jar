#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#350: the deploy gate runs every test except a commented live list, so a new test needs no line. Hermetic."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


live = {}
for line in (HERE / "gate_live_tests.txt").read_text().splitlines():
    if line.strip() and not line.startswith("#"):
        name, _, why = line.partition(" ")
        live[name] = why.strip()
check("every live-list entry is a real test file", all((HERE / n).exists() for n in live), str([n for n in live if not (HERE / n).exists()]))
check("every live-list entry says why", all(live.values()))
check("test_household_agent is on the live list", "test_household_agent.py" in live)
dep = (HERE / "deploy_titan_node.sh").read_text()
listed = set(re.findall(r"scripts/(test_[a-z0-9_]+\.py)", dep))
check("nothing is both listed and live", not (listed & set(live)), str(listed & set(live)))
check("the deploy has the discovery pass, with the throwaway stores still set",
      # Since #637 the stores are a subshell's exports: the discovery loop must sit inside it, before it closes.
      "gate_live_tests.txt" in dep and "\n(\n" in dep and
      dep.index("\n(\n") < dep.index("TEST_STATE=") < dep.index("for f in scripts/test_*.py") < dep.index(") || exit 1"))
check("the local runner runs the same pass", "gate_live_tests.txt" in (HERE / "gate_plan.sh").read_text())
marked = {p.name for p in HERE.glob("test_*.py") if re.search(r"^# gate: hermetic", p.read_text(), re.M)}
every = {p.name for p in HERE.glob("test_*.py")}
unclassified = sorted(every - listed - marked - set(live))
check("every test is listed, marked hermetic, or on the live list (an unclassified test fails here)",
      not unclassified, f"classify: {unclassified}")
check("nothing marked hermetic is on the live list", not (marked & set(live)), str(marked & set(live)))
check("discovery is opt-in: the deploy skips a test without the marker",
      "grep -q '^# gate: hermetic'" in dep and "grep -q '^# gate: hermetic'" in (HERE / "gate_plan.sh").read_text())
print(f"  (marked, run by discovery: {', '.join(sorted(marked - listed)) or 'none'})")
fail_rx = re.compile(r"(^|[^0-9])[1-9][0-9]* failed")
check("a zero failure count passes; two or ten failures don't",
      not fail_rx.search("16 passed, 0 failed") and fail_rx.search("3 passed, 2 failed") and fail_rx.search("10 failed"))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all gate discovery checks passed'}")
sys.exit(1 if failures else 0)
