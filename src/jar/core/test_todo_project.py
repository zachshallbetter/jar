#!/usr/bin/env python3
# gate: hermetic
"""A to-do filed under a project as it's added (#1646, add_todo's project): by name among the projects the asker may
file into; checked first, so a refusal adds nothing; only the asker's own list or (an adult's) the household's, never
another person's to-do into the asker's project; names listed wrapped; eval turns change nothing. Fictional people
(Alex the admin, Robin an adult, Ava a kid); temporary stores. Run: python3 scripts/test_todo_project.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-todo-project-"))
os.environ.update({"SPACES_DB": str(_TMP / "spaces.db"), "TODOS_DB": str(_TMP / "todos.db"),
                   "HOUSEHOLD_DB": str(_TMP / "household.db"), "CAPABILITY_SWITCHES": str(_TMP / "switches.json"),
                   "PROACTIVE_DB": str(_TMP / "proactive.db"), "MCP_SERVERS": str(_TMP / "none.json"),
                   "AGENT_TURN_SOURCE": "user"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import projects  # noqa: E402
import spaces as S  # noqa: E402
import todos  # noqa: E402
import household_identity as hh  # noqa: E402

store = hh.HouseholdStore(_TMP / "store.db")
hh.reset_store(store)
ROBIN = store.add_person("Robin Hale", "adult")["id"]

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def as_person(pid, role="admin", source="user"):
    lib._TURN_IO.person = {"id": pid, "name": pid.title()} if pid else None
    lib._TURN_IO.role = role
    lib._TURN_IO.source = source
    lib._TURN_IO.capability_tiers = None


kitchen = S.create_project("alex", "admin", "Kitchen")
S.create_project(ROBIN, "adult", "Garden")
shared = S.create_project("alex", "admin", "Trip", audience="household")


def filed_under(title):
    t = next(x for x in todos.list_for(["alex", ROBIN, "household", "ava"]) if x["title"] == title)
    return projects.filings().get(f"todo:{t['id']}")


as_person("alex")
out = lib._execute_tool("add_todo", {"title": "Call the tile shop", "project": "kitchen"})
check("Alex adds a to-do to their Kitchen project (any case): added and filed", (out, filed_under("Call the tile shop")),
      ("Added “Call the tile shop” to your list. It's filed under Kitchen.", kitchen["id"]))
n = len(todos.list_for(["alex"]))
out = lib._execute_tool("add_todo", {"title": "Order grout", "project": "Garden"})
check("a project that isn't theirs to file into (Robin's Garden): not added at all, their own names wrapped",
      (out.startswith("Not added: none of your projects is called that."), lib._TURN_IO.untrusted_nonce in out,
       "Garden" in out, len(todos.list_for(["alex"])) - n), (True, True, False, 0))
out = lib._execute_tool("add_todo", {"title": "Book the cabin", "for": "household", "project": "Trip"})
check("the household's list, into Alex's shared Trip project: filed", filed_under("Book the cabin"), shared["id"])
n = len(todos.list_for([ROBIN]))
out = lib._execute_tool("add_todo", {"title": "Pick up the keys", "for": "robin", "project": "Kitchen"})
check("someone else's list (Robin's) into Alex's project: refused, nothing added",
      (out, len(todos.list_for([ROBIN])) - n), ("Not added: a project holds your own to-dos and the household's.", 0))
as_person(ROBIN, "adult")
out = lib._execute_tool("add_todo", {"title": "Pack the tent", "project": "Trip"})
check("Robin files her own to-do under the household's Trip (she can see it)", filed_under("Pack the tent"), shared["id"])
out = lib._execute_tool("add_todo", {"title": "Measure the island", "project": "Kitchen"})
check("…but never under Alex's private Kitchen", out.startswith("Not added: none of your projects"), True)
as_person("ava", "kid")
out = lib._execute_tool("add_todo", {"title": "Bring snacks", "for": "household", "project": "Trip"})
check("a kid can't file the household's to-dos", out, "Not added: a project holds your own to-dos.")
as_person("alex", source="eval")
n = len(todos.list_for(["alex"]))
check("an eval turn adds and files nothing", (lib._execute_tool("add_todo", {"title": "Seal the tiles", "project": "Kitchen"})
                                              .startswith("Test turn"), len(todos.list_for(["alex"])) - n), (True, 0))
as_person("alex")
out = lib._execute_tool("add_todo", {"title": "Buy towels"})
check("no project: exactly as before", (out, filed_under("Buy towels")), ("Added “Buy towels” to your list.", None))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
