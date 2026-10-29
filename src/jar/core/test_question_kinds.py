#!/usr/bin/env python3
"""question_kinds.py (#378): questions sorted into kinds by keyword rules, joined to the turn that answered
them, reported as counts only. Hermetic: a temp messages table and turn log.

Run: python3 scripts/test_question_kinds.py
"""
# gate: hermetic
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import question_kinds as qk  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


for q, k in [("What's on my calendar tomorrow?", "calendar"), ("Remind me to call the vet", "reminders"),
             ("Remember the wifi password is otter-77", "remember"), ("Which bills are due?", "bills"),
             ("When should I leave for the dentist?", "travel"), ("What did Robin text me?", "messages"),
             ("Turn off the living room lights", "home"), ("Is titan's GPU busy?", "titan"), ("Tell me a joke", "other")]:
    check(f"kind: {q}", qk.kind_of(q), k)

T = Path(tempfile.mkdtemp(prefix="titan-kinds-test-"))
db = T / "titan_memory.db"
con = sqlite3.connect(db)
con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT, ts REAL)")
now = time.time()
SECRET = "the alarm code is 4321"
rows = [("chat", "user", "What's on my calendar tomorrow?", now - 3600), ("chat", "assistant", SECRET, now - 3590),
        ("chat:robin", "user", "What's on Saturday? " + SECRET, now - 1800),
        ("chat", "user", "Tell me a joke", now - 600), ("chat", "user", "old one", now - 30 * 86400)]
con.executemany("INSERT INTO messages (conv_id, role, content, ts) VALUES (?,?,?,?)", rows)
con.commit()
con.close()
tl = T / "turns.jsonl"
tl.write_text("\n".join(json.dumps(r) for r in [
    {"ts": now - 3550, "source": "user", "tool_names": ["list_calendar_events"]},
    {"ts": now - 1750, "source": "user", "tool_names": []},
    {"ts": now - 1700, "source": "eval", "tool_names": ["list_calendar_events"]},
    {"ts": now - 560, "source": "user", "tool_names": ["web_search"]}]) + "\n")
qs = qk.questions(db, now - 7 * 86400)
check("user messages in the window only", len(qs), 3)
rep = qk.report(qs, qk.turns(tl, now - 7 * 86400), 7)
check("calendar: two asked, one read the calendar, one answered without a tool",
      (rep["calendar"]["asked"], rep["calendar"]["no_tool_pct"], rep["calendar"]["structured_pct"]), (2, 50, 50))
check("an eval turn never joins", rep["calendar"]["joined"], 2)
check("a web search is a prose answer", rep["other"]["prose_pct"], 100)
out = io.StringIO()
with redirect_stdout(out):
    sys.argv = ["question_kinds.py", "--db", str(db), "--turn-log", str(tl), "--json"]
    qk.main()
check("no question or reply text in the output", any(s in out.getvalue() for s in (SECRET, "Saturday", "joke")), False)

# "other" split by titan's local model (#378): a labeller is only ever asked about "other" questions, and its answer
# must be one of the fixed labels
seen = []
qs2 = qk.questions(db, now - 7 * 86400, split_other=lambda t: seen.append(t) or "entertainment")
check("other becomes other:<label>", sorted(k for _, _, k in qs2), ["calendar", "calendar", "other:entertainment"])
check("the labeller only sees the 'other' questions", len(seen), 1)
check("an off-list label from the model counts as unclear", qk.OTHER_LABELS[-1], "unclear")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
