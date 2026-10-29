#!/usr/bin/env python3
# gate: hermetic
"""Listing projects by asking (#1646, list_projects): the asker's own, the ones shared with them, their own archived
ones; names only, wrapped as people's text; never another person's private project; no counts (a shared project's
filings include other people's items). Fictional people (Alex the admin, Robin an adult, Ava a kid); temporary
stores. Run: python3 scripts/test_list_projects.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-list-projects-"))
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


def as_person(pid, role="admin"):
    lib._TURN_IO.person = {"id": pid, "name": pid} if pid else None
    lib._TURN_IO.role = role
    lib._TURN_IO.source = "user"
    lib._TURN_IO.capability_tiers = None


S.create_project("alex", "admin", "Kitchen")
trip = S.create_project("alex", "admin", "Trip", audience="household")
old = S.create_project("alex", "admin", "Old taxes")
S.update_project(old["id"], "alex", "admin", archived=True)
S.create_project("robin", "adult", "Garden")
S.create_project("robin", "adult", "Surprise party")   # Robin's private
projects.file("robin", "adult", "todo:9", trip["id"])  # Robin's item in Alex's shared Trip

as_person("alex")
out = lib._execute_tool("list_projects", {})
check("Alex: theirs, then their archived; wrapped as people's text",
      ("Yours: Kitchen, Trip" in out, "Archived (yours, can be brought back): Old taxes" in out,
       out.index(lib._TURN_IO.untrusted_nonce) < out.index("Kitchen")), (True, True, True))
check("…never Robin's private projects", ("Garden" in out, "Surprise party" in out), (False, False))
check("…and no counts (Robin's item in Trip isn't hinted at)", any(ch.isdigit() for ch in out.split(lib._TURN_IO.untrusted_nonce)[1]
                                                                   .replace(lib._TURN_IO.untrusted_nonce, "")), False)
as_person("robin", "adult")
out = lib._execute_tool("list_projects", {})
check("Robin: theirs, and Alex's Trip as shared with them; not Alex's private Kitchen",
      ("Yours: Garden, Surprise party" in out, "Shared with you: Trip" in out, "Kitchen" in out), (True, True, False))
as_person("ava", "kid")
out = lib._execute_tool("list_projects", {})
check("Ava (a kid, none of her own): the household's Trip as shared with her, nobody's private ones",
      ("Shared with you: Trip" in out, "Yours:" in out, any(x in out for x in ("Kitchen", "Garden", "Surprise"))),
      (True, False, False))
check("offered to adults, the admin and kids (their own), never a shared screen",
      [(as_person(p, r), lib._role_allows("list_projects"))[1] for p, r in (("alex", "admin"), ("robin", "adult"),
                                                                           ("ava", "kid"), (None, "household"))],
      [True, True, True, False])
import tool_descriptors as td  # noqa: E402
check("a read in the projects domain", (td.lookup("list_projects").domain, td.lookup("list_projects").risk), ("projects", "read"))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
