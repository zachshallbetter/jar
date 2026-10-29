#!/usr/bin/env python3
# gate: hermetic
"""Merging two projects by asking (#1646, project_merge; projects.merge): the asker's own two, by name; everything
filed in the first moves to the second and the first is archived; one undo token puts it all back; never another
person's project; a test turn changes nothing; adults and the admin only. Fictional people (Alex the admin, Robin an
adult); temporary stores. Run: python3 scripts/test_project_merge.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-project-merge-"))
os.environ.update({"SPACES_DB": str(_TMP / "spaces.db"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "CAPABILITY_SWITCHES": str(_TMP / "switches.json"), "PROACTIVE_DB": str(_TMP / "proactive.db"),
                   "MCP_SERVERS": str(_TMP / "none.json"), "AGENT_TURN_SOURCE": "user"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import projects  # noqa: E402
import spaces as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def as_person(pid, role="admin", source="user"):
    lib._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    lib._TURN_IO.role = role
    lib._TURN_IO.source = source
    lib._TURN_IO.capability_tiers = None


tiles = S.create_project("alex", "admin", "Tiles")
kitchen = S.create_project("alex", "admin", "Kitchen")
garden = S.create_project("robin", "adult", "Garden")
projects.file("alex", "admin", "todo:1", tiles["id"])
projects.file("alex", "admin", "todo:2", tiles["id"])
projects.file("alex", "admin", "todo:3", kitchen["id"])

as_person("alex")
out = lib._execute_tool("project_merge", {"project": "tiles", "into": "KITCHEN"})
check("Alex merges Tiles into Kitchen (any case): two moved, the token to undo it",
      (out.startswith("Done: moved 2 things into it"), "undo=" in out), (True, True))
f = projects.filings()
check("…both of Tiles' items are in Kitchen now; Tiles is archived",
      ((f.get("todo:1"), f.get("todo:2"), f.get("todo:3")), S.get_project(tiles["id"])),
      ((kitchen["id"],) * 3, None))
token = out.rsplit("undo=", 1)[1].rstrip(".")
as_person("robin", "adult")
check("Robin can't undo Alex's merge with Alex's token", lib._execute_tool("project_merge", {"undo": token}).startswith("Not done"),
      True)
as_person("alex")
out = lib._execute_tool("project_merge", {"undo": token})
f = projects.filings()
check("Alex's undo: everything back, Tiles back",
      (out, (f.get("todo:1"), f.get("todo:2")), S.get_project(tiles["id"])["name"]),
      ("Done: put back 2 items and brought the project back.", (tiles["id"],) * 2, "Tiles"))
out = lib._execute_tool("project_merge", {"project": "Tiles", "into": "Garden"})
check("into someone else's project (Robin's Garden): refused, their own names wrapped",
      (out.startswith("Not done: say two of your own projects"), lib._TURN_IO.untrusted_nonce in out, "Garden" in out),
      (True, True, False))
check("the same project twice: refused", lib._execute_tool("project_merge", {"project": "Tiles", "into": "tiles"}),
      "Not done: that's the same project twice.")
as_person("alex", source="eval")
check("an eval turn changes nothing", (lib._execute_tool("project_merge", {"project": "Tiles", "into": "Kitchen"}),
                                       projects.filings().get("todo:1")), ("Test turn: nothing changed.", tiles["id"]))
check("offered to the admin and adults, never a kid or a shared screen",
      [(as_person(p, r), lib._role_allows("project_merge"))[1] for p, r in (("alex", "admin"), ("robin", "adult"),
                                                                           ("ava", "kid"), (None, "household"))],
      [True, True, False, False])
import tool_descriptors as td  # noqa: E402
check("a write in the projects domain, undone by its token", (td.lookup("project_merge").domain, td.lookup("project_merge").risk),
      ("projects", "write"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
