#!/usr/bin/env python3
"""#351: the Decisions seeder writes only what changed and retires a Decision no longer listed. Hermetic."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_seed_decisions as kd  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


(t0, c0), (t1, c1) = kd.DECISIONS[0], kd.DECISIONS[1]
existing = [{"source_id": "d0", "title": t0, "source_type": "knowledge_record"},
            {"source_id": "d1", "title": t1, "source_type": "knowledge_record"},
            {"source_id": "old", "title": "Decision: something retired", "source_type": "knowledge_record"},
            {"source_id": "hers", "title": "Decision: Robin's own record", "source_type": "knowledge_record"},
            {"source_id": "rb", "title": "Titan runbook — Backups", "source_type": "knowledge_record"},
            {"source_id": "mine", "title": "Decision: my own note", "source_type": "note"}]
SEEDED = {"d0": True, "d1": True, "old": True, "hers": False}
p = kd.plan(existing, {"d0": c0, "d1": c1 + " (edited)"}, SEEDED)
check("an unchanged Decision isn't rewritten", t0 in p["same"] and all(t != t0 for t, _ in p["write"]))
check("a changed Decision is rewritten", any(t == t1 for t, _ in p["write"]))
check("new Decisions are written", len(p["write"]) == len(kd.DECISIONS) - 1)
check("a Decision no longer listed is retired", p["retire"] == [("old", "Decision: something retired")], str(p["retire"]))
check("runbook sections and someone's own note are never touched",
      all(sid not in ("rb", "mine") for sid, _ in p["retire"]))
check("a person's own 'Decision: …' record the seeder didn't write is never retired",
      all(sid != "hers" for sid, _ in p["retire"]))
check("unsure whether the seeder wrote it: not retired", kd.plan(existing, {}, {})["retire"] == [])
check("whitespace differences aren't changes", kd.plan(existing[:1], {"d0": "  " + c0.replace(" ", "  ")}, SEEDED)["same"] == [t0])
check("nothing to retire: no backup", kd.retire([]) is None)
dep = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy_titan_node.sh")).read()
check("the deploy refreshes Decisions before the stray check",
      dep.index("kb_seed_decisions.py --quiet") < dep.index("python3 scripts/rule_records.py"))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all decision seeding checks passed'}")
sys.exit(1 if failures else 0)
