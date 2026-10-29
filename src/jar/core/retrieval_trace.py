#!/usr/bin/env python3
"""Retrieval trace: what each turn's knowledge-base searches returned and what became of each hit
(titan-node#368, the foundation for KB tuning #370-#372). Content-free: source ids, types, scores and ranks,
the stage each hit reached, and whether the reply cited it; the query only as a hash and a word count.

Per turn, in memory (begin → query/mark → end), then one write:
  queries  (turn_id, at, person, path, qhash, qwords)      path: passive (the per-turn context) | search
  hits     (query, rank, source_id, source_type, score, stage, cited)
           stage: kept | dropped:floor (tidy: the score floor, downloads, a series counted once)
                  | dropped:system (titan's own notes, kept out of household turns) | dropped:topic
                  | shown (put in front of the model)
Each person's rows are theirs and the admin's (GET /v1/retrieval/trace). Rows older than RETAIN_DAYS go.

  python3 scripts/retrieval_trace.py summary [--days 7]    hits shown and never cited; drops cited elsewhere
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DB = Path(os.environ.get("RETRIEVAL_TRACE_DB", "/ai/logs/retrieval-trace.db"))
RETAIN_DAYS = 30
_T = threading.local()
_lock = threading.Lock()


def begin() -> None:
    _T.items = []


def _items() -> list[dict[str, Any]] | None:
    return getattr(_T, "items", None)


def query(path: str, text: str, raw: Iterable[dict[str, Any]], kept: Iterable[dict[str, Any]]) -> None:
    """One search: every hit the viewer may see, in rank order, and which ones the tidy step kept."""
    items = _items()
    if items is None:
        return
    keep = {str(h.get("source_id") or "") for h in kept}
    hits = []
    for i, h in enumerate(raw):
        sid = str(h.get("source_id") or "")
        if not sid:
            continue
        try:
            score = round(float(h.get("score")), 4)
        except (TypeError, ValueError):
            score = None
        md = h.get("metadata") if isinstance(h.get("metadata"), dict) else {}
        rr = h.get("rerank_score")
        hits.append({"rank": i + 1, "source_id": sid, "score": score, "rerank": rr if isinstance(rr, (int, float)) else None,
                     "source_type": str(h.get("source_type") or md.get("source") or "")[:40],
                     "stage": "kept" if sid in keep else "dropped:floor"})
    items.append({"path": path, "qhash": hashlib.sha256(str(text).strip().lower().encode()).hexdigest()[:16],
                  "qwords": len(str(text).split()), "at": time.time(), "hits": hits})


def mark(stage: str, hits: Iterable[dict[str, Any]]) -> None:
    """The last search's kept hits reached a later stage (shown, or dropped by a later filter)."""
    items = _items()
    if not items:
        return
    ids = {str(h.get("source_id") or "") for h in hits}
    for h in items[-1]["hits"]:
        if h["source_id"] in ids and h["stage"] == "kept":
            h["stage"] = stage


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS queries (id INTEGER PRIMARY KEY, turn_id TEXT, at REAL, person TEXT, "
                "path TEXT, qhash TEXT, qwords INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS hits (query INTEGER, rank INTEGER, source_id TEXT, source_type TEXT, "
                "score REAL, stage TEXT, cited INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_hits_query ON hits(query)")
    if "rerank" not in {r[1] for r in con.execute("PRAGMA table_info(hits)")}:
        con.execute("ALTER TABLE hits ADD COLUMN rerank REAL")   # #369: the reranker's score, when it ran
    if "corrected" not in {r[1] for r in con.execute("PRAGMA table_info(queries)")}:
        # #368: 1 = the turn's own claim check corrected the reply; 2 = the person corrected it in a later turn
        con.execute("ALTER TABLE queries ADD COLUMN corrected INTEGER NOT NULL DEFAULT 0")
    return con


def end(turn_id: str, person: str | None, cited: Iterable[str], path: Path | None = None, corrected: int = 0) -> int:
    """Write this turn's searches, with which sources the reply cited. → searches written."""
    items = _items() or []
    _T.items = None
    if not items:
        return 0
    cited_ids = {str(c) for c in cited}
    with _lock:
        con = _db(path)
        try:
            for q in items:
                cur = con.execute("INSERT INTO queries (turn_id, at, person, path, qhash, qwords, corrected) "
                                  "VALUES (?,?,?,?,?,?,?)",
                                  (turn_id, q["at"], person, q["path"], q["qhash"], q["qwords"], int(corrected or 0)))
                con.executemany("INSERT INTO hits (query, rank, source_id, source_type, score, stage, cited, rerank) "
                                "VALUES (?,?,?,?,?,?,?,?)",
                                [(cur.lastrowid, h["rank"], h["source_id"], h["source_type"], h["score"], h["stage"],
                                  int(h["source_id"] in cited_ids), h.get("rerank")) for h in q["hits"]])
            old = time.time() - RETAIN_DAYS * 86400
            con.execute("DELETE FROM hits WHERE query IN (SELECT id FROM queries WHERE at < ?)", (old,))
            con.execute("DELETE FROM queries WHERE at < ?", (old,))
            con.commit()
        finally:
            con.close()
    return len(items)


def mark_corrected(person: str | None, within_s: float = 1800, path: Path | None = None) -> int:
    """The person corrected titan (their memory facts say so): the searches of their latest traced turn in the last
    `within_s` seconds are marked as corrected later (2), so tuning sees what led to a wrong answer. → rows."""
    if not person:
        return 0
    with _lock:
        con = _db(path)
        try:
            r = con.execute("SELECT turn_id FROM queries WHERE person=? AND at>=? ORDER BY at DESC LIMIT 1",
                            (person, time.time() - within_s)).fetchone()
            if not r:
                return 0
            n = con.execute("UPDATE queries SET corrected=2 WHERE turn_id=? AND corrected=0", (r[0],)).rowcount
            con.commit()
            return n
        finally:
            con.close()


def rows(viewer: str | None, role: str | None, days: float = 7, person: str | None = None,
         path: Path | None = None) -> list[dict[str, Any]]:
    """Searches with their hits: a person's own; the admin's choice of person (or everyone)."""
    if role != "admin":
        person = viewer
        if not person:
            return []
    con = _db(path)
    con.row_factory = sqlite3.Row
    try:
        q = "SELECT * FROM queries WHERE at >= ?"
        a: list[Any] = [time.time() - days * 86400]
        if person:
            q += " AND person=?"
            a.append(person)
        out = []
        for r in con.execute(q + " ORDER BY at DESC LIMIT 500", a).fetchall():
            hits = [dict(h) for h in con.execute("SELECT rank, source_id, source_type, score, rerank, stage, cited FROM hits "
                                                 "WHERE query=? ORDER BY rank", (r["id"],))]
            out.append({**{k: r[k] for k in ("turn_id", "at", "person", "path", "qhash", "qwords")}, "hits": hits})
        return out
    finally:
        con.close()


def summary(days: float = 7, path: Path | None = None) -> dict[str, Any]:
    """The weekly view (#368): sources shown and never cited; sources a filter dropped that a reply cited
    elsewhere (a filter too eager); citation rate per path and per stage."""
    con = _db(path)
    try:
        since = time.time() - days * 86400
        base = "FROM hits h JOIN queries q ON q.id=h.query WHERE q.at >= ?"
        never = con.execute(f"SELECT h.source_id, COUNT(*) {base} AND h.stage='shown' GROUP BY h.source_id "
                            "HAVING SUM(h.cited)=0 ORDER BY COUNT(*) DESC LIMIT 15", (since,)).fetchall()
        cited = {r[0] for r in con.execute(f"SELECT DISTINCT h.source_id {base} AND h.cited=1", (since,))}
        dropped = con.execute(f"SELECT h.stage, h.source_id, COUNT(*) {base} AND h.stage LIKE 'dropped:%' "
                              "GROUP BY h.stage, h.source_id", (since,)).fetchall()
        rate = con.execute(f"SELECT q.path, COUNT(DISTINCT q.id), SUM(h.stage='shown'), SUM(h.cited) {base} "
                           "GROUP BY q.path", (since,)).fetchall()
        # Sources a reply cited in a turn that was corrected (by its own check or later by the person): suspects.
        wrong = con.execute(f"SELECT h.source_id, COUNT(*) {base} AND h.cited=1 AND q.corrected>0 "
                            "GROUP BY h.source_id ORDER BY COUNT(*) DESC LIMIT 15", (since,)).fetchall()
        return {"days": days,
                "shown_never_cited": [{"source_id": s, "times": n} for s, n in never],
                "dropped_but_cited_elsewhere": [{"stage": st, "source_id": s, "times": n}
                                                for st, s, n in dropped if s in cited],
                "by_path": {p: {"searches": n, "shown": int(sh or 0), "cited": int(c or 0)} for p, n, sh, c in rate},
                "cited_in_corrected_turns": [{"source_id": s, "times": n} for s, n in wrong]}
    finally:
        con.close()


def main() -> int:
    import json
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("--days", type=float, default=7)
    a = ap.parse_args()
    print(json.dumps(summary(a.days), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
