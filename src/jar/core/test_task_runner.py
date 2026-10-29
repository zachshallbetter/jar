#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#343 slice 2: the opt-in and the runner. Hermetic: temp store; steps, cards, changes and grants are faked."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = Path(tempfile.mkdtemp())
os.environ["TASKS_DB"] = str(tmp / "tasks.db")
os.environ.pop("AGENT_TASK_AUTONOMY", None)
import scenarios as sc  # noqa: E402
import task_runner as tr  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


PT = ZoneInfo("America/Los_Angeles")
def at(h, m=0, day=2):
    return datetime(2026, 10, day, h, m, tzinfo=PT).timestamp()


grants_issued, cards, steps = [], [], []
def issue(owner, subject, now):
    grants_issued.append((owner, subject))
    return {"id": "gr-1"}


def resolve(names):
    return tr.resolve_deps(names, "zach", "admin", world_find=lambda n: ["zach:ava"] if "ava" in n.lower() else [],
                           item_find=lambda n: ["cx-party"] if "party" in n.lower() else [])


def card(title, detail, **k):
    cards.append({"title": title, "detail": detail, **k})


SECRET = "Robin's text: meet at the back door at 5"
step_result = {"text": "Working on “Ava's party”:\n✓ Shortlist venues — " + SECRET + "\n(1 step run; task is running)\nNext: Book"}
def execute_step(s, ctx, person):
    steps.append(ctx)
    return step_result["text"]


change_rows = {"rows": []}
def changes_fn(since, item_ids=None, person=None, role=None):
    if change_rows["rows"] is None:
        raise RuntimeError("unreadable")
    return [r for r in change_rows["rows"] if r.get("item_id") in (item_ids or [])]


grant_ok = {"why": None}
RUN = dict(execute_step=execute_step, card=card, changes_fn=changes_fn, world_fn=lambda ids, since, p, r: [],
           describe=lambda cs: "; ".join(f"{c['item_id']} {sorted((c.get('diff') or {}))}" for c in cs),
           grant_use=lambda gid, owner, tid: grant_ok["why"], person_of=lambda o: {"id": o, "role": "admin"})

# Off by default.
check("off: opt-in refused", "switched off" in tr.opt_in("t1", "zach", "admin", "Ava's party", "daily", issue_grant=issue,
                                                          resolve=resolve))
check("off: the runner does nothing", tr.run_due(at(10), **RUN) == [])
os.environ["AGENT_TASK_AUTONOMY"] = "1"

msg = tr.opt_in("t1", "zach", "admin", "Ava's party", "daily", daily_cap=1, depends_on=["Ava's party", "Ava"],
                issue_grant=issue, resolve=resolve, now=at(8))
s = sc.get("t1")
check("opt-in: scenario, deps resolved through the owner's view, a titan task_step grant",
      s["cadence"] == "daily" and s["deps"]["items"] == ["cx-party"] and s["deps"]["entities"] == ["zach:ava"]
      and grants_issued == [("zach", ("task_step", "task:t1"))] and s["grant_id"] == "gr-1", str(s))
check("daily: next step at 9 household time", s["next_due"] == at(9))
check("the reply says never at night and how to stop", "never at night" in msg and "stop the task" in msg)
check("quiet hours: no step, no card", tr.run_due(at(23), **RUN) == [] and tr.run_due(at(6, 59, day=3), **RUN) == [] and not cards)

# A step: content-free receipt (a tool result never lands in the store).
out = tr.run_due(at(9, 1), **RUN)
check("due: one step, a card to the owner", out == [{"task_id": "t1", "status": "advanced"}] and cards[-1]["owner"] == "zach"
      and cards[-1]["key"] == "task-t1", str(out))
dump = " ".join(r[0] for r in sqlite3.connect(os.environ["TASKS_DB"]).execute("SELECT envelope FROM envelopes"))
check("a tool result never lands in the receipts", SECRET not in dump and "back door" not in dump, dump[:300])
rec = json.loads(dump)
check("the receipt holds status, the owner's step names and counts", rec["outcome"] == {"status": "advanced",
      "steps": ["Shortlist venues"], "changes_disclosed": 0}, str(rec["outcome"]))
check("next daily step tomorrow at 9", sc.get("t1")["next_due"] == at(9, day=3))
check("the cap is per day: not again today", tr.run_due(at(9, 30), **RUN) == [])

# Stale-safe: an unwatched change is disclosed and the step runs.
change_rows["rows"] = [{"item_id": "cx-party", "change": "changed", "diff": {"title": ["Party", "Ava's party"]}, "at": at(10)}]
cards.clear(); steps.clear()
out = tr.run_due(at(9, 1, day=3), **RUN)
check("stale-safe: the step runs and the card opens with what changed", out[0]["status"] == "advanced"
      and cards[-1]["detail"].startswith("Since the last step:") and "Since the last step" in steps[-1], str(cards[-1]))

# Conflicted: a watched field changed → no step, waiting, a card naming it, Continue / Stop.
change_rows["rows"] = [{"item_id": "cx-party", "change": "changed", "diff": {"start": ["Sat", "Sun"]}, "at": at(10, day=3)}]
cards.clear(); steps.clear()
out = tr.run_due(at(9, 1, day=4), **RUN)
check("conflicted: no step, waiting, the change named, Continue and Stop", out == [{"task_id": "t1", "status": "waiting",
      "reason": "dependency_changed"}] and not steps and sc.get("t1")["state"] == "waiting" and "start" in cards[-1]["detail"]
      and [a["label"] for a in cards[-1]["actions"]] == ["Continue", "Stop"] and cards[-1]["urgency"] == 2, str(cards[-1:]))
sc.answer("t1", "Sunday is fine, keep going")
sc.update("t1", next_due=at(9, day=5))
change_rows["rows"] = []
cards.clear(); steps.clear()
out = tr.run_due(at(9, 1, day=5), **RUN)
check("an answer resumes it, and the step sees the answer", out[0]["status"] == "advanced" and "Sunday is fine" in steps[-1])

# Unknown: the dependencies can't be read → waiting, worded from the owner's own plan.
change_rows["rows"] = None
cards.clear()
sc.update("t1", next_due=at(9, day=6))
out = tr.run_due(at(9, 1, day=6), **RUN)
check("unknown: waiting, 'I can't read something this task depends on', never whose", out[0]["status"] == "waiting"
      and "can't read something this task depends on" in cards[-1]["detail"]
      and "fence" not in cards[-1]["detail"].lower() and "robin" not in cards[-1]["detail"].lower())

# A blocked step waits; a finished task is done.
change_rows["rows"] = []
sc.update("t1", state="active", next_due=at(9, day=7))
step_result["text"] = "Working on “Ava's party”:\n⏸ Book the venue — needs you: pick the date\n(0 steps run; task is running)"
out = tr.run_due(at(9, 1, day=7), **RUN)
check("a step that needs the person: waiting, urgent", out[0]["status"] == "blocked" and sc.get("t1")["state"] == "waiting"
      and cards[-1]["urgency"] == 2)
sc.update("t1", state="active", next_due=at(9, day=8))
step_result["text"] = "Working on “Ava's party”:\n✓ Send invites — sent\n(1 step run; task is completed)"
out = tr.run_due(at(9, 1, day=8), **RUN)
check("the last step: done, no next", out[0]["status"] == "done" and sc.get("t1")["state"] == "done" and sc.get("t1")["next_due"] is None)

# A grant that no longer covers it stops the task.
tr.opt_in("t2", "zach", "admin", "Taxes", "after_each", issue_grant=issue, resolve=resolve, now=at(9, day=9))
grant_ok["why"] = "the grant was revoked"
out = tr.run_due(at(9, 1, day=9), **RUN)
check("a revoked grant stops it, with one card", out == [{"task_id": "t2", "status": "stopped", "reason": "grant"}]
      and sc.get("t2")["state"] == "stopped")
grant_ok["why"] = None

# Three halts in a row on the same blocker stop it.
tr.opt_in("t3", "zach", "admin", "Car registration", "after_each", depends_on=["party"], issue_grant=issue, resolve=resolve,
          now=at(9, day=10))
for i in range(3):
    e = sc.envelope(sc.get("t3"), "next", 1, grant_digest="gr-1", changes=[], now=at(9, day=10) + i)
    sc.record(e, sc.halt("step_needs_you", [], [], "zach"))
cards.clear()
out = tr.run_due(at(9, 1, day=10), **RUN)
check("three halts in a row: stopped, one card", out == [{"task_id": "t3", "status": "stopped"}] and len(cards) == 1
      and cards[0]["title"].startswith("Stopped"))

# Fail closed: an owner that can't be looked up gets no step, a halt on record, no card; never a default role.
tr.opt_in("t4", "zach", "admin", "Gone owner", "after_each", issue_grant=issue, resolve=resolve, now=at(9, day=11))
cards.clear(); steps.clear()
for gone in (lambda o: None, lambda o: {"id": o}, lambda o: (_ for _ in ()).throw(RuntimeError("store down"))):
    sc.update("t4", state="active", next_due=at(9, day=11))
    out = tr.run_due(at(9, 1, day=11), **{**RUN, "person_of": gone})
    check("owner unknown: halted, no step, no card", out == [{"task_id": "t4", "status": "halted", "reason": "owner_unknown"}]
          and not steps and not cards, str(out))
last = json.loads(sqlite3.connect(os.environ["TASKS_DB"]).execute(
    "SELECT envelope FROM envelopes WHERE task_id='t4' ORDER BY at DESC LIMIT 1").fetchone()[0])
check("the halt says owner_unknown", last["outcome"]["halt"]["reason"] == "owner_unknown")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_api_server.py")).read()
check("the API never defaults a step's role upward", 'get("role") or "admin"' not in src.split("def _run_due_tasks")[1].split("async def _scheduled_runner")[0])

# AGENT_TASK_AUTONOMY_OWNERS: the trial is Zach's account only.
os.environ["AGENT_TASK_AUTONOMY_OWNERS"] = "zach"
check("an owner outside the list can't opt in", "one account first" in tr.opt_in(
    "t5", "p_robin", "adult", "Robin's thing", "daily", issue_grant=issue, resolve=resolve, now=at(9, day=12)))
sc.create("t6", "p_robin", "Made before the list", cadence="after_each", now=at(9, day=12))
sc.update("t6", next_due=at(9, day=12), state="active")
cards.clear(); steps.clear()
out = tr.run_due(at(9, 1, day=12), **RUN)
check("and the runner skips an owner outside the list, untouched", all(o["task_id"] != "t6" for o in out) and not steps
      and sc.get("t6")["state"] == "active", str(out))
check("the listed owner is allowed", sc.owner_allowed("zach") and sc.owner_allowed("ZACH") and not sc.owner_allowed("p_robin"))
os.environ.pop("AGENT_TASK_AUTONOMY_OWNERS")
check("unset: no owner restriction", sc.owner_allowed("p_robin"))

check("opt-out: cadence off stops it advancing", "won't advance on its own" in tr.opt_in(
    "t1", "zach", "admin", "Ava's party", "off", issue_grant=issue, resolve=resolve) and sc.get("t1")["cadence"] == "off")

# run_task under autonomy: only what the titan grant covers (no file writes), and the scenario in the prompt.
import agent_runtime_lib as lib  # noqa: E402
seen = {}
class WF:
    def __call__(self, rt, path, method="GET", body=None, **kw):
        if path.startswith("/v1/definitions/"):
            return {"steps": [{"step_key": "s0", "title": "Shortlist venues"}, {"step_key": "s1", "title": "Book"}]}
        if path.endswith("/steps"):
            return {}
        return {"status": "running", "current_step_key": "s0", "definition_id": "d", "input": {"goal": "Ava's party"}}
lib._runtime_request = WF()
lib._run_agent_turn = lambda sub, allowed_tools=None, **k: seen.update(tools=set(allowed_tools), prompt=sub[0]["content"]) or "DONE: ok"
lib._TURN_IO.person, lib._TURN_IO.role = {"id": "zach"}, "admin"
lib._TURN_IO.task_autonomous, lib._TURN_IO.task_context = True, "What they told you about it:\n- Sunday is fine"
lib.execute_tool("run_task", {"task_id": "t1", "max_steps": 1})
check("an autonomous step can't write files (E2 exact-only), reads stay", "file_write" not in seen["tools"]
      and "file_edit" not in seen["tools"] and "kb_retrieve" in seen["tools"], str(sorted(seen["tools"]))[:200])
check("and its prompt carries the scenario", "Sunday is fine" in seen["prompt"])
check("an autonomous step may save a note, and never uses a device tool (no screen or app reads unattended)",
      "ingest_text" in seen["tools"] and not (seen["tools"] & set(lib._DEVICE_TOOLS))
      and "get_screen" not in seen["tools"], str(sorted(seen["tools"] & set(lib._DEVICE_TOOLS))))
check("nor anything that sends data out of the house or reaches titan's files",
      not (seen["tools"] & {"jev_decide", "web_search", "fetch_url", "describe_image", "file_read", "file_list",
                            "run_check", "check_result", "host_status"}), str(sorted(seen["tools"])))
lib._TURN_IO.task_autonomous, lib._TURN_IO.task_context = False, None
lib.execute_tool("run_task", {"task_id": "t1", "max_steps": 1})
check("a task run by hand keeps its usual tools", "file_write" in seen["tools"])
tr.opt_in("t7", "zach", "admin", "On its own", "daily", issue_grant=issue, resolve=resolve, now=at(9, day=13))
seen.clear()
out = lib.execute_tool("run_task", {"task_id": "t7"})
check("a task on its own schedule isn't run by hand", out.startswith("Not run: this task advances on its own") and not seen, out)
sc.update("t7", cadence="off")
lib.execute_tool("run_task", {"task_id": "t7", "max_steps": 1})
check("once its schedule is stopped, it runs by hand again", bool(seen))
lib._TURN_IO.answered_tasks = set()
real_source = lib.turn_source
lib.turn_source = lambda: "eval"     # the answer on a test turn stores nothing, but is remembered for the turn
lib.execute_tool("task_answer", {"task_id": "task-5f2a", "text": "Sunday is fine"})
lib.turn_source = real_source        # a real turn's run_task (eval turns never execute it at all)
seen.clear()
out = lib.execute_tool("run_task", {"task_id": "task-5f2a"})
check("a task answered this turn isn't run in the same turn (even one with no schedule, as in turn_eval)",
      out.startswith("Not run: the answer was just passed") and not seen, out)
lib._TURN_IO.answered_tasks = set()

# 10-03 audit (#802's shape): changing a task already advancing keeps what the call leaves out; above all what it
# depends on, which is what makes it stop and ask ("let it do 2 a day" used to empty the watched deps).
tr.opt_in("t9", "zach", "admin", "Ava's party", "after_each", daily_cap=3, depends_on=["Ava's party"], issue_grant=issue,
          resolve=resolve, now=at(8, day=20))
tr.opt_in("t9", "zach", "admin", "Ava's party", None, daily_cap=2, issue_grant=issue, resolve=resolve, now=at(8, day=20))
s9 = sc.get("t9")
check("a change to the cap keeps the deps and the cadence", (s9["deps"]["items"], s9["cadence"], s9["daily_cap"]) ==
      (["cx-party"], "after_each", 2), str(s9))
tr.opt_in("t9", "zach", "admin", "Ava's party", "daily", issue_grant=issue, resolve=resolve, now=at(8, day=20))
s9 = sc.get("t9")
check("a change to the cadence keeps the cap and the deps", (s9["deps"]["items"], s9["cadence"], s9["daily_cap"]) ==
      (["cx-party"], "daily", 2), str(s9))
tr.opt_in("t9", "zach", "admin", "Ava's party", None, depends_on=[], issue_grant=issue, resolve=resolve, now=at(8, day=20))
check("deps given as empty clear them (said, not omitted)", sc.get("t9")["deps"].get("items") in ([], None), str(sc.get("t9")))
tr.opt_in("t10", "zach", "admin", "New one", None, issue_grant=issue, resolve=resolve, now=at(8, day=20))
check("a new one without cadence or cap: daily, 1", (sc.get("t10")["cadence"], sc.get("t10")["daily_cap"]) == ("daily", 1))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all task runner checks passed'}")
sys.exit(1 if failures else 0)
