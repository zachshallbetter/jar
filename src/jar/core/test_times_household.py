#!/usr/bin/env python3
# gate: hermetic
"""Times a person reads are household time with its zone, not titan's UTC (#1568): the correction digest's entries,
a todo's GitHub issue body, the turn review's title and file name, a kept voice note's title. Frozen at 23:30 on
Oct 11 2026 in Los Angeles, when UTC is already Oct 12. Also: stamps with a trailing Z in backup file names are
read as UTC by backup_retention, old bare stamps still work, and backup_store prunes both. Made-up people."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ["AGENT_TZ"] = "America/Los_Angeles"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backup_retention as R  # noqa: E402
import backup_store as BS  # noqa: E402
import correction_digest as cd  # noqa: E402
import local_time  # noqa: E402
import todo_issues as ti  # noqa: E402
import turn_review as tr  # noqa: E402
import voice_notes as vn  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


# 23:30 PDT on Sunday Oct 11 2026 is 06:30 UTC on Oct 12.
NOW = datetime(2026, 10, 12, 6, 30, tzinfo=timezone.utc).timestamp()
time.time = lambda: NOW                       # frozen: the whole file runs at this instant
local_time.now = lambda name=None: datetime.fromtimestamp(NOW, local_time.zone(name))

c = cd.Correction(ts=NOW - 60, asked="when is the dentist", reply="Tuesday", correction="no, Thursday, Alex said so",
                  why="pattern", what_went_wrong="wrong day")
entry = "\n".join(cd._entries([c]))
check("correction digest entry is Pacific, with its zone", "### Sun 10-11 23:29 PDT: wrong day" in entry, True)
check("…and not the UTC day", "10-12" in entry, False)

body = ti.issue_body({"id": "t1", "title": "Fix the porch light", "detail": "Robin asked", "created_at": NOW - 60,
                      "created_by": "Robin"}, "titan-node")
check("todo issue body says household time with zone", "on 2026-10-11 23:29 PDT" in body, True)

empty = {"turns": 0, "p50": 0, "p95": 0, "brains": __import__("collections").Counter(), "errors": {}, "fallbacks": {},
         "budget_hits": 0, "slow": [], "slow_tools": {}}
ws = {k: [] for k in ("corrections", "repeats", "claim_corrections", "promises", "raw_errors", "tool_names")}
mem = {"sampled": 0, "filtered": [], "duplicate": [], "confirm": [], "others": 0}
md = tr.render(7, empty, ws, mem)
check("turn review title is the household's date", md.splitlines()[0], "# Real-turn review, last 7 days (draft, 2026-10-11)")
check("turn review week key is the household's date", local_time.day(time.time()), "2026-10-11")

kept = []
ok, _ = vn.keep({"id": "c1", "detail": "hello", "from": "Ava", "created_at": NOW - 60}, "jordan", "adult",
                index=lambda *a, **k: kept.append(a), now=NOW)
check("voice note kept", ok, True)
check("voice note title is the household's day", kept[0][1], "Voice note from Ava, Oct 11, 2026")
check("…while its machine stamp stays UTC with Z", kept[0][4]["observed_at"], "2026-10-12T06:29:00Z")

# backup stamps: a trailing Z is UTC, a bare one is local-or-UTC as before
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    now = NOW
    def stamp_utc(age_days):
        return time.strftime("%Y%m%d-%H%M%S", time.gmtime(now - age_days * 86400))
    new = d / f"kb-lifecycle-{stamp_utc(40)}Z.jsonl"
    old = d / f"kb-lifecycle-{stamp_utc(40)}.jsonl"
    for p in (new, old):
        p.write_text("x")
        os.utime(p, (now - 50 * 86400, now - 50 * 86400))
    check("age of a Z-stamped name, in UTC", round(R.age_s(new, now) / 3600), 40 * 24)
    check("age of an old bare name still parses", abs(R.age_s(old, now) / 3600 - 40 * 24) <= 24, True)
    got = R.prune(d, "kb-lifecycle-*.jsonl", now=now, apply=False)
    check("both name styles come due", sorted(got), sorted([new.name, old.name]))

    runs = d / "runs"
    runs.mkdir()
    for n in ("20261001-120000", "20261002-120000Z", "20261003-120000Z"):
        (runs / n).mkdir()
    (runs / "not-a-run").mkdir()
    gone = BS.prune(runs, 2)
    check("backup_store prunes the oldest across old and new names", [p.name for p in gone], ["20261001-120000"])
    check("a new run's folder name is UTC with Z",
          BS.STAMP_RX.match(time.strftime("%Y%m%d-%H%M%SZ", time.gmtime(NOW))) is not None, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
