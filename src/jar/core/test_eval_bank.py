#!/usr/bin/env python3
"""eval_bank.py (#373): what a person stated or corrected becomes a (question, right answer) pair, kept as
theirs (owner and audience of the statement), used only for them, retired when the fact changes. Hermetic:
a temp world model and bank.

Run: python3 scripts/test_eval_bank.py
"""
# gate: hermetic
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-evalbank-test-"))
os.environ["WORLD_DIR"] = str(_TMP / "world")
os.environ["WORLD_DB"] = str(_TMP / "world.db")
sys.path.insert(0, str(Path(__file__).parent))
import eval_bank as eb  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


FOLKS = [{"id": "zach", "name": "Zach", "role": "admin", "parents": []},
         {"id": "robin", "name": "Robin", "role": "adult", "parents": []}]
B = W.Bubbles(_TMP / "world")
db = _TMP / "bank.db"
r1 = W.set_fact(B, "the vet", "phone", "555-0142", "robin", "adult", etype="place", people=FOLKS)
r2 = W.set_fact(B, "my gym", "phone", "555-0100", "zach", "admin", etype="place", people=FOLKS)
check("two facts stated", (r1[0], r2[0]), (True, True))
got = eb.harvest(B, db, now=100.0)
check("each stated fact is a pair", (got["added"], got["live"]), (2, 2))
z = eb.for_person("zach", db)
m = eb.for_person("robin", db)
check("Zach's pair: his question and answer", [(p["question"], p["answer"]) for p in z], [("What is my gym's phone?", "555-0100")])
check("Robin's is hers, with her statement's audience", ([p["answer"] for p in m], m[0]["owner"]), (["555-0142"], "robin"))
check("…and a person sees only their own", "555-0142" in str(z), False)
check("harvesting again adds nothing", eb.harvest(B, db, now=200.0)["added"], 0)
W.set_fact(B, "the vet", "phone", "555-0199", "robin", "adult", etype="place", people=FOLKS)
got = eb.harvest(B, db, now=300.0)
live = [p["answer"] for p in eb.for_person("robin", db)]
check("a corrected fact: the old pair is retired, the new value is examined", (live, got["retired"] >= 1 or got["changed"] >= 1),
      (["555-0199"], True))
con = sqlite3.connect(db)
check("the old answer is never examined again", con.execute("SELECT COUNT(*) FROM pairs WHERE answer='555-0142' AND retired_at IS NULL")
      .fetchone()[0], 0)
con.close()
check("the bank file is the owner's only", oct(os.stat(db).st_mode & 0o777), "0o600")
check("status is counts, never questions", set(eb.status(db)), {"live_by_person", "retired"})
check("questions in the world's words", (eb.question_for("Swim lessons", "activity", "schedule"),
      eb.question_for("Grandma", "person", "place")), ("When is Swim lessons?", "Where is Grandma?"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
