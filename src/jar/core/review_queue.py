#!/usr/bin/env python3
"""The weekly review's decisions, acted on (titan-node#349).

turn_review.py drafts each Sunday: candidate findings from the week's real turns, and a sample of the newest
memory facts. They land here as review items, and the admin decides them in chat (weekly_review lists the
pending ones; review_decide acts), from the phone or the Mac:
  fact     keep    → pinned (it stays, and comes first in recall)
           forget  → forgotten, restorably (POST /v1/memory/facts/<id>/restore brings it back)
  finding  confirm → a ticket on Claude's list (the todo bridge files it as a GitHub issue)
           strike  → recorded with the reason; the same evidence isn't raised again
The share of facts kept is the weekly memory number (/ai/logs/real-turn-review.jsonl).
Findings carry counts and claims, never anyone's words: issues are read outside titan.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

DB = Path(os.environ.get("REVIEW_DB", "/ai/logs/turn-review/review.db"))
DECISIONS = {"fact": ("keep", "forget"), "finding": ("confirm", "strike")}


def _db(db: Path | None = None) -> sqlite3.Connection:
    p = db or DB
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, week TEXT NOT NULL, ref TEXT NOT NULL, text TEXT NOT NULL,
        suggest TEXT, status TEXT NOT NULL DEFAULT 'pending', reason TEXT, decided_at REAL, result TEXT)""")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return con


def signature(text: str) -> str:
    return hashlib.sha256(" ".join(str(text).lower().split()).encode()).hexdigest()[:16]


def struck_refs(db: Path | None = None) -> set[str]:
    con = _db(db)
    try:
        return {r["ref"] for r in con.execute("SELECT ref FROM items WHERE kind='finding' AND status='struck'")}
    finally:
        con.close()


def add_week(week: str, findings: list[dict[str, Any]], facts: list[dict[str, Any]], db: Path | None = None) -> int:
    """findings: [{text}] (claims and counts only); facts: [{id, text, suggest: keep|forget}]. Struck findings
    and facts already queued are skipped. Returns how many were added."""
    struck = struck_refs(db)
    con = _db(db)
    added = 0
    try:
        seen = {r["ref"] for r in con.execute("SELECT ref FROM items WHERE status='pending'")}
        n = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        for kind, rows in (("finding", findings), ("fact", facts)):
            for r in rows:
                ref = r["id"] if kind == "fact" else signature(r["text"])
                if ref in seen or (kind == "finding" and ref in struck):
                    continue
                n += 1
                con.execute("INSERT INTO items (id, kind, week, ref, text, suggest) VALUES (?,?,?,?,?,?)",
                            (f"R{n}", kind, week, ref, str(r["text"])[:300], r.get("suggest")))
                seen.add(ref)
                added += 1
        con.commit()
    finally:
        con.close()
    return added


def pending(db: Path | None = None) -> list[dict[str, Any]]:
    con = _db(db)
    try:
        return [dict(r) for r in con.execute("SELECT * FROM items WHERE status='pending' ORDER BY kind DESC, rowid")]
    finally:
        con.close()


def decide(ids: list[str], decision: str, reason: str, *, forget: Callable[[str], bool],
           pin: Callable[[str], bool], file_issue: Callable[[str], str], db: Path | None = None) -> list[str]:
    """Apply one decision to items. Returns one plain line per item."""
    decision = decision.strip().lower()
    con = _db(db)
    out: list[str] = []
    try:
        for iid in ids:
            r = con.execute("SELECT * FROM items WHERE id=?", (iid.strip().upper(),)).fetchone()
            if not r:
                out.append(f"{iid}: no such review item.")
                continue
            if r["status"] != "pending":
                out.append(f"{r['id']}: already decided ({r['status']}).")
                continue
            if decision not in DECISIONS[r["kind"]]:
                out.append(f"{r['id']}: a {r['kind']} takes {' or '.join(DECISIONS[r['kind']])}, not {decision}.")
                continue
            result = ""
            if r["kind"] == "fact":
                ok = (forget if decision == "forget" else pin)(r["ref"])
                if not ok:
                    out.append(f"{r['id']}: that fact is no longer in memory.")
                    con.execute("UPDATE items SET status='gone', decided_at=? WHERE id=?", (time.time(), r["id"]))
                    continue
                status = "forgotten" if decision == "forget" else "kept"
                result = "restorable" if decision == "forget" else "pinned"
            else:
                status = "confirmed" if decision == "confirm" else "struck"
                if decision == "confirm":
                    result = file_issue(f"Weekly review: {r['text']}"[:200])
            con.execute("UPDATE items SET status=?, reason=?, decided_at=?, result=? WHERE id=?",
                        (status, reason[:300] or None, time.time(), result, r["id"]))
            out.append(f"{r['id']}: {status}" + (f" ({result})" if result else "") + f" — {r['text'][:80]}")
        con.commit()
    finally:
        con.close()
    return out


def kept_share(week: str, db: Path | None = None) -> tuple[int, int]:
    """(facts kept, facts decided) for a week: the weekly memory number."""
    con = _db(db)
    try:
        rows = [r["status"] for r in con.execute("SELECT status FROM items WHERE kind='fact' AND week=? "
                                                 "AND status IN ('kept','forgotten')", (week,))]
    finally:
        con.close()
    return sum(1 for s in rows if s == "kept"), len(rows)


def describe(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Nothing to review: the weekly review has no pending items."
    lines = []
    for r in items:
        hint = f" (suggested: {r['suggest']})" if r.get("suggest") else ""
        lines.append(f"{r['id']} [{r['kind']}] {r['text'][:160]}{hint}")
    return ("Pending weekly-review items. Facts: keep or forget (forgetting can be undone). Findings: confirm "
            "(it becomes a ticket for Claude) or strike (with a reason).\n" + "\n".join(lines))
