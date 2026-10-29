#!/usr/bin/env python3
"""Titan's rules live in two places only: the Decision records (kb_seed_decisions.py) and the
runbook (docs/runbook.md, one note per section or section part via kb_seed_runbook.notes()). Goal item 6, 10-01.

09-30, a June knowledge-base record ("Who you are": "Data never leaves the house") answered a
question about titan's limits wrongly. It was one of 27 old-style rule records beside the current
ones (deleted 10-01). This check keeps them from coming back: every knowledge record must be a
current Decision or a current runbook section. Anything else is listed, and the exit is 1.

The deploy runs it after refreshing the runbook notes. By hand, on titan, with the agent env:
    python3 scripts/rule_records.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def allowed_titles() -> set[str]:
    import kb_seed_decisions
    import kb_seed_runbook
    return ({t for t, _ in kb_seed_decisions.DECISIONS}
            | {t for t, _ in kb_seed_runbook.notes(kb_seed_runbook.RUNBOOK.read_text(encoding="utf-8"))})


def stray(sources: list[dict], allowed: set[str]) -> list[tuple[str, str]]:
    """(source_id, why) for each knowledge record that isn't a current Decision or runbook section."""
    out = []
    for s in sources:
        if s.get("source_type") != "knowledge_record":
            continue
        title = str(s.get("title") or "")
        if title in allowed:
            continue
        if title.startswith("Decision:"):
            why = f"“{title}” is no longer in kb_seed_decisions.py (renamed or retired)"
        elif title.startswith("Titan runbook — "):
            why = f"“{title}” is no longer a section (or part) of docs/runbook.md"
        else:
            why = f"“{title}” is an old-style rule record: neither a Decision nor a runbook section"
        out.append((s["source_id"], why))
    return out


def main() -> int:
    import kb_hygiene
    sources = kb_hygiene.list_sources()
    allowed = allowed_titles()
    bad = stray(sources, allowed)
    n = sum(1 for s in sources if s.get("source_type") == "knowledge_record")
    if bad:
        for sid, why in bad:
            print(f"  STRAY {sid}: {why}")
        print(f"rule records: {n - len(bad)} current, {len(bad)} stray. Delete each (back it up first: "
              f"kb_hygiene.py), or make it a Decision in kb_seed_decisions.py or a runbook section.")
        return 1
    print(f"rule records: {n} current, 0 stray")
    return 0


if __name__ == "__main__":
    sys.exit(main())
