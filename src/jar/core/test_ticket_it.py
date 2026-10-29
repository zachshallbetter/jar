#!/usr/bin/env python3
# gate: hermetic   (temp stores; the workflow runtime is a fake; nothing live)
"""#1614: "ticket it" files a ticket, never a durable task, and a durable task that IS asked for can be found.

Zach's conversations, 10-12: "Ticket it as a task to explore and test" and "add the items" both became plan_task durable
tasks (one "setting up Checkmk on Titan"), and neither could be found afterwards. Covered here, per person (fictional:
Alex the admin, Robin an adult, Ava a kid):
  - the phrasings: a ticket, items, a durable request, neither; and software installs
  - a ticket from the admin goes on Claude's list (a GitHub issue); Robin's and Ava's on their own list, said plainly;
    a kid never reaches Claude's list
  - no durable task is created for a ticket, for items, without an explicit ask, or to install software
  - a durable task that is asked for is created, its reply says where, and list_todos and tasks_for show it to its owner
    (never to another adult)
  - "ticket"/"issue" words declare add_todo; "tasks" words declare list_todos; "I've filed a ticket" after a plan_task
    is a claim no tool backs
Runs itself under TZ=UTC and TZ=America/Los_Angeles, with the clocks pinned.
Run: python3 scripts/test_ticket_it.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

if not os.environ.get("TICKET_IT_TZ"):
    rc = 0
    for tz in ("UTC", "America/Los_Angeles"):
        print(f"══ TZ={tz} ══", flush=True)
        rc |= subprocess.call([sys.executable, __file__], env={**os.environ, "TZ": tz, "TICKET_IT_TZ": tz})
    sys.exit(rc)

_TMP = Path(tempfile.mkdtemp(prefix="titan-ticket-it-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "TASK_LOG_DB": "task_log.db",
             "TASKS_DB": "tasks.db", "TODOS_DB": "todos.db", "AGENT_TURN_LOG": "turns.jsonl",
             "PROACTIVE_DB": "proactive.db", "ROUTINES_DB": "routines.db", "DAILY_LOG_DIR": "daily",
             "HOUSEHOLD_PREFS": "household.json", "REFLECTION_DB": "reflection.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.update({"MEMORY_HYBRID": "0", "AGENT_TURN_SOURCE": "test", "AGENT_TZ": "America/Los_Angeles",
                   "HA_TOKEN_FILE": str(_TMP / "no-ha-token")})
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_runtime_lib as lib  # noqa: E402
import continuity  # noqa: E402
import household_identity as hh  # noqa: E402
import task_log  # noqa: E402
import ticket_guard as tg  # noqa: E402
import todos  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = 1_791_000_000.0   # 2026-10-03, pinned
todos.time.time = lambda: NOW
task_log.time.time = lambda: NOW
continuity.emit = lambda *a, **k: None

st = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(st)
alex = st.add_person("Alex Hale", "admin")["id"]
robin = st.add_person("Robin Hale", "adult")["id"]
ava = st.add_person("Ava Hale", "kid")["id"]

# The fake workflow runtime: what plan_task makes, and what tasks_for reads back.
DEFS: dict[str, list] = {}
INSTANCES: list[dict] = []


def fake_runtime(runtime, path, method, body=None, timeout=30):
    if path == "/v1/definitions" and method == "POST":
        did = f"d-{len(DEFS)}"
        DEFS[did] = body["steps"]
        return {"definition_id": did}
    if path == "/v1/instances" and method == "POST":
        INSTANCES.append({"instance_id": body["instance_id"], "definition_id": body["definition_id"],
                          "status": "running", "current_step_key": "s0", "input": body["input"],
                          "metadata": body["metadata"], "updated_at": "2026-10-03T18:00:00Z"})
        return {"instance_id": body["instance_id"]}
    if path == "/v1/instances" and method == "GET":
        return {"instances": [dict(i) for i in INSTANCES]}
    if path.startswith("/v1/definitions/") and method == "GET":
        return {"steps": DEFS.get(path.rsplit("/", 1)[1], [])}
    return {}


lib._runtime_request = fake_runtime
lib._next_step_at = lambda tid, now: None
T = lib.execute_tool


def turn(pid, name, role, words, shown=None):
    """One person's turn: who asks, and their own words in this message (what the guards read)."""
    lib._TURN_IO.person = {"id": pid, "name": name}
    lib._TURN_IO.role = role
    lib._TURN_IO.asker_words = words
    lib._TURN_IO.shown = shown
    lib._TURN_IO.failed_calls = None


def on_list(owner):
    return [t["title"] for t in todos.list_for([owner])]


print("── the phrasings ──")
for w in ("ticket it", "Ticket it as a task to explore and test", "file a ticket for the porch light",
          "make an issue about the widget", "open a GitHub issue", "can you ticket that"):
    check(f"a ticket: {w!r}", (tg.ticket_asked(w), tg.durable_asked(w)), (True, False))
check("items: 'add the items'", (tg.items_asked("add the items"), tg.durable_asked("add the items")), (True, False))
for w in ("what's the issue with the TV", "plan a dinner party", "add milk"):
    check(f"not a ticket: {w!r}", tg.ticket_asked(w) or tg.items_asked(w), False)
check("a durable ask: 'plan a dinner party'", tg.durable_asked("plan a dinner party"), True)
check("a durable ask beside ticket words needs the strong words",
      (tg.durable_asked("ticket it so you can work on it over time"), tg.durable_asked("file a ticket to plan the trip")),
      (True, False))
for w in ("Keep working on the move until it is done", "Work on this until it is complete",
          "Thanks. Keep going on the garage until it's clear"):
    check(f"an imperative durable ask: {w!r}", tg.durable_asked(w), True)
for w in ("add milk", "I worked on it yesterday", "They keep working late", "Robin will work on the fence"):
    check(f"not a durable ask: {w!r}", tg.durable_asked(w), False)
check("software: 'Set up Checkmk on Titan'", bool(tg.installs_software("Set up Checkmk on Titan")), True)
check("software: 'docker run checkmk'", bool(tg.installs_software("docker run checkmk/check-mk-raw")), True)
check("not software: 'Install the car seat'", tg.installs_software("Install the car seat"), None)

print("── Alex (admin): a ticket goes on Claude's list, never a task ──")
turn(alex, "Alex", "admin", "Ticket it as a task to explore and test")
said = T("plan_task", {"goal": "Explore and test the paper", "steps": "Read it; Try the method"})
check("plan_task refused, pointing at add_todo", (said.startswith("Not planned: they asked for a ticket"),
                                                  "add_todo" in said), (True, True))
check("…no durable task made", len(INSTANCES), 0)
said = T("add_todo", {"title": "Explore and test the paper's method", "kind": "ticket"})
check("add_todo kind=ticket, no `for`: Claude's list (a GitHub issue)", (on_list(todos.CLAUDE), on_list(alex)),
      (["Explore and test the paper's method"], []))
check("…the reply says it becomes a GitHub issue", "becomes a GitHub issue" in said, True)
turn(alex, "Alex", "admin", "add the items")
said = T("plan_task", {"goal": "Checkmk", "steps": "Set up Checkmk on titan; Add hosts"})
check("'add the items' + installing software: refused, naming the install", (said.startswith("Not planned: titan never "
      "installs"), len(INSTANCES)), (True, 0))
said = T("plan_task", {"goal": "Video notes", "steps": "Watch again; Note the tips"})
check("'add the items' without software: still no task", (said.startswith("Not planned: they asked for a ticket"),
                                                         len(INSTANCES)), (True, 0))
said = T("add_todo", {"title": "Look at Checkmk for monitoring"})
check("…items go on Alex's own list (only ticket words mean Claude's)", "Look at Checkmk for monitoring" in on_list(alex),
      True)

turn(alex, "Alex", "admin", "file a ticket for the crash and add milk")
n_claude = len(on_list(todos.CLAUDE))
T("add_todo", {"title": "App crashes on launch", "kind": "ticket"})
said = T("add_todo", {"title": "Milk"})
check("a mixed message routes per item: the crash on Claude's list, the milk on Alex's own",
      (on_list(todos.CLAUDE)[n_claude:], "Milk" in on_list(alex), "Milk" in on_list(todos.CLAUDE)),
      (["App crashes on launch"], True, False))
check("…the milk's reply is a plain todo", said, "Added “Milk” to your list.")

print("── Robin (adult): a ticket is a to-do on her own list, said plainly ──")
n_claude = len(on_list(todos.CLAUDE))
turn(robin, "Robin", "adult", "file a ticket for the porch light flicker")
said = T("add_todo", {"title": "Porch light flickers", "kind": "ticket"})
check("on Robin's list", ("Porch light flickers" in on_list(robin), len(on_list(todos.CLAUDE))), (True, n_claude))
check("…the reply says it's a to-do, not a ticket", ("to your list" in said, "not a ticket" in said), (True, True))
said = T("add_todo", {"title": "Porch light ticket", "for": "claude"})
check("Robin can't reach Claude's list", (said, len(on_list(todos.CLAUDE))),
      ("Not done: Only the household's admin keeps a list for Claude.", n_claude))
said = T("plan_task", {"goal": "Porch light", "steps": "Buy bulb; Replace"})
check("no durable task for Robin's ticket", len(INSTANCES), 0)

print("── Ava (kid): never a GitHub issue ──")
turn(ava, "Ava", "kid", "make an issue about the tablet being slow")
said = T("add_todo", {"title": "Tablet is slow", "for": "claude"})
check("a kid's for=claude is refused", (said.startswith("Not done:"), len(on_list(todos.CLAUDE))), (True, n_claude))
said = T("add_todo", {"title": "Tablet is slow", "kind": "ticket"})
check("…her own list instead, said plainly", ("Tablet is slow" in on_list(ava), "not a ticket" in said), (True, True))
T("plan_task", {"goal": "Tablet", "steps": "Restart; Clear space"})
check("no durable task for a kid", len(INSTANCES), 0)

print("── a durable task only on an explicit ask ──")
turn(alex, "Alex", "admin", "can you add milk")
said = T("plan_task", {"goal": "Buy milk", "steps": "Go to the store; Buy milk"})
check("no explicit ask: refused", (said.startswith("Not planned: a durable task is only"), len(INSTANCES)), (True, 0))
turn(alex, "Alex", "admin", "work on it over time: set up Checkmk on titan")
said = T("plan_task", {"goal": "Checkmk monitoring", "steps": "Set up Checkmk on titan; Add hosts"})
check("even asked for, never one that installs software", (said.startswith("Not planned: titan never installs"),
                                                          len(INSTANCES)), (True, 0))
turn(alex, "Alex", "admin", "Plan the garage clean-out for Saturday")
said = T("plan_task", {"goal": "Garage clean-out", "steps": "Sort shelves; Donate boxes; Sweep"})
check("an explicit ask: the task is made", (said.startswith("Started task “Garage clean-out”"), len(INSTANCES)), (True, 1))
check("…its reply says where to find it", "under Tasks in Jellybean" in said, True)
check("…owned by Alex", INSTANCES[0]["metadata"]["owner"], alex)
turn(alex, "Alex", "admin", "Keep working on the move until it is done")
said = T("plan_task", {"goal": "The move", "steps": "Book movers; Pack the kitchen"})
check("an imperative ask makes the task", (said.startswith("Started task “The move”"), len(INSTANCES)), (True, 2))
INSTANCES.pop()
turn(alex, "Alex", "admin", "yes", shown="Want me to plan the recital prep as a task?")
said = T("plan_task", {"goal": "Recital prep", "steps": "Book the hall; Print programs"})
check("a bare yes to titan's own offer of a task counts", (said.startswith("Started task"), len(INSTANCES)), (True, 2))
turn(alex, "Alex", "admin", "yes", shown="Done. Anything else?")
said = T("plan_task", {"goal": "Something else", "steps": "One; Two"})
check("…a yes to anything else doesn't", (said.startswith("Not planned"), len(INSTANCES)), (True, 2))
lib._TURN_IO.asker_words = None
said = T("plan_task", {"goal": "Fence repair", "steps": "Measure; Buy posts"})
check("no person's words (a direct call): planned as before", (said.startswith("Started task"), len(INSTANCES)), (True, 3))

print("── findable ──")
turn(alex, "Alex", "admin", "what are my tasks")
out = T("list_todos", {})
check("list_todos shows Alex's durable tasks, with ids", ("Tasks titan is working on" in out, "Garage clean-out" in out,
      INSTANCES[0]["instance_id"] in out), (True, True, True))
check("…and still his todos", "Look at Checkmk for monitoring" in out, True)
check("…'me' too", "Garage clean-out" in T("list_todos", {"for": "me"}), True)
check("…not on Claude's list alone", "Garage clean-out" in T("list_todos", {"for": "claude"}), False)
ids = [t["id"] for t in lib.tasks_for(alex, "admin")["tasks"]]
check("tasks_for (GET /v1/tasks, Hub › Tasks) has it", INSTANCES[0]["instance_id"] in ids, True)
turn(robin, "Robin", "adult", "what are my tasks")
out = T("list_todos", {})
check("another adult never sees Alex's private task", ("Garage clean-out" in out, "Tasks titan" in out), (False, False))
INSTANCES[0]["status"] = "completed"
turn(alex, "Alex", "admin", "what's on my list")
check("a finished task leaves the list", "Garage clean-out" in T("list_todos", {}), False)

print("── tools declared, claims checked ──")
for w in ("ticket it", "Ticket it as a task to explore and test", "file a ticket for the porch light",
          "make an issue about the widget", "open a GitHub issue", "can you ticket that", "file a bug for the crash",
          "log a bug", "add the items", "add these steps", "add all those ideas", "put it in as a ticket"):
    check(f"guarded and offered add_todo: {w!r}", (tg.ticket_asked(w) or tg.items_asked(w),
                                                   "add_todo" in lib.narrowed_tools(w)), (True, True))
check("'ticket it' declares add_todo", "add_todo" in lib.narrowed_tools("ticket it as a task to explore"), True)
check("'make an issue' declares add_todo", "add_todo" in lib.narrowed_tools("make an issue for that"), True)
check("'what are my tasks' declares list_todos", "list_todos" in lib.narrowed_tools("what are my tasks"), True)
lib._TURN_IO.outcomes = [{"tool": "plan_task", "ok": True, "text": "Started task"}]
check("'I've filed a ticket' after only plan_task: a claim no tool backs",
      [w for _, w in lib._unbacked_claims("I've filed a ticket for that.")], ["file that ticket"])
lib._TURN_IO.outcomes = [{"tool": "add_todo", "ok": True, "text": "Added"}]
check("…after add_todo: backed", lib._unbacked_claims("I've filed a ticket for that."), [])
lib._TURN_IO.outcomes = None
lib._TURN_IO.person = lib._TURN_IO.role = lib._TURN_IO.asker_words = lib._TURN_IO.shown = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
