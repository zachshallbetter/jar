#!/usr/bin/env python3
# gate: hermetic   (temp world and people stores; nothing live)
"""#805 (#802's shape): set_fact made a second entity when the name missed and the model passed `type`, splitting
the facts ("Hale Plumbng"'s new number went to a new private entity; Hale Plumbing kept the old one). The tool now
refuses a near name, naming the one it has, unless new='yes'. Seeding and memory extraction are unchanged.
Run: python3 scripts/test_set_fact_near.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-setfact-near-"))
os.environ.update({"WORLD_DIR": str(_TMP / "world"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "AGENT_TZ": "America/Los_Angeles", "AGENT_TURN_SOURCE": "test"})
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.get_store()
robin = st.add_person("Robin Hale", "adult")["id"]
jordan = st.add_person("Jordan Hale", "adult")["id"]
PEOPLE = st.people()
w = W.get()
W.set_fact(w, "Hale Plumbing", "phone", "555-0101", robin, "adult", etype="org", people=PEOPLE, household=True)
W.set_fact(w, "Dr Ava Stone", "phone", "555-0202", jordan, "adult", etype="person", people=PEOPLE)   # Jordan's own


def T(args, who=robin, role="adult"):
    lib._TURN_IO.person, lib._TURN_IO.role = {"id": who, "name": "x"}, role
    try:
        return lib.execute_tool("set_fact", args)
    finally:
        lib._TURN_IO.person = lib._TURN_IO.role = None


def count():
    return len(W.get().entities())


print("── the world function ──")
check("near: words inside either way, titles aside, a typo", [n for _, n in W.similar_entities(w, "Hale Plumbng", robin, "adult")]
      + [n for _, n in W.similar_entities(w, "plumbing", robin, "adult")], ["Hale Plumbing", "Hale Plumbing"])
check("…never what this person can't see (Jordan's own)", W.similar_entities(w, "Ava Stone", robin, "adult"), [])
check("…a different name isn't near", W.similar_entities(w, "Maple Dental", robin, "adult"), [])
n0 = count()
check("seeding and extraction don't check (near_check off by default)",
      W.set_fact(w, "Hale Plumbng", "phone", "555-0303", robin, "adult", etype="org", people=PEOPLE)[2], "ok")
n0 = count()

print("── the tool ──")
out = T({"entity": "Hale Plumbin", "attribute": "phone", "value": "555-0404", "type": "org"})
check("a near-miss name with type: not recorded, naming the one titan has",
      (out.startswith("Not recorded: I have “Hale Plumbing”"), count()), (True, n0))
out = T({"entity": "Hale Plumbing", "attribute": "phone", "value": "555-0404"})
check("the right name updates it", out.startswith("Recorded."), True)
out = T({"entity": "Maple Dental", "attribute": "phone", "value": "555-0505", "type": "org"})
check("a new name is recorded", (out.startswith("Recorded (new: Maple Dental)"), count()), (True, n0 + 1))
out = T({"entity": "Ava Stone", "attribute": "phone", "value": "555-0606", "type": "person"})
check("another person's private entity doesn't block (or leak)", ("Dr Ava Stone" in out, out.startswith("Recorded")), (False, True))
check("a near name is refused…", T({"entity": "Plumbing", "attribute": "phone", "value": "555-0707", "type": "org"})
      .startswith("Not recorded: I have “Hale Plumbing”"), True)
out = T({"entity": "Plumbing", "attribute": "phone", "value": "555-0707", "type": "org", "new": "yes"})
check("…and new='yes' makes it a separate one", out.startswith("Recorded (new: Plumbing)"), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
