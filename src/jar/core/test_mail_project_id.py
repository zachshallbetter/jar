#!/usr/bin/env python3
# gate: hermetic
"""mail_server.project_id (#955, #1490): a keep rule names its project by name; on titan that resolves to the owner's
OWN project entity in the real world model, never another bubble's, never an entity that isn't a project, and a name
with no project yet resolves to nothing (the thread then files by its facet alone). Built on a real world model in a
temporary WORLD_DIR (KB's review of #1494: a fake hid that the entities column is "type"). Fictional household.
Run: python3 scripts/test_mail_project_id.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mail-project-id-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "WORLD_DIR": "world", "FENCES_DB": "fences.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import household_identity as hh  # noqa: E402
import mail_server as M  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.HouseholdStore(_TMP / "household.db")
hh.reset_store(st)
ALEX = hh.ADMIN_PERSON_ID
ROBIN = st.add_person("Robin Quarry", "adult")["id"]
B = W.Bubbles(_TMP / "world")
people = lambda: st.people()  # noqa: E731

ok1 = W.set_fact(B, "Harbor Works", "role", "client project", ALEX, "admin", etype="project", people=people(), facet="work")[0]
ok2 = W.set_fact(B, "Dockside Partners", "role", "client", ALEX, "admin", etype="org", people=people())[0]
ok3 = W.set_fact(B, "Lighthouse Build", "role", "client project", ROBIN, "adult", etype="project", people=people(),
                 facet="work")[0]
check("the fixture: Alex's project, Alex's org, Robin's project (all recorded)", (ok1, ok2, ok3), (True, True, True))

pid = M.project_id(ALEX, "Harbor Works", B)
check("Alex's own project, by its exact name: its <bubble>:<entity> id", (pid or "").startswith(W.bubble_name(ALEX) + ":"), True)
check("…and that id is a project entity (the entities column is \"type\")", B.entity(pid)["type"] if pid else None, "project")
check("an org of Alex's is never a project", M.project_id(ALEX, "Dockside Partners", B), None)
check("Robin's project never resolves for Alex", M.project_id(ALEX, "Lighthouse Build", B), None)
check("…but does for Robin, in Robin's bubble", (M.project_id(ROBIN, "Lighthouse Build", B) or "").startswith(W.bubble_name(ROBIN) + ":"),
      True)
check("a name with no project yet: nothing (the thread files by its facet alone)", M.project_id(ALEX, "Nothing Here", B), None)
check("an exact name only, never a part of one", M.project_id(ALEX, "Harbor", B), None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
