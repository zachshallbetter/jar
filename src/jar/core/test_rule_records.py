#!/usr/bin/env python3
"""Rules live in Decisions and the runbook only (goal item 6). Hermetic."""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import kb_seed_decisions  # noqa: E402
import rule_records  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


allowed = rule_records.allowed_titles()
dec = next(iter(t for t, _ in kb_seed_decisions.DECISIONS))
rb = next(t for t in allowed if t.startswith("Titan runbook — "))
src = [
    {"source_id": "d", "source_type": "knowledge_record", "title": dec},
    {"source_id": "r", "source_type": "knowledge_record", "title": rb},
    {"source_id": "mail", "source_type": "connector:mail", "title": "Who you are"},
    {"source_id": "old", "source_type": "knowledge_record", "title": "Who you are"},
    {"source_id": "gone", "source_type": "knowledge_record", "title": "Decision: something retired"},
    {"source_id": "sec", "source_type": "knowledge_record", "title": "Titan runbook — A section that was removed"},
]
bad = dict(rule_records.stray(src, allowed))
check("current Decisions and runbook sections pass", "d" not in bad and "r" not in bad)
check("only knowledge records are judged", "mail" not in bad)
check("an old-style record is stray", "old" in bad and "old-style" in bad["old"], str(bad))
check("a retired Decision is stray", "gone" in bad and "kb_seed_decisions" in bad["gone"])
check("a removed runbook section is stray", "sec" in bad and "runbook" in bad["sec"])
check("every Decision is titled “Decision: …”", all(t.startswith("Decision: ") for t, _ in kb_seed_decisions.DECISIONS))

# 10-04 (#912): a long section is several notes (kb_seed_runbook.parts); every one is current, none stray.
import kb_seed_runbook  # noqa: E402
_parts = [t for t, _ in kb_seed_runbook.notes(kb_seed_runbook.RUNBOOK.read_text(encoding="utf-8")) if t.count(" — ") >= 2]
check("the runbook's section parts are allowed titles", bool(_parts) and set(_parts) <= rule_records.allowed_titles(), str(_parts[:2]))

# Only the two seeders write knowledge records. Readers (this check, the owner backfill, hygiene, kb_privacy) may name it.
WRITERS = {"kb_seed_decisions.py", "kb_seed_runbook.py"}
READERS = {"rule_records.py", "kb_backfill_owner.py", "kb_hygiene.py", "kb_privacy.py",   # kb_privacy: what an engineering agent reads (#355)
           "zones.py",   # zones: a knowledge record is the system zone (#833)
           "household_reset.py"}   # the reset keeps knowledge records (titan's seeds): it names them, never writes one
others = sorted(p.name for p in HERE.glob("*.py")
                if not p.name.startswith("test_") and p.name not in WRITERS | READERS
                and re.search(r"[\"']knowledge_record[\"']", p.read_text(encoding="utf-8", errors="ignore")))
check("no other code writes a knowledge record", not others, str(others))
check("the old seeders stay deleted", not (HERE / "kb_seed_titan.py").exists() and not (HERE / "kb_seed_jellybean.py").exists())
docs = (HERE.parent / "docs" / "runbook.md").read_text(encoding="utf-8")
check("the runbook doesn't say to run them", "kb_seed_titan" not in docs and "kb_seed_jellybean" not in docs)
dep = (HERE / "deploy_titan_node.sh").read_text()
check("the deploy runs the live check after the runbook refresh",
      "rule_records.py" in dep and dep.index("kb_seed_runbook.py --quiet") < dep.index("scripts/rule_records.py"))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all rule-record checks passed'}")
sys.exit(1 if failures else 0)
