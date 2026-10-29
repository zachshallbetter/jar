#!/usr/bin/env python3
"""The knowledge base's misses, collected per person (#377 half 1; design on the issue, 10-06). Read-only.

A miss is a retrieval-trace query (#368) that was corrected, or an explicit search that found nothing: the same rule as
the weekly turn report (agent_turn_report.knowledge_misses). The trace is content-free, so a miss's question is read
where it already lives: the person's own user message in their own conversation (conv_id_for) just before the lookup,
within QUESTION_WINDOW_S. Nothing is copied anywhere; a person's misses are only ever read for that person, for the
titan-local nightly job that will propose fixes (half 2, after review).

Three more signals, counted only (#377 P1, the Coordination design of 10-10), none read for proposals yet:
  kept nothing   an explicit search (search, search-rerank) with hits where none reached `shown` (all dropped by the
                 floor, the topic filter or the system bubble): the empty-search rule misses these
  rephrase       an explicit search that showed nothing, followed within REPHRASE_S by the same person's lookup in
                 another turn, worded differently (qhash), that was shown and cited: the first one is the miss. A passive
                 lookup never starts a pair: it shows nothing on most turns (the topic filter), so "hi" and then a new
                 question would count
  KB corrected   corrected by the person later (2), or by the turn's own check (1) only when one of its KB hits was
                 cited: a 1 with nothing cited is the calendar/reminder claim check (answer_corrections), not the KB

  python3 scripts/knowledge_misses.py counts      the household's counts (7 / 14 days), each signal, no words
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

QUESTION_WINDOW_S = 120
REPHRASE_S = 300
EXPLICIT = ("search", "search-rerank")
DAYS = 14


def _trace(path: Path | None = None) -> Path:
    return Path(path or os.environ.get("RETRIEVAL_TRACE_DB", "/ai/logs/retrieval-trace.db"))


def _rows(trace: Path, since: float, person: str | None = None) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{trace}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        q = ("SELECT q.id, q.at, q.person, q.path, q.corrected > 0 AS corrected, "
             "NOT EXISTS (SELECT 1 FROM hits h WHERE h.query=q.id) AS empty FROM queries q WHERE q.at >= ?")
        args: list[Any] = [since]
        if person is not None:
            q, args = q + " AND q.person=?", args + [person]
        rows = con.execute(q + " ORDER BY q.at", args).fetchall()
        return [r for r in rows if r["corrected"] or (r["path"] == "search" and r["empty"])], con
    except sqlite3.Error:
        con.close()
        raise


def _signals(con: sqlite3.Connection, since: float) -> dict[str, int]:
    """The P1 signals since `since`, as counts of lookups (module docstring). Rows never leave this function."""
    qs = con.execute("SELECT q.id, q.at, q.person, q.turn_id, q.path, q.qhash, q.corrected, "
                     "(SELECT COUNT(*) FROM hits h WHERE h.query=q.id) AS n, "
                     "EXISTS (SELECT 1 FROM hits h WHERE h.query=q.id AND h.stage='shown') AS shown, "
                     "EXISTS (SELECT 1 FROM hits h WHERE h.query=q.id AND h.cited=1) AS cited, "
                     "EXISTS (SELECT 1 FROM hits h WHERE h.query=q.id AND h.stage='shown' AND h.cited=1) AS used "
                     "FROM queries q WHERE q.at >= ? ORDER BY q.at", (since,)).fetchall()
    kept_nothing = sum(1 for q in qs if q["path"] in EXPLICIT and q["n"] and not q["shown"])
    kb_corrected = sum(1 for q in qs if q["corrected"] == 2 or (q["corrected"] == 1 and q["cited"]))
    rephrase = 0
    for i, a in enumerate(qs):
        if a["path"] not in EXPLICIT or a["shown"] or not a["person"]:
            continue
        for b in qs[i + 1:]:
            if b["at"] - a["at"] > REPHRASE_S:
                break
            if (b["person"] == a["person"] and b["used"] and b["qhash"] != a["qhash"]
                    and not (a["turn_id"] and a["turn_id"] == b["turn_id"])):   # another turn: the person asked again
                rephrase += 1
                break
    return {"kept_nothing": kept_nothing, "rephrase": rephrase, "kb_corrected": kb_corrected}


def counts(trace_db: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """The household's real misses over 7 and 14 days, and each P1 signal on its own: numbers only, never a person,
    a qhash or a word."""
    trace = _trace(trace_db)
    if not trace.exists():
        return {}
    now = time.time() if now is None else now
    out = {}
    for days in (7, 14):
        rows, con = _rows(trace, now - days * 86400)
        try:
            out[days] = {"misses": len(rows), "corrected": sum(1 for r in rows if r["corrected"]),
                         "empty_searches": sum(1 for r in rows if not r["corrected"]),
                         **_signals(con, now - days * 86400)}
        finally:
            con.close()
    return out


def collect(person: str, mem: Any, *, trace_db: Path | None = None, now: float | None = None,
            days: int = DAYS) -> list[dict[str, Any]]:
    """This person's misses, each with their own question: {query_id, at, path, corrected, question, shown, cited}.
    For the titan-local nightly job only; never printed, never sent anywhere."""
    import household_identity as hh
    trace = _trace(trace_db)
    if not person or not trace.exists():
        return []
    now = time.time() if now is None else now
    rows, con = _rows(trace, now - days * 86400, person)
    try:
        hits = {r["id"]: con.execute("SELECT source_id, cited, stage, rank FROM hits WHERE query=? ORDER BY rank",
                                     (r["id"],)).fetchall() for r in rows}
    finally:
        con.close()
    conv = hh.conv_id_for(person)
    out = []
    for r in rows:
        msg = mem._db.execute("SELECT content FROM messages WHERE conv_id=? AND role='user' AND ts<=? AND ts>=? "
                              "ORDER BY ts DESC LIMIT 1", (conv, r["at"], r["at"] - QUESTION_WINDOW_S)).fetchone()
        if not msg:
            continue                     # no question of theirs to read (a background lookup): nothing to learn from
        hs = hits.get(r["id"]) or []
        out.append({"query_id": r["id"], "at": r["at"], "path": r["path"], "corrected": bool(r["corrected"]),
                    "question": msg[0], "shown": [h[0] for h in hs], "cited": [h[0] for h in hs if h[1]],
                    # #377 P2: which hits reached the model (stage "shown") and each hit's rank in the search
                    "reached": [h[0] for h in hs if h[2] == "shown"], "rank": {h[0]: h[3] for h in hs}})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The knowledge base's misses (#377); counts only")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("counts")
    ap.parse_args(argv)
    print(json.dumps(counts()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
