#!/usr/bin/env python3
"""A self-growing eval bank (#373): every fact a person stated or corrected (the world model's user evidence,
#358) becomes a (question, right answer) pair, so titan is examined on what the household actually told it.

Per person: a pair keeps the owner and audience of the statement it came from, is used only in that person's
own eval turns, and is never shown to anyone else. Titan-only (EVAL_BANK_DB, mode 600), never in git. A pair
whose fact is superseded or withdrawn is retired, so the bank never examines titan on a stale answer.

  python3 scripts/eval_bank.py harvest              (on titan; the nightly runs it)
  python3 scripts/eval_bank.py status               (counts only)
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = Path(os.environ.get("EVAL_BANK_DB", "/ai/datasets/evals/eval_bank.db"))


def _db(db: Path | None = None) -> sqlite3.Connection:
    p = db or DB
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS pairs (
        ref TEXT PRIMARY KEY, owner TEXT NOT NULL, audience TEXT NOT NULL, source TEXT NOT NULL,
        question TEXT NOT NULL, answer TEXT NOT NULL, created_at REAL NOT NULL, retired_at REAL,
        asked INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 0, last_asked REAL, last_ok INTEGER)""")
    if "misses" not in {r[1] for r in con.execute("PRAGMA table_info(pairs)")}:
        con.execute("ALTER TABLE pairs ADD COLUMN misses INTEGER NOT NULL DEFAULT 0")   # in a row (#595)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return con


def question_for(name: str, etype: str, attr: str) -> str:
    """The question a stated fact answers, in the world model's own words for it."""
    import world
    if attr == "schedule":
        return f"When is {name}?"
    if attr == "place":
        return f"Where is {name}?"
    return f"What is {world._poss(name)} {world._label_attr(etype, attr)}?"


def stated(bubbles: Any) -> list[dict[str, Any]]:
    """Every current fact a person stated or corrected (evidence kind 'user'), with who said it and who may
    see it. Proposed, rejected or superseded claims aren't facts yet (or any more)."""
    out = []
    for b in bubbles.bubbles():
        con = bubbles.world(b).con
        rows = con.execute(
            "SELECT c.id AS claim_id, c.attr, c.value, e.type, e.name, ev.by, ev.owner, ev.audience "
            "FROM claims c JOIN entities e ON e.id = c.entity_id JOIN evidence ev ON ev.claim_id = c.id "
            "WHERE ev.kind = 'user' AND c.status = 'current' AND c.qual = '' AND e.merged_into IS NULL").fetchall()
        for r in rows:
            who = r["by"] or r["owner"]
            if who and str(r["value"]).strip():
                out.append({"ref": f"world:{b}:{r['claim_id']}", "owner": str(who), "audience": str(r["audience"]),
                            "question": question_for(r["name"], r["type"], r["attr"]), "answer": str(r["value"]).strip()})
    return out


def harvest(bubbles: Any = None, db: Path | None = None, now: float | None = None) -> dict[str, int]:
    """New stated facts become pairs; a pair whose fact is no longer current is retired. → counts."""
    if bubbles is None:
        import world
        bubbles = world.get()
    now = now or time.time()
    facts = {f["ref"]: f for f in stated(bubbles)}
    con = _db(db)
    try:
        have = {r["ref"]: r for r in con.execute("SELECT ref, answer, retired_at FROM pairs")}
        added = changed = retired = 0
        for ref, f in facts.items():
            if ref not in have:
                con.execute("INSERT INTO pairs (ref, owner, audience, source, question, answer, created_at) "
                            "VALUES (?,?,?,?,?,?,?)", (ref, f["owner"], f["audience"], "world-statement", f["question"],
                                                      f["answer"], now))
                added += 1
            elif have[ref]["retired_at"] or have[ref]["answer"] != f["answer"]:
                con.execute("UPDATE pairs SET answer=?, question=?, owner=?, audience=?, retired_at=NULL WHERE ref=?",
                            (f["answer"], f["question"], f["owner"], f["audience"], ref))
                changed += 1
        for ref, r in have.items():
            if ref not in facts and not r["retired_at"]:
                con.execute("UPDATE pairs SET retired_at=? WHERE ref=?", (now, ref))
                retired += 1
        con.commit()
        live = con.execute("SELECT COUNT(*) FROM pairs WHERE retired_at IS NULL").fetchone()[0]
    finally:
        con.close()
    return {"added": added, "changed": changed, "retired": retired, "live": live}


def for_person(person: str, db: Path | None = None) -> list[dict[str, Any]]:
    """The live pairs that are this person's own: only ever asked in their own eval turns."""
    con = _db(db)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM pairs WHERE retired_at IS NULL AND owner=? ORDER BY ref",
                                             (person,))]
    finally:
        con.close()


def record(ref: str, ok: bool, db: Path | None = None, now: float | None = None) -> None:
    """A pair was asked: count it, and keep whether it passed (in the person's own store, never a log)."""
    con = _db(db)
    try:
        con.execute("UPDATE pairs SET asked = asked + 1, ok = ok + ?, last_asked = ?, last_ok = ?, "
                    "misses = CASE WHEN ? THEN 0 ELSE misses + 1 END WHERE ref = ?",
                    (1 if ok else 0, now or time.time(), 1 if ok else 0, 1 if ok else 0, ref))
        con.commit()
    finally:
        con.close()


def status(db: Path | None = None) -> dict[str, Any]:
    con = _db(db)
    try:
        by = {r[0]: r[1] for r in con.execute("SELECT owner, COUNT(*) FROM pairs WHERE retired_at IS NULL GROUP BY owner")}
        retired = con.execute("SELECT COUNT(*) FROM pairs WHERE retired_at IS NOT NULL").fetchone()[0]
    finally:
        con.close()
    return {"live_by_person": by, "retired": retired}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["harvest", "status"])
    a = ap.parse_args()
    print(harvest() if a.cmd == "harvest" else status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
