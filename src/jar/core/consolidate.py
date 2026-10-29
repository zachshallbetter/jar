#!/usr/bin/env python3
"""Overnight consolidation: knowledge fixes from the day's misses, proposed to the person they belong to
(titan-node#377). Never a silent edit: each proposal is a card in that person's daily slot (#360, lowest priority),
and its buttons start a turn they see.

From the last days of:
  - corrected turns (the retrieval trace, #572: a reply corrected by its own check or later by the person) and
    the notes those replies cited → "check": "A reply that used your note “X” was corrected", to the note's owner;
  - knowledge gaps (#549: a fact asked about twice that titan doesn't hold) where the person has a note whose title
    names that thing → "gapnote": "Your note “X” may have the vet's hours", to the person who asked.
Only notes the person may see; ids and titles stay on titan; counts are printed, never titles.

  python3 scripts/consolidate.py [--days 7] [--apply]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

NOTE_TYPES = ("note", "document", "upload")


def suspects(days: float, trace_db: Path | None = None) -> dict[str, int]:
    """source_id → times a corrected reply cited it."""
    import retrieval_trace
    con = retrieval_trace._db(trace_db)
    try:
        return {s: n for s, n in con.execute(
            "SELECT h.source_id, COUNT(*) FROM hits h JOIN queries q ON q.id=h.query WHERE q.corrected>0 AND h.cited=1 "
            "AND q.at>=? GROUP BY h.source_id", (time.time() - days * 86400,))}
    finally:
        con.close()


def proposals(w, sources: list[dict[str, Any]], acl: dict[str, tuple[str | None, str]], roles: dict[str, str],
              sus: dict[str, int], days: float = 7) -> list[dict[str, Any]]:
    """What to propose, to whom: [{person, kind, source_id, title, entity, attr}]."""
    import kb_privacy
    import world as W
    out: list[dict[str, Any]] = []
    titles = {s["source_id"]: str(s.get("title") or "") for s in sources}
    for sid, _n in sus.items():
        owner = (acl.get(sid) or (None, None))[0] or W.ADMIN
        if sid in titles and roles.get(owner) in ("admin", "adult", "kid"):
            out.append({"person": owner, "kind": "check", "source_id": sid, "title": titles[sid], "entity": "", "attr": ""})
    notes = [s for s in sources if str(s.get("source_type") or "") in NOTE_TYPES or "scan" in str(s.get("source_type") or "")]
    since = w.now().timestamp() - days * 86400    # the world's clock: gaps are stamped with it (a fixed test clock aged out, 10-10)
    for b in (w.bubbles() if hasattr(w, "split") else [None]):
        ww = w.world(b) if b is not None else w
        for eid, attr, person, n in ww.con.execute(
                "SELECT entity_id, attr, person, COUNT(*) FROM gaps WHERE at>=? AND closed IS NULL GROUP BY entity_id, attr, "
                "person HAVING COUNT(*)>=?", (since, W.GAP_MIN_ASKS)):
            ent = ww.entity(eid)
            role = roles.get(person)
            if ent is None or role not in ("admin", "adult", "kid"):
                continue
            full = W.norm_alias(ent["name"])
            others = {W.norm_alias(x) for x in ww.aliases(eid) if len(W.norm_alias(x)) >= 3} - {full}
            seen = [s for s in notes if kb_privacy.visible_acl(*(acl.get(s["source_id"]) or (W.ADMIN, "private")),
                                                               person, role)]
            for names in ({full}, others):        # a note titled with its full name first, then an alias
                hit = next((s for s in seen if any(f" {nm} " in " " + W.norm_alias(str(s.get("title") or "")) + " "
                                                   for nm in names)), None)
                if hit:
                    out.append({"person": person, "kind": "gapnote", "source_id": hit["source_id"],
                                "title": str(hit.get("title") or ""), "entity": ent["name"], "attr": attr})
                    break
    return out


def store(w, props: list[dict[str, Any]]) -> int:
    """Each new proposal into its person's own bubble file (a repeat is ignored). → stored."""
    import world as W
    n = 0
    for p in props:
        ww = w.world(W.bubble_name(p["person"])) if hasattr(w, "split") else w
        with W._lock, ww.con:
            n += ww.con.execute("INSERT OR IGNORE INTO note_reviews (person, kind, source_id, title, entity, attr, made) "
                                "VALUES (?,?,?,?,?,?,?)", (p["person"], p["kind"], p["source_id"], p["title"],
                                                           p["entity"], p["attr"], ww.clock())).rowcount
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    import household_identity as hh
    import kb_hygiene
    import kb_privacy
    import world as W
    w = W.get()
    sources = kb_hygiene.list_sources()
    acl = kb_privacy.acl_rows([s["source_id"] for s in sources])
    roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()}
    props = proposals(w, sources, acl, roles, suspects(a.days), a.days)
    stored = store(w, props) if a.apply else 0
    kinds: dict[str, int] = {}
    for p in props:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(json.dumps({"proposals": kinds, "people": sorted({p["person"] for p in props}), "stored": stored,
                      "dry_run": not a.apply}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
