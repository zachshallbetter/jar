#!/usr/bin/env python3
"""Todos (todos.py): the tools, who sees which list, and the API. Hermetic: temp stores.
Run: python3 scripts/test_todos.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-todos-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["AGENT_TURN_SOURCE"] = "user"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import todos  # noqa: E402

lib._runtime_request = lambda *a, **k: {"instances": []}   # list_todos also lists durable tasks (#1614): none here

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")
    return good


def as_(role, pid=None, name=None):
    lib._TURN_IO.role = role
    lib._TURN_IO.person = {"id": pid, "name": name} if pid else None


store = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(store)
rob = store.add_person("Robin", "adult")["id"]
leo = store.add_person("Leo", "kid")["id"]
T = lib.execute_tool

print("── adding ──")
as_("admin", "zach", "Zach")
check("my own list by default", T("add_todo", {"title": "Renew car registration"}), "Added “Renew car registration” to your list.")
check("the household's", T("add_todo", {"title": "Buy furnace filter", "for": "household"}), "Added “Buy furnace filter” to the household's list.")
check("Claude's (admin)", T("add_todo", {"title": "Tailscale in phone setup", "detail": "user can just add the VPN", "for": "claude"}).startswith(
      "Added “Tailscale in phone setup” to Claude's list. Within 10 minutes it becomes a GitHub issue"), True)
check("someone else's by name", T("add_todo", {"title": "Call the dentist", "for": "Robin"}), "Added “Call the dentist” to Robin's list.")
check("an unknown name lists the lists", T("add_todo", {"title": "x", "for": "Jordan"}).startswith("Not done: No one in the household called Jordan"), True)
check("no title", T("add_todo", {"title": "  "}), "Not added: a todo needs a title")

print("\n── who sees what ──")
out = T("list_todos", {})
check("the admin sees his, the household's and Claude's", all(x in out for x in ("Renew car registration", "Buy furnace filter", "Tailscale in phone setup")), True)
check("…not Robin's", "Call the dentist" in out, False)
check("one list", T("list_todos", {"for": "claude"}), "• Tailscale in phone setup — user can just add the VPN")
as_("adult", rob, "Robin")
out = T("list_todos", {})
check("Robin sees hers and the household's", ("Call the dentist" in out, "Buy furnace filter" in out, "Renew car" in out, "Tailscale" in out),
      (True, True, False, False))
check("an adult can't write to Claude's list", T("add_todo", {"title": "x", "for": "claude"}), "Not done: Only the household's admin keeps a list for Claude.")
as_("kid", leo, "Leo")
check("a kid adds to their own", T("add_todo", {"title": "Library book"}), "Added “Library book” to your list.")
check("…and the household's", T("add_todo", {"title": "Out of milk", "for": "household"}), "Added “Out of milk” to the household's list.")
check("…not someone else's", T("add_todo", {"title": "x", "for": "Robin"}), "Not done: You can add to your own list or the household's.")
as_("household")
check("a household device: the household list", T("add_todo", {"title": "Water plants"}), "Added “Water plants” to the household's list.")

print("\n── done ──")
as_("admin", "zach", "Zach")
check("by title", T("complete_todo", {"todo": "furnace filter"}), "Marked “Buy furnace filter” done.")
check("it's off the open list", "furnace" in T("list_todos", {"for": "household"}), False)
check("…and shows with include_done", "✓ done" in T("list_todos", {"for": "household", "include_done": "true"}), True)
check("nothing matching", T("complete_todo", {"todo": "zzz"}).startswith("Not done: no open todo"), True)
T("add_todo", {"title": "Fix gate latch"}); T("add_todo", {"title": "Fix gate hinge"})
check("several match → ask which", T("complete_todo", {"todo": "fix gate"}).startswith("Several todos match"), True)
as_("adult", rob, "Robin")
check("Robin can't complete Zach's", T("complete_todo", {"todo": "Renew car registration"}).startswith("Not done"), True)
os.environ["AGENT_TURN_SOURCE"] = "eval"
check("an eval turn adds nothing", (T("add_todo", {"title": "Eval todo"}).startswith("Test turn"),
                                    "Eval todo" in T("list_todos", {})), (True, False))
os.environ["AGENT_TURN_SOURCE"] = "user"
check("the plan_task description points at add_todo", "not a simple todo: use add_todo" in lib._TOOL_REGISTRY["plan_task"]["description"], True)
as_(None)

print("\n── never a quiet second one (10-03 audit, #802's shape) ──")
as_("adult", rob, "Robin")
T("add_todo", {"title": "Renew the library card"})
n0 = len(todos.list_for([rob]))
check("a similar open todo: not added, naming it", (T("add_todo", {"title": "renew library card"}), len(todos.list_for([rob]))),
      ("Not added: “Renew the library card” is already open on that list. If it's the same thing, say so (mark the old one "
       "done with complete_todo first to replace it). If the person wants another one as well, ask them, then call again "
       "with new='yes'.", n0))
check("…a shorter wording too", T("add_todo", {"title": "Library card"}).startswith("Not added: “Renew the library card”"), True)
T("add_todo", {"title": "Oat milk"})
check("a one-word item isn't a near match for a longer one (grocery lists)", (T("add_todo", {"title": "milk"}),
      T("add_todo", {"title": "AA batteries"}), T("add_todo", {"title": "batteries"})),
      ("Added “milk” to your list.", "Added “AA batteries” to your list.", "Added “batteries” to your list."))
check("…but the same word again is", T("add_todo", {"title": "Milk"}).startswith("Not added: “milk” is already open"), True)
check("a different thing is added", T("add_todo", {"title": "Return the library book"}),
      "Added “Return the library book” to your list.")
check("another list's todo isn't a match", T("add_todo", {"title": "Renew the library card", "for": "household"}),
      "Added “Renew the library card” to the household's list.")
check("new='yes' adds another", T("add_todo", {"title": "Renew the library card", "new": "yes"}),
      "Added “Renew the library card” to your list.")

print("\n── the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}
s = c.post("/v1/pair/start", json={"new_person": {"name": "Ava", "role": "kid"}}, headers=M).json()
KID = {"X-Agent-Token": c.post("/v1/pair/claim", json={"code": s["code"], "device_name": "Ava's iPad", "platform": "ios"}).json()["token"]}
r = c.post("/v1/todos", json={"title": "Ship live cards", "for": "claude"}, headers=M)
check("POST to Claude's list (admin)", (r.status_code, r.json()["todo"]["for"], r.json()["todo"]["created_by"]), (200, "claude", "Zach"))
check("GET ?for=claude is what the Claude sessions read", [t["title"] for t in c.get("/v1/todos?for=claude", headers=M).json()["todos"]],
      ["Tailscale in phone setup", "Ship live cards"])
check("a kid can't read Claude's list", c.get("/v1/todos?for=claude", headers=KID).json()["todos"], [])
check("a kid can't post to it", c.post("/v1/todos", json={"title": "x", "for": "claude"}, headers=KID).status_code, 403)
r = c.post("/v1/todos", json={"title": "Science project"}, headers=KID)
check("a kid posts to their own", (r.status_code, r.json()["todo"]["created_by"]), (200, "Ava"))
tid = r.json()["todo"]["id"]
check("the admin can't mark the kid's own done (not his list)", c.post(f"/v1/todos/{tid}/done", headers=M).json(), {"done": False})
check("the kid can", c.post(f"/v1/todos/{tid}/done", headers=KID).json(), {"done": True})
check("no title → 400", c.post("/v1/todos", json={"for": "me"}, headers=M).status_code, 400)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
