#!/usr/bin/env python3
# gate: hermetic
"""titan-reranker is disabled on purpose (#1622, 10-15: the A/B showed reranking doesn't help the KB bank). Nothing that
checks titan's health expects it: not preflight (the registry), not the canary, not boot resilience (it's optional
there, reported, never required), not the system-world service list. titan-exec can't restart it or any unit that
isn't enabled. KB_RERANK's code stays, off. Reads files and modules only. Run: python3 scripts/test_reranker_off.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


reg = json.loads((HERE / "runtime-registry.json").read_text())
check("preflight's registry has no reranker", any("rerank" in e["name"] for e in reg["runtimes"] + reg["brokers"]), False)
check("the canary doesn't check it", "rerank" in (HERE / "agent_canary.py").read_text().lower(), False)
check("preflight_check doesn't name it", "rerank" in (HERE / "preflight_check.py").read_text().lower(), False)
boot = json.loads((HERE / "boot_resilience_units.json").read_text())
check("boot resilience: optional (reported), not required",
      ("titan-reranker.service" in boot["always_on_services"], "titan-reranker.service" in boot["optional"]), (False, True))
import system_world  # noqa: E402
check("the system-world services don't list it", [s for s in system_world.SERVICES if "rerank" in s[0] or "rerank" in s[2]], [])
import titan_exec  # noqa: E402
check("titan-exec can't touch it", ("titan-reranker.service" in titan_exec.UNITS, "titan-reranker.service" in titan_exec.MODELS),
      (False, False))
inst = (HERE.parent / "server-setup" / "install_titan_exec.sh").read_text()
check("the installer's fixed lists and its verify reads don't name it", "titan-reranker" in inst, False)
import kb_rerank  # noqa: E402
check("KB_RERANK stays off by default (the code stays for a re-test)", kb_rerank.enabled(), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
