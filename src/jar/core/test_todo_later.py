#!/usr/bin/env python3
# gate: hermetic
"""Putting a todo off by asking (#1646; the app's Later, #1233): an open todo the asker can see leaves their own Now
until a date (9 AM when no time is given) or a number of days, at most 30; only theirs, never anyone else's who sees
the list; a done todo, a shared screen and an eval turn change nothing. Fictional people (Alex the admin, Robin an
adult, Ava a kid); temporary stores. Run: python3 scripts/test_todo_later.py"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-todo-later-"))
os.environ.update({"SPACES_DB": str(_TMP / "spaces.db"), "TODOS_DB": str(_TMP / "todos.db"),
                   "HOUSEHOLD_DB": str(_TMP / "household.db"), "CAPABILITY_SWITCHES": str(_TMP / "switches.json"),
                   "PROACTIVE_DB": str(_TMP / "proactive.db"), "MCP_SERVERS": str(_TMP / "none.json"),
                   "AGENT_TURN_SOURCE": "user"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402
import todos  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


store = hh.HouseholdStore(_TMP / "store.db")
hh.reset_store(store)
ROBIN = store.add_person("Robin Hale", "adult")["id"]
AVA = store.add_person("Ava Hale", "kid")["id"]


def as_person(pid, role="admin", source="user"):
    lib._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    lib._TURN_IO.role = role
    lib._TURN_IO.source = source
    lib._TURN_IO.capability_tiers = None


grout = todos.add("Order grout", "household", created_by="Alex")
tiles = todos.add("Call the tile shop", ROBIN, created_by="Robin")
done = todos.add("Pay the plumber", ROBIN, created_by="Robin")
todos.complete(done["id"], done_by="Robin")
now = lib._local_now()

as_person(ROBIN, "adult")
out = lib._execute_tool("todo_later", {"todo": "tile shop", "days": 2})
want = (now + timedelta(days=2)).replace(hour=9, minute=0, second=0, microsecond=0)
check("Robin puts her todo off two days: until 9 AM then, said plainly",
      (out.startswith("Put “Call the tile shop” off until"), "only yours" in out), (True, True))
check("…hidden from Robin's Now until then", f"todo:{tiles['id']}" in proactive.snoozed_items(ROBIN), True)
check("…at 9 AM, two days out", proactive.snoozed_items(ROBIN, now=want.timestamp() - 60) >= {f"todo:{tiles['id']}"}
      and f"todo:{tiles['id']}" not in proactive.snoozed_items(ROBIN, now=want.timestamp() + 60), True)
day = (now + timedelta(days=3)).date().isoformat()
lib._execute_tool("todo_later", {"todo": "grout", "until": day})
check("the household's todo, until a date: hidden for Robin only, never for Alex", (
      f"todo:{grout['id']}" in proactive.snoozed_items(ROBIN), f"todo:{grout['id']}" in proactive.snoozed_items(hh.ADMIN_PERSON_ID)),
      (True, False))
for label, args in (("31 days", {"todo": "grout", "days": 31}), ("in the past", {"todo": "grout", "until": "2020-01-01"}),
                    ("not a date", {"todo": "grout", "until": "someday"})):
    check(f"{label}: refused", lib._execute_tool("todo_later", args).startswith("Not done"), True)
import datetime as _dt  # noqa: E402
real_now = lib._local_now
early = real_now().replace(hour=6, minute=0, second=0, microsecond=0)
lib._local_now = lambda: early   # 6 AM: "30 days" at 9 AM would be past the cap by three hours
check("30 days, asked at 6 AM: allowed, back at the cap (review)",
      lib._execute_tool("todo_later", {"todo": "grout", "days": 30}).startswith("Put “Order grout” off until"), True)
far = (early + _dt.timedelta(days=30)).date().isoformat()
check("…a date 30 days out too", lib._execute_tool("todo_later", {"todo": "grout", "until": far}).startswith("Put"), True)
lib._local_now = real_now
check("days 0 (a number): refused, never a day", lib._execute_tool("todo_later", {"todo": "grout", "days": 0}).startswith("Not done"),
      True)
zulu = (real_now() + _dt.timedelta(days=2)).astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
check("an until ending in Z (UTC, as models write it) is read", lib._execute_tool("todo_later", {"todo": "grout", "until": zulu})
      .startswith("Put"), True)
check("not on titan's list (a Reminders item): says to move the Reminder's due date (10-16 live)",
      lib._execute_tool("todo_later", {"todo": "Eggs", "days": 2}),
      "Not done: no open todo like that on titan's to-do list. If it's a Reminder (list_reminders), put it off by "
      "changing its due date with update_reminder.")
check("…complete_todo keeps its own words", lib._execute_tool("complete_todo", {"todo": "Eggs"}),
      "Not done: no open todo like that. Use list_todos to see them.")
check("a done todo: nothing to put off", lib._execute_tool("todo_later", {"todo": "plumber", "days": 1}).startswith("Not done"),
      True)
as_person(AVA, "kid")
check("Ava (a kid) can't reach Robin's list", lib._execute_tool("todo_later", {"todo": "tile shop", "for": "Robin", "days": 1})
      .startswith("Not done"), True)
out = lib._execute_tool("todo_later", {"todo": "grout", "days": 1})
check("…but can put the household's todo off on her own Now", f"todo:{grout['id']}" in proactive.snoozed_items(AVA), True)
as_person(None, "household")
check("a shared screen has no Now to put it off on", lib._role_allows("todo_later"), False)
as_person(ROBIN, "adult", source="eval")
n = len(proactive.snoozed_items(ROBIN))
lib._execute_tool("todo_later", {"todo": "grout", "days": 5})
check("an eval turn changes nothing", len(proactive.snoozed_items(ROBIN)), n)
import tool_descriptors as td  # noqa: E402
check("a write in the todos domain, undone by itself", (td.lookup("todo_later").domain, td.lookup("todo_later").risk),
      ("todos", "write"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
