#!/usr/bin/env python3
# gate: hermetic
"""The knowledge base's misses, collected per person (#377 half 1): a corrected lookup or an empty explicit search is
a miss; its question is the person's own message just before it, read from their own conversation; another person's
misses are never read for them; a background lookup with no question of theirs is skipped; counts carry no words.
P1's signals (the 10-10 design): kept nothing (an explicit search whose hits were all dropped), rephrase (an explicit
search that showed nothing, then the same person's differently worded lookup in another turn within 5 minutes, shown
and cited; a passive lookup never starts one) and KB-only corrected (2, or 1 with a cited hit: the calendar/reminder
claim check's 1 is left out), each counted for 7 and 14 days
with no person, qhash, turn id or word in counts() or the report line; the turn log's turn_id when the turn has one.
Fictional people (Alex, Robin, Jordan, Ava, Leo); a temporary trace, memory and turn log; no model.
Run: python3 scripts/test_knowledge_misses.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-misses-"))
os.environ["AGENT_MEMORY_DIR"] = str(_TMP / "memory")
os.environ["RETRIEVAL_TRACE_DB"] = str(_TMP / "trace.db")
os.environ["MEMORY_HYBRID"] = "0"
os.environ["GEMINI_API_KEY"] = "test-key-not-real"
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "ROUTINES_DB": "routines.db", "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import agent_memory  # noqa: E402
import household_identity as hh  # noqa: E402
import knowledge_misses as K  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ROBIN, JORDAN = "p_robin", "p_jordan"
NOW = time.time()
mem = agent_memory.AgentMemory()
con = sqlite3.connect(_TMP / "trace.db")
con.execute("CREATE TABLE queries (id INTEGER PRIMARY KEY, turn_id TEXT, at REAL, person TEXT, path TEXT, qhash TEXT, "
            "qwords INTEGER, corrected INTEGER DEFAULT 0)")
con.execute("CREATE TABLE hits (query INTEGER, rank INTEGER, source_id TEXT, source_type TEXT, score REAL, stage TEXT, "
            "cited INTEGER)")


def ask(person, text, at):
    mid = mem.add_message(hh.conv_id_for(person), "user", text)
    mem._db.execute("UPDATE messages SET ts=? WHERE id=?", (at, mid["id"] if isinstance(mid, dict) else mid))
    mem._db.commit()


def lookup(qid, person, at, path, corrected=0, hits=()):
    con.execute("INSERT INTO queries (id, at, person, path, corrected) VALUES (?,?,?,?,?)", (qid, at, person, path, corrected))
    for i, (sid, cited) in enumerate(hits):
        con.execute("INSERT INTO hits VALUES (?,?,?,?,?,?,?)", (qid, i, sid, "note", 0.5, "dense", cited))
    con.commit()


ask(ROBIN, "When does the pool open on Saturdays?", NOW - 3 * 86400)
lookup(1, ROBIN, NOW - 3 * 86400 + 4, "context", corrected=1, hits=[("note-gym", 1), ("note-bins", 0)])
ask(ROBIN, "Find the plumber's quote", NOW - 2 * 86400)
lookup(2, ROBIN, NOW - 2 * 86400 + 3, "search")                                  # explicit, nothing found
ask(ROBIN, "What's for dinner?", NOW - 86400)
lookup(3, ROBIN, NOW - 86400 + 2, "context", hits=[("note-meals", 1)])          # fine: not a miss
lookup(4, ROBIN, NOW - 3600, "search")                                          # a background search: no question
ask(JORDAN, "Where is the spare key?", NOW - 86400)
lookup(5, JORDAN, NOW - 86400 + 5, "context", corrected=1, hits=[("note-keys", 0)])
lookup(6, ROBIN, NOW - 20 * 86400, "search")                                    # too old for 14 days

got = K.collect(ROBIN, mem, now=NOW)
check("Robin's misses: the corrected lookup and the empty search, each with his own words; not the fine one, not the "
      "background search, not one older than 14 days",
      [(m["query_id"], m["corrected"], m["question"]) for m in got],
      [(1, True, "When does the pool open on Saturdays?"), (2, False, "Find the plumber's quote")])
check("what the corrected one showed and cited", (got[0]["shown"], got[0]["cited"]), (["note-gym", "note-bins"], ["note-gym"]))
check("Jordan's miss is never read for Robin, and Robin's never for Jordan",
      ([m["query_id"] for m in K.collect(JORDAN, mem, now=NOW)], any("spare key" in m["question"] for m in got)),
      ([5], False))
c = K.counts(now=NOW)
check("counts: numbers only, the household's; Jordan's corrected lookup cited nothing (a claim-check 1): not KB",
      (c[14], "pool" in json.dumps(c), ROBIN in json.dumps(c)),
      ({"misses": 4, "corrected": 2, "empty_searches": 2, "kept_nothing": 0, "rephrase": 0, "kb_corrected": 1},
       False, False))
check("no trace yet: nothing, no error", (K.collect(ROBIN, mem, trace_db=_TMP / "none.db"), K.counts(trace_db=_TMP / "none.db")),
      ([], {}))

print("\n── P1 signals: kept nothing, rephrase, KB-only corrected ──")
ALEX, AVA, LEO = "p_alex", "p_ava", "p_leo"
SIG = _TMP / "signals.db"
sc = sqlite3.connect(SIG)
sc.execute("CREATE TABLE queries (id INTEGER PRIMARY KEY, turn_id TEXT, at REAL, person TEXT, path TEXT, qhash TEXT, "
           "qwords INTEGER, corrected INTEGER DEFAULT 0)")
sc.execute("CREATE TABLE hits (query INTEGER, rank INTEGER, source_id TEXT, source_type TEXT, score REAL, stage TEXT, "
           "cited INTEGER)")
QHASHES, TURNS = set(), set()


def traced(qid, person, at, path, turn, qhash, hits=(), corrected=0):
    QHASHES.add(qhash)
    TURNS.add(turn)
    sc.execute("INSERT INTO queries (id, turn_id, at, person, path, qhash, qwords, corrected) VALUES (?,?,?,?,?,?,?,?)",
               (qid, turn, at, person, path, qhash, 5, corrected))
    for i, (sid, stage, cited) in enumerate(hits):
        sc.execute("INSERT INTO hits VALUES (?,?,?,?,?,?,?)", (qid, i + 1, sid, "note", 0.4, stage, cited))
    sc.commit()


H = 3600
T = NOW - 2 * 86400   # inside 7 days
OLD = NOW - 10 * 86400   # inside 14 days only
# kept nothing: explicit searches whose hits were all dropped
traced(101, ALEX, T, "search", "t-a1", "a1a1a1a1a1a1a1a1", [("n-1", "dropped:floor", 0), ("n-2", "dropped:topic", 0)])
traced(102, ALEX, T + 1 * H, "search-rerank", "t-a2", "a2a2a2a2a2a2a2a2", [("n-3", "dropped:system", 0)])
traced(103, ALEX, T + 2 * H, "search", "t-a3", "a3a3a3a3a3a3a3a3", [("n-4", "shown", 0), ("n-5", "dropped:floor", 0)])
traced(104, ALEX, T + 3 * H, "passive", "t-a4", "a4a4a4a4a4a4a4a4", [("n-6", "dropped:topic", 0)])   # passive: not
traced(105, ALEX, T + 4 * H, "search", "t-a5", "a5a5a5a5a5a5a5a5")                    # no hits: an empty search
traced(106, ALEX, OLD, "search", "t-a6", "a6a6a6a6a6a6a6a6", [("n-7", "dropped:floor", 0)])
# rephrase: Robin showed nothing, then asked again in other words within 5 minutes, shown and cited
traced(201, ROBIN, T + 5 * H, "search", "t-r1", "b1b1b1b1b1b1b1b1")
traced(202, ROBIN, T + 5 * H + 120, "search", "t-r2", "b2b2b2b2b2b2b2b2", [("n-8", "shown", 1)])
traced(203, ROBIN, OLD + H, "search", "t-r3", "b3b3b3b3b3b3b3b3")
traced(204, ROBIN, OLD + H + 200, "search", "t-r4", "b4b4b4b4b4b4b4b4", [("n-9", "shown", 1)])
traced(211, JORDAN, T + 6 * H, "search", "t-j1", "c1c1c1c1c1c1c1c1")                     # asked again too late
traced(212, JORDAN, T + 6 * H + 400, "passive", "t-j2", "c2c2c2c2c2c2c2c2", [("n-10", "shown", 1)])
traced(221, AVA, T + 7 * H, "search", "t-v1", "d1d1d1d1d1d1d1d1")                        # the same words again
traced(222, AVA, T + 7 * H + 60, "passive", "t-v2", "d1d1d1d1d1d1d1d1", [("n-11", "shown", 1)])
traced(231, LEO, T + 8 * H, "search", "t-l1", "e1e1e1e1e1e1e1e1")                        # the model's own search,
traced(232, LEO, T + 8 * H + 5, "search", "t-l1", "e2e2e2e2e2e2e2e2", [("n-12", "shown", 1)])   # the same turn
traced(241, ALEX, T + 9 * H, "search", "t-a7", "f1f1f1f1f1f1f1f1")                       # someone else found it
traced(242, ROBIN, T + 9 * H + 60, "passive", "t-r5", "f2f2f2f2f2f2f2f2", [("n-13", "shown", 1)])
traced(251, ROBIN, T + 10 * H, "search", "t-r6", "f3f3f3f3f3f3f3f3")                     # shown, never cited
traced(252, ROBIN, T + 10 * H + 60, "passive", "t-r7", "f4f4f4f4f4f4f4f4", [("n-14", "shown", 0)])
# a passive lookup that showed nothing ("hi"), then a new question's cited search in the next turn: conversation
traced(261, AVA, T + 15 * H, "passive", "t-v4", "h1h1h1h1h1h1h1h1", [("n-18", "dropped:topic", 0)])
traced(262, AVA, T + 15 * H + 60, "search", "t-v5", "h2h2h2h2h2h2h2h2", [("n-18", "shown", 1)])
# corrected: the person later (2); the turn's own check (1) with a KB hit cited; a calendar claim check's 1 is not
traced(301, JORDAN, T + 11 * H, "passive", "t-j3", "g1g1g1g1g1g1g1g1", [("n-15", "shown", 0)], corrected=2)
traced(302, JORDAN, T + 12 * H, "passive", "t-j4", "g2g2g2g2g2g2g2g2", [("n-16", "shown", 1)], corrected=1)
traced(303, AVA, T + 13 * H, "passive", "t-v3", "g3g3g3g3g3g3g3g3", corrected=1)
traced(304, LEO, T + 14 * H, "passive", "t-l2", "g4g4g4g4g4g4g4g4", [("n-17", "shown", 0)], corrected=1)

s = K.counts(trace_db=SIG, now=NOW)
pick = ("kept_nothing", "rephrase", "kb_corrected")
check("7 days: kept nothing 2 (a floor/topic drop, a system drop; not a passive drop, not an empty or shown search); "
      "rephrase 1 (Robin's search, asked again; not too late, the same words, the same turn, another person, never "
      "cited, nor a passive lookup that showed nothing before a new question's search); KB corrected 2",
      {k: s[7][k] for k in pick}, {"kept_nothing": 2, "rephrase": 1, "kb_corrected": 2})
check("14 days adds the older kept-nothing search and the older rephrase", {k: s[14][k] for k in pick},
      {"kept_nothing": 3, "rephrase": 2, "kb_corrected": 2})
check("the calendar/reminder claim check's corrections (1, nothing cited) are left out; the trace's raw corrected "
      "count still has them", (s[7]["kb_corrected"], s[7]["corrected"]), (2, 4))


def pair_count(first_path):
    """One pair alone: Leo's first lookup (nothing shown), then his next turn's cited search on other words."""
    db = _TMP / f"pair-{first_path}.db"
    pc = sqlite3.connect(db)
    pc.execute("CREATE TABLE queries (id INTEGER PRIMARY KEY, turn_id TEXT, at REAL, person TEXT, path TEXT, qhash TEXT, "
               "qwords INTEGER, corrected INTEGER DEFAULT 0)")
    pc.execute("CREATE TABLE hits (query INTEGER, rank INTEGER, source_id TEXT, source_type TEXT, score REAL, "
               "stage TEXT, cited INTEGER)")
    pc.execute("INSERT INTO queries VALUES (1, 't-1', ?, ?, ?, '0a0a0a0a0a0a0a0a', 2, 0)", (T, LEO, first_path))
    pc.execute("INSERT INTO hits VALUES (1, 1, 'n-20', 'note', 0.3, 'dropped:topic', 0)")
    pc.execute("INSERT INTO queries VALUES (2, 't-2', ?, ?, 'search', '0b0b0b0b0b0b0b0b', 6, 0)", (T + 90, LEO))
    pc.execute("INSERT INTO hits VALUES (2, 1, 'n-21', 'note', 0.7, 'shown', 1)")
    pc.commit()
    pc.close()
    return K.counts(trace_db=db, now=NOW)[7]["rephrase"]


check("a passive lookup that showed nothing, then the next turn's cited search: conversation, not a rephrase; "
      "an explicit search, then the next turn's cited search: a rephrase",
      {p: pair_count(p) for p in ("passive", "search", "search-rerank")},
      {"passive": 0, "search": 1, "search-rerank": 1})
blob = json.dumps(s)
check("counts carry no person, qhash or turn id: numbers only",
      ([p for p in (ALEX, ROBIN, JORDAN, AVA, LEO) if p in blob], [q for q in QHASHES if q in blob],
       [t for t in TURNS if t in blob],
       all(isinstance(v, int) for d in s.values() for v in d.values())), ([], [], [], True))

import agent_turn_report as rep  # noqa: E402
line = rep.signals_line(s)
check("the report's one line", line,
      ["knowledge-miss signals (7 d / 14 d): kept nothing 2/3, rephrased 1/2, KB-corrected lookups 2/2"])
check("the line carries no person, qhash or turn id",
      [x for x in (ALEX, ROBIN, JORDAN, AVA, LEO, *QHASHES, *TURNS) if x in line[0]], [])
check("no trace: no line", rep.signals_line(K.counts(trace_db=_TMP / "none.db")), [])

print("\n── the turn log's turn_id ──")
import agent_runtime_lib as lib  # noqa: E402


def logged_turn(turn_id):
    real_inner = lib._run_agent_turn_inner
    lib._run_agent_turn_inner = lambda messages, *a, **k: "A test answer."
    lib._TURN_IO.person, lib._TURN_IO.role, lib._TURN_IO.turn_id = {"id": ALEX, "name": "Alex"}, "adult", turn_id
    try:
        lib._run_agent_turn([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
    finally:
        lib._run_agent_turn_inner = real_inner
        lib._TURN_IO.person = lib._TURN_IO.role = lib._TURN_IO.turn_id = None
    return json.loads(Path(os.environ["AGENT_TURN_LOG"]).read_text().splitlines()[-1])


rec = logged_turn("4f0c1d2e-0000-4000-8000-00000000abcd")
check("a turn with an id: the log record names it", rec.get("turn_id"), "4f0c1d2e-0000-4000-8000-00000000abcd")
rec = logged_turn(None)
check("a turn without one (a background turn): no turn_id field", "turn_id" in rec, False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
