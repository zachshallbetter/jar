#!/usr/bin/env python3
"""Self-exam misses, diagnosed (titan-node#595, #373's follow-up). A pair the nightly self-exam (#594) missed on two
nights running is looked up again, in code, as its owner:
  not found        the question doesn't find the entity: proposed to the owner ("What do you call it?"), a card in
                   their daily slot (#360, lowest priority), never an edit
  not answered     the entity is found but the turn block has no answer with the stated value: a code fix (report)
  reply missed     the block has the value; the model's reply didn't say it: wording (report)
  retired          the fact isn't current any more (the bank retires it on its next harvest)
Each flagged pair is re-asked first on the next night. Reported by pair id only; nothing else is printed or stored.

  python3 scripts/selfexam_fixes.py [--apply]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
MISSES = 2


def diagnose(w, pairs: list[dict[str, Any]], roles: dict[str, str]) -> list[dict[str, Any]]:
    import self_exam
    import world as W
    out = []
    for p in pairs:
        if (p.get("misses") or 0) < MISSES or not str(p["ref"]).startswith("world:"):
            continue
        _, bubble, claim_id = p["ref"].split(":", 2)
        ww = w.world(bubble) if hasattr(w, "split") else w
        c = ww.claim(int(claim_id))
        row = {"pair": self_exam.pair_id(p["ref"]), "ref": p["ref"], "owner": p["owner"]}
        if c is None or c["status"] != "current":
            out.append({**row, "why": "retired"})
            continue
        ent = ww.entity(c["entity_id"])
        cid = f"{bubble}:{ent['id']}" if hasattr(w, "split") else ent["id"]
        role = roles.get(p["owner"], "adult")
        if cid not in w.find(p["question"], p["owner"], role):
            out.append({**row, "why": "not found", "entity": ent["name"], "attr": c["attr"]})
            continue
        block, _ = W.context(w, p["question"], p["owner"], role)
        out.append({**row, "why": "reply missed" if self_exam.passes(block, p["answer"]) else "not answered"})
    return out


def propose(w, rows: list[dict[str, Any]]) -> int:
    """'not found' → a review for its owner (in their own bubble file), once."""
    import world as W
    n = 0
    for r in rows:
        if r["why"] != "not found":
            continue
        ww = w.world(W.bubble_name(r["owner"])) if hasattr(w, "split") else w
        with W._lock, ww.con:
            n += ww.con.execute("INSERT OR IGNORE INTO note_reviews (person, kind, source_id, title, entity, attr, made) "
                                "VALUES (?,?,?,?,?,?,?)", (r["owner"], "unfound", r["ref"], r["entity"], r["entity"],
                                                           r["attr"], ww.clock())).rowcount
    return n


def reask_first(rows: list[dict[str, Any]], db=None) -> None:
    """Flagged pairs go to the front of the next night's exam (least recently asked first)."""
    import eval_bank
    con = eval_bank._db(db)
    try:
        con.executemany("UPDATE pairs SET last_asked = 0 WHERE ref = ?", [(r["ref"],) for r in rows if r["why"] != "retired"])
        con.commit()
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    import eval_bank
    import household_identity as hh
    import world as W
    roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()}
    w = W.get()
    pairs = [p for person in (W.ADMIN,) for p in eval_bank.for_person(person)]   # the admin's pairs only (#594)
    rows = diagnose(w, pairs, roles)
    if a.apply:
        propose(w, rows)
        reask_first(rows)
    print(json.dumps({"flagged": [{"pair": r["pair"], "why": r["why"]} for r in rows], "dry_run": not a.apply}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
