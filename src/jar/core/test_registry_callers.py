#!/usr/bin/env python3
# gate: hermetic
"""Fleet services in the runtime registry (#1623, Zach 10-14). The seven runtimes and two brokers that titan-node itself
never calls carry "callers": "fleet"; titan-node's own six runtimes, the document runtime and the decision broker don't.
Preflight names a fleet service as "(fleet)" and still checks and alerts on it. Reads the registry file only.
Run: python3 scripts/test_registry_callers.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import preflight_check as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


reg = json.loads((HERE / "runtime-registry.json").read_text())
fleet = sorted(e["name"] for e in reg["runtimes"] + reg["brokers"] if e.get("callers") == "fleet")
check("exactly the fleet's runtimes and brokers are marked", fleet, sorted([
    "identity-runtime", "image-runtime", "intelligence-runtime", "policy-runtime", "text-runtime", "posture-runtime",
    "observer-runtime", "llm-model-broker", "image-broker"]))
check("titan-node's own are not", [e["name"] for e in reg["runtimes"] + reg["brokers"]
                                   if e["name"] in ("retrieval-runtime", "workflow-runtime", "record-runtime", "fetch-runtime",
                                                    "event-runtime", "epistemic-runtime", "document-runtime", "decision-broker")
                                   and e.get("callers")], [])
check("the only value is fleet", {e.get("callers") for e in reg["runtimes"] + reg["brokers"]} <= {None, "fleet"}, True)
check("every entry still has its health URL (preflight still checks it)",
      all(e.get("health_url") for e in reg["runtimes"] + reg["brokers"]), True)
check("preflight's labels", (P.label({"name": "text-runtime", "callers": "fleet"}), P.label({"name": "record-runtime"}),
                             P.label({"name": "image-broker", "callers": "fleet"}, broker=True),
                             P.label({"name": "decision-broker"}, broker=True)),
      ("text-runtime(fleet)", "record-runtime", "image-broker(broker,fleet)", "decision-broker(broker)"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
