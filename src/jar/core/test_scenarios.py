#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass once #455 lands; touches nothing live)
"""#343 slice 1: a task's scenario, its step envelope and the consistency decision. Hermetic: temp store."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = Path(tempfile.mkdtemp())
os.environ["TASKS_DB"] = str(tmp / "tasks.db")
import scenarios as sc  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


check("off unless AGENT_TASK_AUTONOMY is set", not sc.enabled())
EV = "cx-calendar-party"
s = sc.create("task-1", "zach", "Ava's birthday party", cadence="daily", daily_cap=2,
              deps={"entities": ["zach:ava"], "items": [EV], "watch": {EV: ["start", "location"]}}, now=100.0)
check("a scenario holds the goal, owner, cadence and what it depends on",
      s["goal"] == "Ava's birthday party" and s["owner"] == "zach" and s["cadence"] == "daily"
      and s["deps"]["watch"][EV] == ["start", "location"], str(s))
check("the store is created 640", (os.stat(os.environ["TASKS_DB"]).st_mode & 0o777) == 0o640)
try:
    sc.create("t2", "zach", "x", cadence="hourly")
    check("an unknown cadence is refused", False)
except ValueError:
    check("an unknown cadence is refused", True)

C = sc.consistency
moved = {"item_id": EV, "change": "changed", "diff": {"start": ["2026-10-03T18:00", "2026-10-04T18:00"]}}
noted = {"item_id": EV, "change": "changed", "diff": {"title": ["Party", "Ava's party"]}}
other = {"item_id": "cx-mail-1", "change": "created", "diff": {"title": [None, "RSVP"]}}
gone = {"item_id": EV, "change": "removed", "diff": {}}
check("nothing changed: none", C(s, []) == ("none", []))
check("a watched field changed: conflicted (freeze)", C(s, [moved])[0] == "conflicted")
check("a dependency removed: conflicted", C(s, [gone])[0] == "conflicted")
check("an unwatched change: stale-safe (continue, disclose)", C(s, [noted, other]) == ("stale_safe", [noted, other]))
check("unreadable: unknown (stop)", C(s, None) == ("unknown", []))
check("conflicted names only the deciding change", C(s, [noted, moved])[1] == [moved])

e = sc.envelope(s, "s2", 1, grant_digest="sha256:" + "a" * 64, changes=[moved], current={EV: {"start": "x"}}, now=200.0)
check("the envelope carries task, step, owner, attempt, grant and context digests, since and the decision",
      e["task_id"] == "task-1" and e["step_key"] == "s2" and e["owner"] == "zach" and e["attempt"] == 1
      and e["grant_digest"].startswith("sha256:") and e["context_sha256"].startswith("sha256:")
      and e["consistency"] == "conflicted" and e["deciding"] == [moved])
e2 = sc.envelope(s, "s2", 1, grant_digest=None, changes=[], current={EV: {"start": "y"}}, now=200.0)
check("a different current state is a different context digest", e2["context_sha256"] != e["context_sha256"])

h = sc.halt("Ava's party moved to Sunday", ["s1 Shortlist venues"], ["s2 Book the venue", "s3 Send invites"], "zach")
d = sc.record(e, h)
check("the receipt is digested and kept", d.startswith("sha256:"))
check("the same blocker under the same context isn't retried", sc.same_blocker("task-1", "Ava's party moved to Sunday",
                                                                              e["context_sha256"]))
check("a changed context may try again", not sc.same_blocker("task-1", "Ava's party moved to Sunday", e2["context_sha256"]))

import json, sqlite3  # noqa: E401,E402
stored = json.loads(sqlite3.connect(os.environ["TASKS_DB"]).execute("SELECT envelope FROM envelopes").fetchone()[0])
check("the receipt is content-free: ids, fields named, digests, never values",
      stored["changes"] == [{"dep": EV, "change": "changed", "fields": ["start"], "at": None}]
      and "2026-10-04T18:00" not in json.dumps(stored), str(stored["changes"]))

DAY1, DAY2 = 1_790_900_000.0, 1_790_900_000.0 + 86400
check("the daily cap allows the first steps", sc.can_step_today("task-1", 2, now=DAY1))
sc.count_step("task-1", now=DAY1)
sc.count_step("task-1", now=DAY1 + 60)
check("then stops for the day", not sc.can_step_today("task-1", 2, now=DAY1 + 120))
check("and a new day starts fresh (a burst never spends the month)", sc.can_step_today("task-1", 2, now=DAY2))

# 2026-10-02 06:00 UTC is still Oct 1 in the household (11 PM PT): the same day's cap.
check("the day is the household's, not UTC's", sc._day(1_790_920_800.0) == "2026-10-01")

for i in range(2):
    sc.record({**e, "at": 400.0 + i}, h)
check("three halts in a row on the same blocker stop the task", sc.should_stop("task-1"))
sc.record({**e2, "at": 500.0}, {"result": "done"})
check("a step that went through resets it", not sc.should_stop("task-1"))

sc.update("task-1", state="waiting")
sc.answer("task-1", "Sunday is fine, keep going", "s2", now=300.0)
s = sc.get("task-1")
check("an answer is kept in the scenario and the task resumes", s["state"] == "active"
      and s["answers"][-1]["text"] == "Sunday is fine, keep going")
try:
    sc.update("task-1", goal="x")
    check("the goal and owner can't be changed by update", False)
except ValueError:
    check("the goal and owner can't be changed by update", True)
check("a since block reads the changes", sc.since_block([noted], lambda cs: "calendar: “Ava's party” renamed")
      .startswith("Since the last step:\ncalendar"))
check("no changes, no block", sc.since_block([], str) == "")

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all scenario checks passed'}")
sys.exit(1 if failures else 0)
