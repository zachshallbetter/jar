#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for retrieval_trace (titan-node#368): content-free rows, stages, citations, per-person reads,
retention, the weekly summary.

    python3 scripts/test_retrieval_trace.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DB = Path(tempfile.mkdtemp()) / "trace.db"
os.environ["RETRIEVAL_TRACE_DB"] = str(DB)
import retrieval_trace as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


def hit(sid, score, src="note"):
    return {"source_id": sid, "score": score, "chunk_text": "Ava's ballet is Thursdays at 4:30", "metadata": {"source": src}}


RAW = [hit("n1", 0.71), hit("runbook-1", 0.66, "kb_seed"), hit("n2", 0.58), hit("cx-mail-9", 0.44, "mail")]
T.begin()
T.query("passive", "When is Ava's ballet on Thursday?", RAW, RAW[:3])     # the floor dropped the mail
T.mark("dropped:system", [RAW[1]])
T.mark("shown", [RAW[0]])
T.mark("dropped:topic", RAW[:3])
T.query("search", "ballet studio address", [hit("n2", 0.62)], [hit("n2", 0.62)])
T.mark("shown", [hit("n2", 0.62)])
check("one turn, two searches written", T.end("t1", "ava", ["n1"]), 2)
r = T.rows("ava", "kid", 7)
check("…the passive search's stages", [(h["source_id"], h["stage"], h["cited"]) for h in r[-1]["hits"]],
      [("n1", "shown", 1), ("runbook-1", "dropped:system", 0), ("n2", "dropped:topic", 0), ("cx-mail-9", "dropped:floor", 0)])
check("…the search's", [(h["source_id"], h["stage"], h["cited"]) for h in r[0]["hits"]], [("n2", "shown", 0)])
con = sqlite3.connect(DB)
blob = " ".join(str(x) for row in con.execute("SELECT * FROM queries") for x in row) + \
    " ".join(str(x) for row in con.execute("SELECT * FROM hits") for x in row)
check("content-free: no words of the question or the notes are stored",
      any(w in blob.lower() for w in ("ballet", "thursday", "ava's", "studio")), False)
check("…a hash and a word count instead", [(len(q[0]), q[1]) for q in con.execute("SELECT qhash, qwords FROM queries ORDER BY id")],
      [(16, 6), (16, 3)])
check("a person sees only their own", T.rows("zach", "adult", 7), [])
check("…the admin sees theirs when asking", len(T.rows("zach", "admin", 7, person="ava")), 2)
check("…a shared device sees none", T.rows(None, "household", 7), [])
check("no turn begun: nothing recorded, nothing breaks", (T.query("search", "x", RAW, RAW), T.end("t2", "ava", [])), (None, 0))
T.begin()
T.query("search", "x", [hit("n2", 0.6)], [hit("n2", 0.6)])
T.mark("shown", [hit("n2", 0.6)])
T.end("t3", "zach", ["n2"])
s = T.summary(7)
check("weekly: shown and never cited", s["shown_never_cited"], [])
check("…dropped by a filter but cited elsewhere (a filter too eager)",
      [(d["stage"], d["source_id"]) for d in s["dropped_but_cited_elsewhere"]], [("dropped:topic", "n2")])
check("…per path", s["by_path"], {"passive": {"searches": 1, "shown": 1, "cited": 1}, "search": {"searches": 2, "shown": 2, "cited": 1}})
con.execute("UPDATE queries SET at=?", (time.time() - 40 * 86400,))
con.commit()
T.begin()
T.query("search", "y", [hit("n9", 0.6)], [])
T.end("t4", "zach", [])
check("older than 30 days: gone at the next write", con.execute("SELECT COUNT(*) FROM queries").fetchone()[0], 1)
check("the file is private to titan's account", oct(DB.stat().st_mode & 0o777), "0o600")

# the context search's hits are citable (#368 side finding): noted before the turn's record is reset
os.environ.setdefault("AGENT_TURN_SOURCE", "test")
os.environ.setdefault("HOUSEHOLD_DB", str(Path(tempfile.mkdtemp()) / "hh.db"))
os.environ.setdefault("KB_ACL_DB", str(Path(tempfile.mkdtemp()) / "acl.db"))
import agent_runtime_lib as A  # noqa: E402
A._TURN_INFO.info = {"_kb_hits": [{"source_id": "stale-from-last-turn"}]}
A.begin_turn_info()
A._note_sources([{"source_id": "n1", "title": "Ballet", "chunk_text": "Ballet is Thursdays", "metadata": {}}])
check("a fresh record per turn, holding only this turn's context hits", [h["source_id"] for h in A.carry_kb_hits()], ["n1"])

# #368: later corrections, linked to the searches behind them
T.begin(); T.query("search", "when is the dentist", [{"source_id": "n9", "score": 0.7}], [{"source_id": "n9", "score": 0.7}])
T.mark("shown", [{"source_id": "n9"}])
check("a turn whose own claim check corrected it is marked 1", T.end("t9", "robin", ["n9"], corrected=1), 1)
T.begin(); T.query("search", "vet phone", [{"source_id": "n8", "score": 0.7}], [{"source_id": "n8", "score": 0.7}])
T.mark("shown", [{"source_id": "n8"}])
T.end("t10", "robin", ["n8"])
check("the person correcting titan marks their latest turn's searches 2", T.mark_corrected("robin"), 1)
check("…only once", T.mark_corrected("robin"), 0)
check("…never someone else's", T.mark_corrected("leo"), 0)
sm = T.summary()
check("the weekly view lists what corrected turns cited", sorted(x["source_id"] for x in sm["cited_in_corrected_turns"]),
      ["n8", "n9"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
