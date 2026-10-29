#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for shadow_replay (titan-node#367): pairing traced searches with the person's own message,
per-person visibility, judging with a random order, counts only.

    python3 scripts/test_shadow_replay.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = Path(tempfile.mkdtemp())
os.environ.update({"RETRIEVAL_TRACE_DB": str(_TMP / "t.db"), "HOUSEHOLD_DB": str(_TMP / "h.db"),
                   "KB_ACL_DB": str(_TMP / "a.db")})
import retrieval_trace as T  # noqa: E402
import shadow_replay as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


now = time.time()
mem = _TMP / "mem.db"
con = sqlite3.connect(mem)
con.execute("CREATE TABLE messages (id TEXT, conv_id TEXT, role TEXT, content TEXT, ts REAL)")
con.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", [
    ("1", "chat", "user", "How does a new phone pair with titan?", now - 100),
    ("2", "chat", "assistant", "…", now - 95),
    ("3", "chat:p_m", "user", "Where is the ballet studio?", now - 50),
    ("4", "chat", "user", "an old question", now - 9 * 86400)])
con.commit()
for tid, person, at in (("t1", "zach", now - 98), ("t2", "p_m", now - 48), ("t3", "zach", now - 9 * 86400 + 2)):
    T.begin()
    T.query("search", "x", [], [])
    T.end(tid, person, [])
tc = sqlite3.connect(os.environ["RETRIEVAL_TRACE_DB"])
for tid, at in (("t1", now - 98), ("t2", now - 48), ("t3", now - 9 * 86400 + 2)):
    tc.execute("UPDATE queries SET at=? WHERE turn_id=?", (at, tid))
tc.commit()
qs = S.questions(7, memory_db=mem)
check("each traced search paired with its person's own message, within the window",
      sorted((q["person"], q["text"]) for q in qs), [("p_m", "Where is the ballet studio?"),
                                                    ("zach", "How does a new phone pair with titan?")])
seen_as = []
base = lambda q, p, r: (seen_as.append((p, r)), [{"source_id": "news"}, {"source_id": "pair"}])[1]
cand = lambda q, p, r: [{"source_id": "pair"}] if "phone" in q else [{"source_id": "news"}, {"source_id": "pair"}]
prompts = []


def judge(prompt):
    prompts.append(prompt)
    first = prompt.split("SET 1:")[1].split("SET 2:")[0]
    return json.dumps({"better": 1 if "news" not in first else 2})   # prefers the set without the newsletter


c = S.compare(qs, base, cand, {"zach": "admin", "p_m": "adult"}, judge)
check("counts: one identical, one differing, judged better for the candidate",
      {k: c[k] for k in ("questions", "identical", "differ", "better", "worse")},
      {"questions": 2, "identical": 1, "differ": 1, "better": 1, "worse": 0})
check("…run as each person, with their role", sorted(seen_as), [("p_m", "adult"), ("zach", "admin")])
check("…the verdict", c["verdict"], "candidate holds")
check("the report carries counts only, no question text", "phone" in json.dumps(c) or "ballet" in json.dumps(c), False)
c2 = S.compare(qs, base, cand, {"zach": "admin", "p_m": "adult"}, None)
check("without a judge: differences counted, unjudged", (c2["differ"], c2["unjudged"]), (1, 1))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
