#!/usr/bin/env python3
"""titan-node#283, first slice: a task step can run later, one at a time, its result seen before the
next; a step that needs the person is never recorded as done; a daily card says what moved. Hermetic:
the workflow runtime, the step's model turn and the cards are faked."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = tempfile.mkdtemp()
os.environ["TASK_LOG_DB"] = str(Path(tmp) / "task_log.db")
import agent_runtime_lib as lib  # noqa: E402
import scheduled  # noqa: E402
import task_log  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


class Workflow:
    """Three steps, sequential, like workflow-runtime."""
    def __init__(self):
        self.steps = [{"step_key": f"s{i}", "title": t} for i, t in enumerate(["Shortlist venues", "Pick a date", "Send invites"])]
        self.cur, self.status, self.posted = 0, "running", []

    def __call__(self, rt, path, method="GET", body=None, **kw):
        if path.startswith("/v1/definitions/"):
            return {"steps": self.steps}
        if path.endswith("/steps") and method == "POST":
            self.posted.append(body)
            self.cur += 1
            if self.cur >= len(self.steps):
                self.status = "completed"
            return {}
        return {"status": self.status, "current_step_key": self.steps[min(self.cur, 2)]["step_key"],
                "definition_id": "d1", "input": {"goal": "Ava's party"}}


replies: list[str] = []
lib._run_agent_turn = lambda sub, **kw: replies.pop(0)
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach"}, "admin"


def run(max_steps="4", scheduled_id=None):
    wf = Workflow()
    lib._runtime_request = wf
    lib._TURN_IO.scheduled = scheduled_id
    try:
        return wf, lib.execute_tool("run_task", {"task_id": "task-1", "max_steps": max_steps})
    finally:
        lib._TURN_IO.scheduled = None


replies[:] = ["Looked at five.\nDONE: Shortlisted Fox Hall and Riverside.", "BLOCKED: You need to pick the date.",
              "DONE: sent"]
wf, out = run()
check("a done step is completed with its result", len(wf.posted) == 1 and wf.posted[0]["output"]["note"].startswith("Shortlisted"), str(wf.posted))
check("a step that needs the person is NOT completed, and the run stops there",
      len(wf.posted) == 1 and "⏸ Pick a date — needs you: You need to pick the date." in out and len(replies) == 1, out)
check("the reply says what's next", out.strip().endswith("Next: Pick a date"), out)

replies[:] = ["I can't call the venue for you; you'll need to.", "DONE: x"]
wf, out = run()
check("no marker but 'I can't…you'll need to': blocked, not done", not wf.posted and "needs you" in out, out)

replies[:] = ["DONE: one", "DONE: two", "DONE: three"]
wf, out = run(max_steps="4", scheduled_id="sa-1")
check("a scheduled run does exactly one step, whatever max_steps says", len(wf.posted) == 1 and len(replies) == 2, out)

replies[:] = ["DONE: a", "DONE: b", "DONE: c"]
wf, out = run(max_steps="4")
check("run now (not scheduled) still works through the steps", len(wf.posted) == 3 and "task is completed" in out, out)

# The log and the daily card.
moved = task_log.moved(0)["zach"]
check("done and blocked steps are logged with the task's goal",
      [m["state"] for m in moved].count("blocked") == 2 and all(m["goal"] == "Ava's party" for m in moved), str(moved)[:300])
title, detail = task_log.digest_card(moved)
check("the daily card counts what moved and what waits on you",
      title.startswith("Tasks today: ") and "waiting on you" in title and "“Ava's party”" in detail, f"{title} | {detail}")
sent = []
task_log.digest(lambda *a, **k: sent.append((a, k)))
check("one card per owner, to that owner, keyed by day",
      len(sent) == 1 and sent[0][1]["to"] == ["zach"] and sent[0][1]["key"].startswith("tasks-"), str(sent)[:300])
sent.clear()
task_log.digest(lambda *a, **k: sent.append((a, k)), now=10**10)
check("nothing moved: no card", not sent)

import sqlite3  # noqa: E402
import time as _time  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import local_time  # noqa: E402

# The household's day, not titan's UTC day (#1567): the daily card's key at 23:30 and 00:30 household time, across a
# UTC midnight (titan's clock says the 10th for both).
_real_time = _time.time
for _h, _m, _d, _want in ((23, 30, 9, "tasks-2026-10-09-ava"), (0, 30, 10, "tasks-2026-10-10-ava")):
    _at = datetime(2026, 10, _d, _h, _m, tzinfo=ZoneInfo("America/Los_Angeles"))
    _db = Path(tmp) / f"day-{_d}.db"
    _con = sqlite3.connect(_db)
    _con.execute("CREATE TABLE steps (task_id TEXT, goal TEXT, step TEXT, state TEXT, note TEXT, owner TEXT, ts REAL)")
    _con.execute("INSERT INTO steps VALUES ('t-day', ?, 'Book seats', 'done', '', 'ava', ?)",
                 ("Leo's recital", _at.timestamp() - 600))
    _con.commit()
    _con.close()
    _time.time = lambda at=_at: at.timestamp()
    _sent = []
    try:
        task_log.digest(lambda *a, **k: _sent.append(k), db=_db)
    finally:
        _time.time = _real_time
    check(f"the daily card key at {_h:02d}:{_m:02d} household time is the household's day",
          [k["key"] for k in _sent] == [_want], str([k["key"] for k in _sent]))

# The scheduled step's card.
cards = []
card = lambda r, title, detail, **kw: cards.append({"title": title, "detail": detail, **kw})  # noqa: E731
row = {"id": "sa-1", "owner": "zach", "what": "next step of Ava's party", "tool": "run_task",
       "args": {"task_id": "task-1"}, "run_at": 1.0}
scheduled._result_card(row, True, "Working on “Ava's party”: ✓ Shortlist venues — done (1 step run; task is running) Next: Pick a date", card)
check("a step done: its card offers the next step as one tap",
      cards[-1]["title"] == "Step done: next step of Ava's party"
      and cards[-1]["actions"][0] == {"label": "Next step", "kind": "prompt", "value": "Run the next step of task task-1"},
      str(cards[-1]))
scheduled._result_card(row, True, "Working on “Ava's party”: ⏸ Pick a date — needs you: pick the date (0 steps run; task is running)", card)
check("a step waiting on the person: urgent, and no Next step button",
      cards[-1]["title"].startswith("Waiting on you") and cards[-1]["urgency"] == 2
      and all(a["label"] != "Next step" for a in cards[-1]["actions"]), str(cards[-1]))
scheduled._result_card({**row, "tool": "get_weather"}, True, "Sunny", card)
check("other scheduled actions keep their plain Done card", cards[-1]["title"].startswith("Done: "))
check("schedule_action accepts run_task", scheduled.refusal("run_task", {"task_id": "t"}, "zach", "admin", check_mac=False) is None)

# 10-02 card: the step's title came back at the start of its own result.
T = "Read this week's calendar and timeline for Ava and Leo"
check("a result that repeats the step's title drops it",
      lib._without_step_title(("done", T + ": Ava has therapy, ballet."), T) == ("done", "Ava has therapy, ballet."))
check("…case and a trailing period don't matter",
      lib._without_step_title(("done", T.lower() + " — done."), T + ".") == ("done", "Done."))
check("a result that is only the title is kept",
      lib._without_step_title(("done", T), T) == ("done", T))
check("other results are untouched",
      lib._without_step_title(("blocked", "Pick a date"), T) == ("blocked", "Pick a date"))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all task-step checks passed'}")
sys.exit(1 if failures else 0)
