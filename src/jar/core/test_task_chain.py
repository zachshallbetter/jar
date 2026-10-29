#!/usr/bin/env python3
# gate: hermetic
"""#343: quick read-only steps back to back (TASK_CHAIN). Off by default; when on, after a run's first step advanced,
more steps run in read-only mode until one needs a change (left for the next scheduled run), needs the person, ends the
task, a budget runs out, or a guard trips (should_stop, the grant, a cap, quiet hours, a conflicted dependency in a fresh
envelope). One card per run with the steps' titles, never what a step read. The chained tool set: E0 only, no egress,
no device. Hermetic: temp store; steps, cards, changes and grants faked. Run: python3 scripts/test_task_chain.py"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
tmp = Path(tempfile.mkdtemp())
os.environ["TASKS_DB"] = str(tmp / "tasks.db")
os.environ["AGENT_TASK_AUTONOMY"] = "1"
os.environ.pop("TASK_CHAIN", None)
import scenarios as sc  # noqa: E402
import task_runner as tr  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


PT = ZoneInfo("America/Los_Angeles")


def at(h, m=0, day=2):
    return datetime(2026, 10, day, h, m, tzinfo=PT).timestamp()


SECRET = "Ava's teacher wrote: pickup moves to 2:15"
calls, cards = [], []
script = {"steps": []}
change_rows = {"rows": [], "after_first": None}
grant = {"why": None}


def step(title, kind="done", task_done=False):
    line = {"done": f"✓ {title} — {SECRET}", "later": f"↷ {title} — needs a change; it runs at the next scheduled step",
            "blocked": f"⏸ {title} — needs you: which school?"}[kind]
    return f"Working on “Weekly kids' note”:\n{line}\n(1 step run; task is {'completed' if task_done else 'running'})"


def execute_step(s, ctx, person, chained=False):
    calls.append(chained)
    if len(calls) == 1 and change_rows["after_first"] is not None:
        change_rows["rows"] = change_rows["after_first"]
    return script["steps"].pop(0) if script["steps"] else step("Idle", "done")


def changes_fn(since, item_ids=None, person=None, role=None):
    return [r for r in change_rows["rows"] if r.get("item_id") in (item_ids or [])]


def card(title, detail, **k):
    cards.append({"title": title, "detail": detail})


RUN = dict(execute_step=execute_step, card=card, changes_fn=changes_fn, world_fn=lambda ids, since, p, r: [],
           describe=lambda cs: "; ".join(c["item_id"] for c in cs), grant_use=lambda gid, owner, tid: grant["why"],
           person_of=lambda o: {"id": o, "role": "admin"})
N = {"i": 0}


def fresh(steps, rows_after_first=None):
    """A new due task (its own id), a script of step results, and a clean slate."""
    N["i"] += 1
    tid = f"t{N['i']}"
    tr.opt_in(tid, "zach", "admin", "Weekly kids' note", "daily", daily_cap=1, depends_on=["Weekly party"],
              issue_grant=lambda o, subj, now: {"id": "gr-1"},
              resolve=lambda names: tr.resolve_deps(names, "zach", "admin", world_find=lambda n: [],
                                                    item_find=lambda n: ["cx-party"]), now=at(8))
    sc.update(tid, next_due=at(9))
    for t in sc.due(at(10)):
        if t["task_id"] != tid:
            sc.update(t["task_id"], state="done", next_due=None)
    calls.clear(), cards.clear()
    script["steps"] = list(steps)
    change_rows["rows"], change_rows["after_first"] = [], rows_after_first
    grant["why"] = None
    return tid


THREE = [step("Read this week's school mail", "done"), step("Read the calendar", "done"),
         step("List the kids' events", "done", task_done=True)]

print("── off by default: today's runner ──")
tid = fresh(THREE)
out = tr.run_due(at(10), **RUN)
check("one step a run, never chained", (calls, out[0]["status"]), ([False], "advanced"))
check("today's card (the step's result) unchanged", SECRET in cards[0]["detail"], True)

os.environ["TASK_CHAIN"] = "1"
print("── on: read-only steps back to back ──")
tid = fresh(THREE)
out = tr.run_due(at(10), **RUN)
check("the first step as today, then two chained in read-only mode; the task finishes in one run",
      (calls, out[0]["status"], out[0].get("chained")), ([False, True, True], "done", 3))
check("one card for the run", len(cards), 1)
check("…listing each step by its title", cards[0]["detail"],
      "Steps taken: Read this week's school mail; Read the calendar; List the kids' events.")
check("…never what a step read", SECRET in cards[0]["detail"], False)
check("the task is done", sc.get(tid)["state"], "done")

print("── a chained step that needs a change waits for the next scheduled run ──")
tid = fresh([step("Read the calendar", "done"), step("Create the note", "later")])
out = tr.run_due(at(10), **RUN)
check("the chain ends there; status needs_write", (calls, out[0]["status"]), ([False, True], "needs_write"))
check("not waiting on the person: the next scheduled run takes it", (sc.get(tid)["state"], bool(sc.get(tid)["next_due"])),
      ("active", True))
check("the card says the next step makes a change", "next step makes a change" in cards[0]["detail"], True)

print("── a chained step that needs the person ends the chain ──")
tid = fresh([step("Read the calendar", "done"), step("Pick the school", "blocked")])
out = tr.run_due(at(10), **RUN)
check("blocked: chain ends, task waits", (calls, out[0]["status"], sc.get(tid)["state"]), ([False, True], "blocked", "waiting"))

print("── budgets ──")
tid = fresh([step(f"Read part {i}", "done") for i in range(10)])
out = tr.run_due(at(10), **RUN)
check(f"CHAIN_STEPS ({tr.CHAIN_STEPS}): at most that many after the first", len(calls), 1 + tr.CHAIN_STEPS)
old = tr.CHAIN_SECONDS
tr.CHAIN_SECONDS = 0.0
tid = fresh([step(f"Read part {i}", "done") for i in range(10)])
tr.run_due(at(10), **RUN)
check("CHAIN_SECONDS spent: no chained step", calls, [False])
tr.CHAIN_SECONDS = old

print("── guards, per chained step ──")
tid = fresh([step("Read part 1", "done"), step("Read part 2", "done")],
            rows_after_first=[{"item_id": "cx-party", "change": "changed", "diff": {"start": ["Sat", "Sun"]}, "at": at(10)}])
out = tr.run_due(at(10), **RUN)
check("a dependency conflicted after the first step: the chain freezes (fresh envelope each step)", calls, [False])
tid = fresh([step("Read part 1", "done"), step("Read part 2", "done")],
            rows_after_first=[{"item_id": "cx-party", "change": "changed", "diff": {"title": ["Party", "Ava's party"]},
                               "at": at(10)}])
tr.run_due(at(10), **RUN)
check("stale but safe: the chain continues", calls[:2], [False, True])
tid = fresh([step("Read part 1", "done"), step("Read part 2", "done")])
real_use = RUN["grant_use"]
seen = {"n": 0}


def grant_once(gid, owner, tid_):
    seen["n"] += 1
    return None if seen["n"] == 1 else "expired"


tr.run_due(at(10), **{**RUN, "grant_use": grant_once})
check("the grant is checked per chained step: expired after the first, no chained step", calls, [False])
tid = fresh([step("Read part 1", "done"), step("Read part 2", "done")])
real_stop = sc.should_stop
sc.should_stop = lambda t, db=None: len(calls) >= 1
tr.run_due(at(10), **RUN)
sc.should_stop = real_stop
check("should_stop is checked per chained step", calls, [False])

print("── the daily cap counts runs, not chained steps ──")
tid = fresh([step(f"Read part {i}", "done") for i in range(3)])
tr.run_due(at(10), **RUN)
check("one run, several steps, the cap still lets tomorrow's run go", sc.get(tid)["state"] in ("active", "done"), True)

print("── the chained tool set ──")
import agent_runtime_lib as L  # noqa: E402

chained = L._CHAINED_STEP_TOOLS()
check("E0 only", sorted(t for t in chained if L.effect_class(t) != "E0"), [])
check("no egress tool (a fetched page can't add a fetch)", sorted(chained & L.EGRESS_TOOLS), [])
check("no device tool", sorted(t for t in chained if t in L._DEVICE_TOOLS), [])
check("a subset of the autonomous set", chained <= L._AUTONOMOUS_STEP_TOOLS(), True)
check("no change tool", sorted(chained & L._change_tools()), [])
print("── run_task itself in chained mode ──")
offered, completed = [], []
INST = {"status": "running", "current_step_key": "s2", "input": {"goal": "Weekly kids' note"}, "definition_id": "d1"}


def rt(svc, path, method="POST", payload=None, timeout=30):
    if path.startswith("/v1/definitions/"):
        return {"steps": [{"step_key": "s2", "title": "Create the note"}]}
    return dict(INST)


reply = {"text": "LATER: create the note in Notes"}
L._runtime_request = rt
L._task_live = lambda *a, **k: None
L._run_agent_turn = lambda msgs, allowed_tools=None, max_rounds=4: (offered.append(set(allowed_tools or ())), reply["text"])[1]
real_exec = L.execute_tool
L.execute_tool = lambda name, args: (completed.append(name), "ok")[1] if name == "complete_step" else real_exec(name, args)
io = L._TURN_IO
io.task_autonomous, io.task_chained, io.task_context = True, True, "ctx"
out = real_exec("run_task", {"task_id": "t-x", "max_steps": 1})
check("a chained step is offered only the chained read-only set (declared, not just refused)", offered[-1] == L._CHAINED_STEP_TOOLS(), True)
check("LATER: not completed, and the run says it waits for the next scheduled step",
      (completed, "needs a change; it runs at the next scheduled step" in out, tr._status_of(out)), ([], True, "needs_write"))
io.task_chained = False
reply["text"] = "DONE: read it"
real_exec("run_task", {"task_id": "t-x", "max_steps": 1})
check("an unchained autonomous step keeps the full autonomous set", offered[-1] == L._AUTONOMOUS_STEP_TOOLS(), True)
io.task_autonomous, io.task_context = False, None
L.execute_tool = real_exec
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
