#!/usr/bin/env python3
# gate: hermetic
"""What's in a project, by asking (#1646, project_items): the turn's own person sees exactly what their app's project
page shows (agent_api_server.items_view_for, the same code as GET /v1/items?view=project:), never another person's
private item filed in a shared project; a project they can't see is refused like a missing one; titles wrapped.
Fictional people (Alex the admin, Robin an adult); a fixed clock; temporary stores. Run: python3 scripts/test_project_items.py"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

T0 = 1791565200.0   # 2026-10-09T15:00:00Z, fixed for every store and the route
_real_time = time.time
time.time = lambda: T0
_TMP = Path(tempfile.mkdtemp(prefix="titan-project-items-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world",
             "LINKS_DB": "links.db", "LINK_SALT_FILE": "link_salt", "FENCES_DB": "fences.db", "TASKS_DB": "tasks.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "PUSH_DB": "push.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "ITEM_REFS_DB": "item_refs.db", "SURFACES_DB": "surfaces.db", "ITEMS_LATENCY_LOG": "items_latency.jsonl",
             "MAIL_BILLS_DB": "mail_bills.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import projects  # noqa: E402
import scheduled  # noqa: E402
import spaces  # noqa: E402
import todos  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _Z  # noqa: E402
lib._local_now = lambda: _dt.fromtimestamp(T0, _Z("America/Los_Angeles"))   # the route's clock, fixed too
lib.kb_sources = lambda *a, **k: []          # no KB runtime here: notes and mail are empty for everyone
lib.record_nodes = lambda *a, **k: ([], True)
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device", "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}, next(p["id"] for p in hh.get_store().people() if p["name"] == name)


ALEX, alex = pair("Alex Hale", "adult")
ROBIN, robin = pair("Robin Hale", "adult")
kitchen = spaces.create_project(alex, "adult", "Kitchen", audience="household")
garage = spaces.create_project(robin, "adult", "Garage")
mine = todos.add("Pick the grout colour", alex, created_by="Alex")
hers = todos.add("Surprise for Alex", robin, created_by="Robin")          # Robin's private to-do
shared = todos.add("Book the tiler", "household", created_by="Alex")
for t, who in ((mine, alex), (hers, robin), (shared, alex)):
    projects.file(who, "adult", f"todo:{t['id']}", kitchen["id"])


def as_person(pid, name, role="adult"):
    lib._TURN_IO.person = {"id": pid, "name": name}
    lib._TURN_IO.role = role
    lib._TURN_IO.source = "user"
    lib._TURN_IO.capability_tiers = None


def page_titles(headers, pid_):
    body = c.get(f"/v1/items?view=project:{pid_}", headers=headers).json()
    return sorted(str((m.get("slots") or {}).get("title")) for g in body.get("groups") or [] for m in g.get("members") or [])


as_person(alex, "Alex Hale")
out = lib._execute_tool("project_items", {"project": "kitchen"})
check("Alex: their Kitchen's items by title and kind, wrapped as people's text",
      ("Pick the grout colour (to-do)" in out, "Book the tiler (to-do)" in out, lib._TURN_IO.untrusted_nonce in out),
      (True, True, True))
check("…never Robin's private to-do filed there", "Surprise for Alex" in out, False)
check("…exactly what Alex's own page shows",
      sorted(x.rsplit(" (", 1)[0] for x in out.split(lib._TURN_IO.untrusted_nonce)[1].splitlines() if " (" in x),
      page_titles(ALEX, kitchen["id"]))
as_person(robin, "Robin Hale")
out = lib._execute_tool("project_items", {"project": "Kitchen"})
check("Robin (the household's project, shared with her): her own private to-do there, and the shared one",
      ("Surprise for Alex (to-do)" in out, "Book the tiler (to-do)" in out, "Pick the grout colour" in out),
      (True, True, False))
check("…exactly what Robin's own page shows",
      sorted(x.rsplit(" (", 1)[0] for x in out.split(lib._TURN_IO.untrusted_nonce)[1].splitlines() if " (" in x),
      page_titles(ROBIN, kitchen["id"]))
as_person(alex, "Alex Hale")
check("Robin's private Garage: refused like a missing one, nothing of it said",
      (lib._execute_tool("project_items", {"project": "Garage"}).startswith("Not done: say which project"),), (True,))
rk = spaces.create_project(robin, "adult", "Kitchen", audience="household")   # a second "Kitchen", Robin's, shared
projects.file(robin, "adult", f"todo:{todos.add('Order Robin cabinets', 'household', created_by='Robin')['id']}", rk["id"])
out = lib._execute_tool("project_items", {"project": "Kitchen"})
check("two Kitchens: Alex's own by default", ("Pick the grout colour" in out, "Order Robin cabinets" in out), (True, False))
out = lib._execute_tool("project_items", {"project": "Kitchen", "owner": "Robin"})
check("…Robin's by whose it is (review)", ("Order Robin cabinets" in out, "Pick the grout colour" in out), (True, False))
as_person(robin, "Robin Hale")
out = lib._execute_tool("project_items", {"project": "Kitchen"})
check("Robin, with her own Kitchen and Alex's: hers by default", "Order Robin cabinets" in out, True)
as_person(alex, "Alex Hale")
gone = spaces.create_project(alex, "adult", "Old porch")
spaces.update_project(gone["id"], alex, "adult", archived=True)
check("an archived project of theirs: says to bring it back first (review)",
      lib._execute_tool("project_items", {"project": "old porch"}),
      "Not done: that project of yours is archived. Bring it back first (project_change, archive false).")
import asyncio  # noqa: E402


async def _inside_a_loop():
    return lib._project_items({"project": "Kitchen"})
check("called with an event loop running (the API's thread): the same answer, read on a thread of its own",
      "Pick the grout colour" in asyncio.run(_inside_a_loop()), True)
check("an empty project: said plainly", (spaces.create_project(alex, "adult", "Attic"),
                                         lib._execute_tool("project_items", {"project": "Attic"}))[1],
      "Nothing is filed in that project yet.")
print("── splitting one by asking ──")
as_person(alex, "Alex Hale")
out = lib._execute_tool("project_split", {"project": "Kitchen", "items": ["pick the grout colour", "Book the tiler"],
                                          "new_name": "Tiling"})
check("Alex splits two of their Kitchen's items into Tiling, with the undo token",
      (out.startswith("Done: moved 2 items into a new project, Tiling"), "undo=" in out), (True, True))
f = projects.filings()
tiling = next(x for x in spaces.projects_for(alex, "adult") if x["name"] == "Tiling")
check("…both moved; Robin's private to-do stays in the Kitchen",
      (f.get(f"todo:{mine['id']}"), f.get(f"todo:{shared['id']}"), f.get(f"todo:{hers['id']}")),
      (tiling["id"], tiling["id"], kitchen["id"]))
check("…the new project is shared as the old one is", tiling["audience"], kitchen["audience"])
token = out.rsplit("undo=", 1)[1].rstrip(".")
check("…its undo puts them back", (lib._execute_tool("project_merge", {"undo": token}).startswith("Done: put back 2"),
                                   projects.filings().get(f"todo:{mine['id']}")), (True, kitchen["id"]))
out = lib._execute_tool("project_split", {"project": "Kitchen", "items": ["Surprise for Alex"], "new_name": "Peek"})
check("an item Alex can't see (Robin's private one) can't be named: nothing moves, the title wrapped",
      (out.startswith("Not done: no item there is titled"), lib._TURN_IO.untrusted_nonce in out,
       projects.filings().get(f"todo:{hers['id']}")), (True, True, kitchen["id"]))
as_person(robin, "Robin Hale")
check("Robin can't split Alex's Kitchen (shared with her)",
      lib._execute_tool("project_split", {"project": "Kitchen", "owner": "Alex", "items": ["Book the tiler"], "new_name": "X"}),
      "Not done: only its owner can split a project; that one is shared with you.")
as_person(alex, "Alex Hale")
lib._TURN_IO.source = "eval"
check("an eval turn changes nothing", (lib._execute_tool("project_split", {"project": "Kitchen", "items": ["Book the tiler"],
                                                                         "new_name": "Y"}),
                                       projects.filings().get(f"todo:{shared['id']}")), ("Test turn: nothing changed.", kitchen["id"]))
lib._TURN_IO.source = "user"
check("project_split: adults and the admin only", [(as_person(x, "x", r), lib._role_allows("project_split"))[1]
                                                   for x, r in ((alex, "adult"), ("ava", "kid"))], [True, False])
lib._TURN_IO.person, lib._TURN_IO.role = None, "household"
check("a shared screen: nothing", lib._project_items({"project": "Kitchen"}).startswith("Not done"), True)
check("offered to adults and kids, never a shared screen",
      [(as_person(p, "x", r), lib._role_allows("project_items"))[1] for p, r in ((alex, "adult"), ("ava", "kid"))], [True, True])
import tool_descriptors as td  # noqa: E402
check("a read in the projects domain", (td.lookup("project_items").domain, td.lookup("project_items").risk), ("projects", "read"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
