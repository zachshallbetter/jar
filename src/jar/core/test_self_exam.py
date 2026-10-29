#!/usr/bin/env python3
"""self_exam.py (#373): only the admin's own pairs are asked, graded in code, recorded in the bank; the output
and log carry ids, kind and pass/fail, never the question, the answer or the reply. Hermetic: a temp bank and
a fake turn.

Run: python3 scripts/test_self_exam.py
"""
# gate: hermetic
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-selfexam-test-"))
os.environ["EVAL_BANK_DB"] = str(_TMP / "bank.db")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
sys.path.insert(0, str(Path(__file__).parent))
import eval_bank as eb  # noqa: E402
import self_exam as se  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


con = eb._db()
for ref, owner, q, a in [("world:zach:1", "zach", "What is my gym's phone?", "555-0100"),
                         ("world:zach:2", "zach", "When is Swim lessons?", "every Tuesday at 4"),
                         ("world:robin:3", "robin", "What is the vet's phone?", "555-0199")]:
    con.execute("INSERT INTO pairs (ref, owner, audience, source, question, answer, created_at) VALUES (?,?,?,?,?,?,?)",
                (ref, owner, "private", "world-statement", q, a, 1.0))
con.commit()
con.close()

asked: list = []
REPLIES = {"What is my gym's phone?": "Your gym's number is (555) 0100.", "When is Swim lessons?": "Swim lessons are on Mondays."}


def fake_ask(q):
    asked.append(q)
    return REPLIES[q]


res = se.exam("zach", fake_ask, now=10.0)
check("only Zach's own pairs are asked", sorted(asked), ["What is my gym's phone?", "When is Swim lessons?"])
check("graded in code: digits match a phone; a wrong day misses", sorted(r["ok"] for r in res), [False, True])
check("results are ids, kind and pass/fail only", all(set(r) == {"pair", "kind", "ok"} for r in res), True)
con = sqlite3.connect(_TMP / "bank.db")
check("recorded in the person's own bank", con.execute("SELECT SUM(asked), SUM(ok) FROM pairs WHERE owner='zach'").fetchone(), (2, 1))
check("Robin's pair is never asked", con.execute("SELECT asked FROM pairs WHERE owner='robin'").fetchone()[0], 0)
con.close()

se.LOG = _TMP / "self-exam.jsonl"
se.turn_eval_ask = fake_ask
out = io.StringIO()
with redirect_stdout(out):
    sys.argv = ["self_exam.py", "--source", "nightly"]
    se.main()
# Without the timestamp: a float like 1790964555.12 holds "555" by chance (a gate flake, 10-02).
text = out.getvalue() + "\n".join(json.dumps({k: v for k, v in json.loads(l).items() if k != "ts"})
                                   for l in se.LOG.read_text().splitlines() if l.strip())
check("no question, answer or reply in the output or the log",
      any(s in text for s in ("gym", "Swim", "555", "Tuesday", "Mondays", "phone")), False)
line = json.loads(se.LOG.read_text().splitlines()[-1])
check("the log line: counts and ids", (line["asked"], line["passed"], line["source"], line["person"]), (2, 1, "nightly", "zach"))

def boom(q):
    raise TimeoutError("slow")
check("an error is a miss, by kind only", se.exam("zach", boom, limit=1, now=20.0)[0]["error"], "TimeoutError")
check("passes(): words or digits", (se.passes("It's every Tuesday at 4.", "every Tuesday at 4"), se.passes("Call 555 0100", "555-0100"),
      se.passes("No idea", "555-0100")), (True, True, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
