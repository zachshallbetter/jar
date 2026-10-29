#!/usr/bin/env python3
# gate: hermetic
"""The autonomous coder's merge rate in the weekly report (titan-node#1099): this period's proposals from its calibration
DB, read-only. Throwaway DB.
Run: python3 scripts/test_coder_merge_rate.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import agent_turn_report as rep  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r} expected={expected!r}"))


tmp = Path(tempfile.mkdtemp(prefix="titan-coder-rate-"))
db = tmp / "cal.db"
check("no DB: no line", rep.coder_line(7, db), None)
con = sqlite3.connect(db)
con.execute("""CREATE TABLE calibration (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, task_title TEXT NOT NULL,
               confidence_stated REAL NOT NULL, outcome TEXT NOT NULL DEFAULT 'pending', pr_number INTEGER, pr_url TEXT,
               created_at REAL NOT NULL, resolved_at REAL)""")
now = time.time()
for outcome, age_d in (("merged", 1), ("merged", 2), ("closed_unmerged", 3), ("opened", 1), ("pending", 0), ("merged", 30)):
    con.execute("INSERT INTO calibration (repo, task_title, confidence_stated, outcome, created_at) VALUES (?,?,?,?,?)",
                ("titan-node", "a task", 0.7, outcome, now - age_d * 86400))
con.commit()
con.close()
check("this week's proposals and how they ended; last month's left out",
      rep.coder_line(7, db, now), "autonomous coder (7 d): 5 proposed, 2 merged, 1 closed unmerged, 2 not yet resolved, "
      "merge rate 66% of resolved")
check("nothing this period: no line", rep.coder_line(0.0001, db, now - 10 * 86400), None)
src = (Path(__file__).parent / "agent_turn_report.py").read_text()
check("wired into the report, and never breaks it", ("line = coder_line(a.days)" in src, "coder merge rate skipped" in src),
      (True, True))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
