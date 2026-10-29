#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for kb_rerank and kb_retrieve's reranked path (titan-node#369): off by default, ordering by
the reranker, its floor replacing the score floor, the fallback when it's unreachable, the trace's rerank score.

    python3 scripts/test_kb_rerank.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp()
os.environ.update({"RETRIEVAL_TRACE_DB": str(Path(_TMP) / "t.db"), "HOUSEHOLD_DB": str(Path(_TMP) / "h.db"),
                   "KB_ACL_DB": str(Path(_TMP) / "a.db"), "AGENT_TURN_SOURCE": "test"})
os.environ.pop("KB_RERANK", None)
import kb_rerank as R  # noqa: E402
import kb_privacy  # noqa: E402
import agent_runtime_lib as A  # noqa: E402
import retrieval_trace as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


check("off unless KB_RERANK=1", R.enabled(), False)
check("the measured floor", R.floor(), -2.8)
HITS = [{"source_id": "news", "title": "Newsletter", "chunk_text": "deals", "score": 0.61},
        {"source_id": "pair", "title": "Pairing", "chunk_text": "pair a new phone with titan", "score": 0.48},
        {"source_id": "low", "title": "Old", "chunk_text": "pairing notes from 2024", "score": 0.30}]
SC = {"news": -6.0, "pair": 2.5, "low": -1.0}
kb_privacy.query = lambda call, q, k, p, r, timeout=30: list(HITS)
R.rerank = lambda q, hits, timeout=8: sorted([{**h, "rerank_score": SC[h["source_id"]]} for h in hits],
                                            key=lambda h: -h["rerank_score"])
T.begin()
got = A._kb_query_reranked("How does a new phone pair with titan?", 5)
check("reranked: best first; the reranker's floor drops the newsletter and keeps a low-retrieval-score answer",
      [h["source_id"] for h in got], ["pair", "low"])
T.end("t1", "zach", ["pair"])
con = sqlite3.connect(os.environ["RETRIEVAL_TRACE_DB"])
check("the trace keeps each hit's rerank score, on its own path",
      sorted(con.execute("SELECT q.path, h.source_id, h.rerank, h.stage FROM hits h JOIN queries q ON q.id=h.query")),
      [("search-rerank", "low", -1.0, "kept"), ("search-rerank", "news", -6.0, "dropped:floor"),
       ("search-rerank", "pair", 2.5, "kept")])


def down(q, hits, timeout=8):
    raise OSError("connection refused")


R.rerank = down
A._kb_query = lambda q, k, timeout=30, passive=False: [HITS[0]]
check("the reranker unreachable: today's path, the search still answers",
      [h["source_id"] for h in A._kb_query_reranked("x", 5)], ["news"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
