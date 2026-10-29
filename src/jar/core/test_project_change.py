#!/usr/bin/env python3
# gate: hermetic
"""Organize a project by asking (#1646, project_change; spaces.update_project's unarchive): rename one of the asker's own
projects (named by them from then on), archive it, bring it back; never another person's, never its audience; a test
turn changes nothing; adults and the admin only. Fictional people (Alex the admin, Robin an adult); temporary stores.
Run: python3 scripts/test_project_change.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-project-change-"))
os.environ.update({"SPACES_DB": str(_TMP / "spaces.db"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "CAPABILITY_SWITCHES": str(_TMP / "switches.json"), "PROACTIVE_DB": str(_TMP / "proactive.db"),
                   "MCP_SERVERS": str(_TMP / "none.json"), "AGENT_TURN_SOURCE": "user"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import spaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


a = S.create_project("alex", "admin", "Kitchen")
r = S.create_project("robin", "adult", "Kitchen")
S.create_project("alex", "admin", "Taxes", audience="household")

print("── the store: archiving is undone ──")
S.update_project(a["id"], "alex", "admin", archived=True)
check("archived: gone from their list, kept", ([p["name"] for p in S.projects_for("alex", "admin") if p["owner"] == "alex"],
                                               [p["id"] for p in S.archived_projects("alex")]), (["Taxes"], [a["id"]]))
S.update_project(a["id"], "alex", "admin", archived=False)
check("archived false brings it back", "Kitchen" in [p["name"] for p in S.projects_for("alex", "admin") if p["owner"] == "alex"],
      True)

print("── the tool ──")
import agent_runtime_lib as lib  # noqa: E402
import projects  # noqa: E402


def as_person(pid, role="admin", source="user"):
    lib._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    lib._TURN_IO.role = role
    lib._TURN_IO.source = source
    lib._TURN_IO.capability_tiers = None


as_person("alex")
out = lib._execute_tool("project_change", {"project": "kitchen", "rename_to": "Kitchen remodel"})
check("Alex renames their own Kitchen (any case)", (out, S.get_project(a["id"])["name"]),
      ("Done: renamed it to Kitchen remodel.", "Kitchen remodel"))
check("…named by them from then on", S.get_project(a["id"])["name_source"], "person")
check("…Robin's Kitchen untouched", S.get_project(r["id"])["name"], "Kitchen")
out = lib._execute_tool("project_change", {"project": "Kitchen remodel", "archive": True})
check("archive: done, and it says how to bring it back", (out.startswith("Done: archived it"), S.get_project(a["id"])), (True, None))
out = lib._execute_tool("project_change", {"project": "Kitchen remodel", "archive": "false"})
check("archive false: back", (out, S.get_project(a["id"])["name"]), ("Done: brought it back.", "Kitchen remodel"))
out = lib._execute_tool("project_change", {"project": "Garage", "rename_to": "x"})
check("a name that isn't theirs: refused, their own names wrapped", (out.startswith("Not done: none of your projects"),
                                                                     lib._TURN_IO.untrusted_nonce in out), (True, True))
as_person("robin", "adult")
out = lib._execute_tool("project_change", {"project": "Taxes", "archive": True})
check("Robin can't touch Alex's Taxes (shared with the household, still Alex's)",
      (out.startswith("Not done"), sorted(p["name"] for p in S.projects_for("alex", "admin") if p["owner"] == "alex")),
      (True, ["Kitchen remodel", "Taxes"]))
check("the audience is never changed: Taxes is still the household's",
      S.get_project([p for p in S.projects_for("alex", "admin") if p["name"] == "Taxes"][0]["id"])["audience"], "household")
as_person("alex", source="eval")
check("an eval turn changes nothing", (lib._execute_tool("project_change", {"project": "Taxes", "archive": True}),
                                       "Taxes" in [p["name"] for p in S.projects_for("alex", "admin")]),
      ("Test turn: nothing changed.", True))
check("offered to the admin and adults, never a kid or a shared screen",
      [(as_person(p, r), lib._role_allows("project_change"))[1] for p, r in (("alex", "admin"), ("robin", "adult"),
                                                                             ("ava", "kid"), (None, "household"))],
      [True, True, False, False])
as_person(None, "admin")
check("no person asking: nothing", lib._project_change({"project": "Taxes", "archive": True}).startswith("Not done"), True)
import tool_descriptors as td  # noqa: E402
d = td.lookup("project_change")
check("a write in its own domain, undone by itself, nothing leaves", (d.domain, d.risk, d.undo, d.egress),
      ("projects", "write", "project_change again", ()))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
