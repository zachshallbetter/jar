#!/usr/bin/env python3
"""Stored memory facts the extractor's new rules would have refused (10-02 audit): ones about who's in the household
or what someone is to it ("The user's household has a child named Robin": the people store's to say, and wrong),
and one-off dated events. With --apply each is moved aside as superseded (forgotten_facts, restorable with
restore_fact), never deleted: deleting is Zach's call. Prints counts per person and reason, never a fact's text.

  python3 scripts/memory_audit.py            dry run
  python3 scripts/memory_audit.py --apply    (a fleet write: only with the owner session's go)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))


def audit(mem: Any, people: list[dict[str, Any]], apply: bool = False) -> dict[str, Any]:
    import agent_memory as am
    out: dict[str, dict[str, int]] = {}
    ids = sorted({str(p["id"]) for p in people} | {am.OWNER})
    for pid in ids:
        rows = mem._db.execute("SELECT id, text FROM facts WHERE person_id=?", (pid,)).fetchall()
        for r in rows:
            why = "roster" if am.contradicts_roster(r["text"], people) else "one_off" if am.one_off(r["text"]) else None
            if not why:
                continue
            out.setdefault(pid, {}).setdefault(why, 0)
            out[pid][why] += 1
            if apply:
                mem.forget_fact(r["id"], person_id=pid)
    return {"by_person": out, "total": sum(n for d in out.values() for n in d.values()), "applied": apply}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    import agent_memory as am
    import household_identity as hh
    print(json.dumps(audit(am.AgentMemory(), hh.get_store().people(), a.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
