#!/usr/bin/env python3
# gate: hermetic
"""What titan did for each person (#1645, capability_audit.summary and the what_i_did tool): the changes that went
through, split into the ones they asked for and the ones titan did on its own; what didn't finish; their own no's;
lookups counted; plain names, never tool ids; their own record only; eval rows aren't anyone's. Fictional people (Alex
the admin, Robin an adult, Ava a kid); a temporary store. Run: python3 scripts/test_what_i_did.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-what-i-did-"))
os.environ.update({"SPACES_DB": str(_TMP / "spaces.db"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "CAPABILITY_SWITCHES": str(_TMP / "switches.json"), "PROACTIVE_DB": str(_TMP / "proactive.db"),
                   "MCP_SERVERS": str(_TMP / "none.json"), "AGENT_TURN_SOURCE": "user"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import capability_audit as CA  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


NOW = time.time()


def rec(person, tool, risk, outcome, source="user", ago=3600, why=None):
    CA.record(person=person, role="adult", surface="iphone", source=source, tool=tool, domain="x", risk=risk,
              outcome=outcome, why=why, now=NOW - ago)


for _ in range(2):
    rec("p_robin", "add_todo", "write", "ok")
rec("p_robin", "complete_todo", "write", "ok", source="routine")
rec("p_robin", "home_control", "write", "ok", source="proactive")
rec("p_robin", "home_control", "write", "ok", source="scheduled")
rec("p_robin", "search_mail", "read", "ok")
rec("p_robin", "kb_retrieve", "read", "ok", source="proactive")
rec("p_robin", "mail_move", "write", "declined", why="person")
rec("p_robin", "home_control", "write", "refused", why="role")
rec("p_robin", "home_control", "write", "timeout")
rec("p_robin", "add_todo", "write", "ok", ago=3 * 86400)              # older than a day
rec("p_robin", "add_todo", "write", "ok", source="eval")              # eval turns aren't anyone's: not recorded
rec("p_alex", "add_todo", "write", "ok")                              # Alex's own

print("── the summary ──")
s = CA.summary("p_robin", since=NOW - 86400)
check("asked vs on its own, changes that went through", (s["asked"], s["own"]),
      ({"add_todo": 2}, {"complete_todo": 1, "home_control": 2}))
check("lookups counted, not listed; a no is the person's, apart; what didn't finish by how",
      (s["reads"], s["declined"], s["not_done"]), (2, 1, {"refused": 1, "timeout": 1}))
check("three days back: the older to-do too", CA.summary("p_robin", since=NOW - 4 * 86400)["asked"], {"add_todo": 3})
check("Alex's record is Alex's", CA.summary("p_alex", since=NOW - 86400)["asked"], {"add_todo": 1})
check("someone with no record: empty", CA.summary("p_ava", since=NOW - 86400),
      {"asked": {}, "own": {}, "reads": 0, "not_done": {}, "declined": 0, "waiting": 0})

print("── the tool ──")
import agent_runtime_lib as lib  # noqa: E402


def as_person(pid, role="adult"):
    lib._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    lib._TURN_IO.role = role
    lib._TURN_IO.source = "user"
    lib._TURN_IO.capability_tiers = None


as_person("p_robin")
out = lib._execute_tool("what_i_did", {})
lines = out.splitlines()
check("Robin's day: on its own first, then what she asked, in plain words",
      lines[:3], ["In the last day (changes, by what they touched):",
                  f"On its own: {lib._TOOL_PLAIN.get('home_control', 'home control')} (2), the to-do list.",
                  "When you asked: the to-do list (2)."])
check("…what didn't finish, her no, the lookups", lines[3:],
      ["Didn't finish: 1 not allowed, 1 timed out.", "You said no 1 time.", "It looked things up 2 times."])
check("no tool ids in it", any(t in out for t in ("add_todo", "home_control", "complete_todo", "mail_move")), False)
check("days: at most 7, odd values are a day", (lib._execute_tool("what_i_did", {"days": 30}).startswith("In the last 7 days"),
                                                lib._execute_tool("what_i_did", {"days": "x"}).startswith("In the last day")),
      (True, True))
as_person("p_ava", "kid")
check("Ava (a kid) reads her own, which is empty", lib._execute_tool("what_i_did", {}).splitlines()[1],
      "titan changed nothing for you in the last day.")
check("offered to adults and kids (each their own), never a shared screen",
      [(as_person(p, r), lib._role_allows("what_i_did"))[1] for p, r in (("p_alex", "admin"), ("p_robin", "adult"),
                                                                        ("p_ava", "kid"), (None, "household"))],
      [True, True, True, False])
as_person(None, "admin")
check("no person asking: nothing read", lib._what_i_did({}).startswith("That's each person's own record"), True)
import tool_descriptors as td  # noqa: E402
d = td.lookup("what_i_did")
check("a read, nothing leaves the house", (d.risk, d.egress), ("read", ()))
print("── from the real record: what started each turn (review) ──")
os.environ["TODOS_DB"] = str(_TMP / "todos.db")
import todos  # noqa: E402


def run_add(title, **io):
    as_person("p_jordan")
    for k in ("scheduled", "jev_source", "task_autonomous"):
        setattr(lib._TURN_IO, k, None)
    lib._TURN_IO.source = io.pop("source", "user")
    for k, v in io.items():
        setattr(lib._TURN_IO, k, v)
    try:
        return lib.execute_tool("add_todo", {"title": title})
    finally:
        for k in ("scheduled", "jev_source", "task_autonomous"):
            setattr(lib._TURN_IO, k, None)
        lib._TURN_IO.source = "user"


run_add("Water the ferns")                                     # Jordan asked, in their chat
run_add("Return the library books", source="temporary")       # their temporary chat: still theirs
run_add("Buy lamp oil", scheduled="sa-1")                      # a scheduled action or routine ran it
run_add("Check the gutters", jev_source="proactive")           # the proactive loop
run_add("Draft the trip list", task_autonomous=True)           # a durable task's step
rows = CA.log("p_jordan", limit=20)
check("the record says what started each one", sorted(r["source"] for r in rows),
      sorted(["user", "temporary", "scheduled", "proactive", "task"]))
s = CA.summary("p_jordan", since=time.time() - 60)
check("asked: their chat and their temporary chat; on its own: the schedule, the loop, the task step",
      (s["asked"], s["own"]), ({"add_todo": 2}, {"add_todo": 3}))
CA.record(person="p_jordan", role="adult", surface="iphone", source="user", tool="kb_forget", domain="knowledgeBase",
          risk="dangerous", outcome="ok", why="act_quiet")
CA.record(person="p_jordan", role="adult", surface="iphone", source="user", tool="kb_forget", domain="knowledgeBase",
          risk="dangerous", outcome="refused", why="held")
s = CA.summary("p_jordan", since=time.time() - 60)
check("a change that ran under an act-alone level is on its own, even in their chat", s["own"].get("kb_forget"), 1)
check("a change held for their OK is waiting, not a failure", (s["waiting"], s["not_done"]), (1, {}))
as_person("p_jordan")
check("…and said so", "Waiting for your OK: 1." in lib._execute_tool("what_i_did", {}), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
