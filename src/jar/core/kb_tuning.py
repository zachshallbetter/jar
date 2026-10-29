#!/usr/bin/env python3
"""Knowledge-base tuning from the retrieval trace (titan-node#370, on #368's trace).

Usage labels: per source, how often it was put in front of the model (shown) and how often a reply drew on it
(cited), each decaying with a half-life so old behaviour fades. (A capped ranking penalty for never-cited "noise
magnets", KB_USAGE_PRIOR, tied the ranking on the KB bank and was removed, #945: 122 cases, hit@1 88 = 88, hit@3
106 = 106, MRR 0.7961 → 0.7948.) Thresholds aren't changed here at all: `sweep` proposes a score
floor and a top-k per source type from the trace, as a report; a person adopts a value through a reviewed
PR, like SkillOpt.

  python3 scripts/kb_tuning.py usage   [--days 30]     per-source labels (ids and counts only)
  python3 scripts/kb_tuning.py sweep   [--days 14]     proposed floors and top-k, per source type
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

HALF_LIFE_DAYS = 14.0


def _db(path: Path | None = None) -> sqlite3.Connection:
    import retrieval_trace
    return retrieval_trace._db(path)


def usage(days: float = 30, path: Path | None = None, now: float | None = None) -> dict[str, dict[str, float]]:
    """source_id → {shown, cited} decayed (half-life HALF_LIFE_DAYS), from the last `days` of trace."""
    now = now or time.time()
    con = _db(path)
    try:
        out: dict[str, dict[str, float]] = {}
        for sid, at, stage, cited in con.execute(
                "SELECT h.source_id, q.at, h.stage, h.cited FROM hits h JOIN queries q ON q.id=h.query WHERE q.at >= ?",
                (now - days * 86400,)):
            w = 0.5 ** (max(0.0, now - at) / 86400 / HALF_LIFE_DAYS)
            u = out.setdefault(sid, {"shown": 0.0, "cited": 0.0})
            if stage == "shown":
                u["shown"] += w
            if cited:
                u["cited"] += w
        return out
    finally:
        con.close()


def sweep(days: float = 14, path: Path | None = None) -> dict[str, Any]:
    """Per source type, from searches whose hits a reply cited: the lowest score a cited hit had (a floor above
    it would have lost an answer), how many shown hits sat below it, and the deepest rank a cited hit reached
    (top-k). Proposals only: nothing is applied."""
    con = _db(path)
    try:
        since = time.time() - days * 86400
        rows = con.execute("SELECT h.source_type, h.score, h.rank, h.stage, h.cited, q.path FROM hits h "
                           "JOIN queries q ON q.id=h.query WHERE q.at >= ? AND h.score IS NOT NULL", (since,)).fetchall()
    finally:
        con.close()
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for st, score, rank, stage, cited, qpath in rows:
        b = by.setdefault((qpath, st or "?"), {"cited_scores": [], "uncited_shown": [], "dropped_floor": 0, "ranks": []})
        if cited:
            b["cited_scores"].append(score)
            b["ranks"].append(rank)
        elif stage == "shown":
            b["uncited_shown"].append(score)
        if stage == "dropped:floor":
            b["dropped_floor"] += 1
    out = {}
    for (qpath, st), b in sorted(by.items()):
        if not b["cited_scores"]:
            out[f"{qpath}/{st}"] = {"cited": 0, "shown_uncited": len(b["uncited_shown"]), "proposal": None,
                                    "note": "never cited in the window: no floor can be learned yet"}
            continue
        floor = round(min(b["cited_scores"]) - 0.02, 2)
        out[f"{qpath}/{st}"] = {
            "cited": len(b["cited_scores"]), "shown_uncited": len(b["uncited_shown"]),
            "uncited_below_proposed_floor": sum(s < floor for s in b["uncited_shown"]),
            "dropped_by_floor": b["dropped_floor"],
            "proposal": {"floor": floor, "top_k": max(b["ranks"])}}
    return {"days": days, "by_path_and_type": out,
            "current": {"passive_floor": float(os.environ.get("KB_MIN_SCORE", "0.5")),
                        "search_floor": float(os.environ.get("KB_MIN_SCORE_SEARCH", "0.35"))},
            "how_to_adopt": "a reviewed PR changing KB_MIN_SCORE / KB_MIN_SCORE_SEARCH (or per-type floors), with "
                            "retrieval_eval before and after"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("usage")
    u.add_argument("--days", type=float, default=30)
    s = sub.add_parser("sweep")
    s.add_argument("--days", type=float, default=14)
    a = ap.parse_args()
    if a.cmd == "usage":
        us = usage(a.days)
        rows = sorted(({"source_id": k, **{x: round(y, 2) for x, y in v.items()}} for k, v in us.items()),
                      key=lambda r: (-r["shown"], r["source_id"]))
        print(json.dumps(rows[:40], indent=2))
    else:
        print(json.dumps(sweep(a.days), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
