#!/usr/bin/env python3
# gate: hermetic
"""What others see of yours (GET /v1/me/sharing): only ever the caller's own items, counted by audience, never
another person's and never a title; a kid's place is their parents'; a shared screen has nothing of its own.
Fictional household (Alex, Robin, Ava); temporary stores.
Run: python3 scripts/test_me_sharing.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-me-sharing-test-"))
for k, v in {"WORLD_DIR": "world", "WORLD_DB": "world.db", "HOUSEHOLD_DB": "household.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import me_sharing as M  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


PEOPLE = [{"id": "alex", "name": "Alex Hale", "role": "admin"}, {"id": "p_robin", "name": "Robin Hale", "role": "adult"},
          {"id": "p_ava", "name": "Ava Hale", "role": "kid", "parents": ["alex", "p_robin"]}]
ACL = {"note-a1": ("alex", "private"), "note-a2": ("alex", "household"), "note-a3": ("alex", '["p_robin"]'),
       "note-r1": ("p_robin", "private"), "note-r2": ("p_robin", "household"), "note-r3": ("p_robin", "adults"),
       "note-v1": ("p_ava", "private")}
w = W.Bubbles(_TMP / "world")
W.set_fact(w, "Maple Dental", "phone", "555-0100", "alex", "admin", etype="org", people=PEOPLE, household=True)
W.set_fact(w, "Reading club", "schedule", "every Tuesday", "alex", "admin", etype="activity", people=PEOPLE)
W.set_fact(w, "Garden plot", "schedule", "every Saturday", "p_robin", "adult", etype="activity", people=PEOPLE)
G = lambda person: [] if person != "alex" else [{"asker_first_name": "Robin", "kind": "scan"}]  # noqa: E731

a = M.summary("alex", "admin", PEOPLE, acl_rows=ACL, w=w, grants=G("alex"))
r = M.summary("p_robin", "adult", PEOPLE, acl_rows=ACL, w=w, grants=G("p_robin"))
v = M.summary("p_ava", "kid", PEOPLE, acl_rows=ACL, w=w, grants=G("p_ava"))
print("── only ever the caller's own ──")
check("Alex's notes by audience", a["notes"], {"private": 1, "household": 1, "adults": 0, "admins": 0, "named": 1})
check("Robin's notes: hers only", r["notes"], {"private": 1, "household": 1, "adults": 1, "admins": 0, "named": 0})
check("Alex's world facts: one household, one his own", (a["facts"]["household"], a["facts"]["private"]), (1, 1))
check("Robin's facts don't count Alex's", (r["facts"]["household"], r["facts"]["private"]), (0, 1))
check("the kid's: their own note only", v["notes"]["private"], 1)
print("── places ──")
check("an adult's place: only them", a["places"], {"seen_by": "only you", "names": []})
check("a kid's place: their parents, by first name", v["places"], {"seen_by": "parents", "names": ["Alex", "Robin"]})
print("── grants, and nothing named ──")
check("the caller's own grants", (a["grants"], r["grants"]), ([{"asker_first_name": "Robin", "kind": "scan"}], []))
out = json.dumps([a, r, v])
check("no titles, ids or values anywhere", [x for x in ("note-", "Maple", "555", "Reading", "Garden", "Tuesday") if x in out], [])
print("── only notes still in the KB ──")
import agent_runtime_lib as _lib  # noqa: E402
import kb_privacy  # noqa: E402
asked = []
_ks, _ar = _lib.kb_sources, kb_privacy.acl_rows
_lib.kb_sources = lambda *a, **k: [{"source_id": "note-1"}, {"source_id": "note-2"}]
kb_privacy.acl_rows = lambda ids=None: (asked.append(sorted(ids) if ids is not None else None),
                                        {"note-1": ("alex", "private"), "note-2": ("alex", "household")})[1]
try:
    got = M.notes("alex")
finally:
    _lib.kb_sources, kb_privacy.acl_rows = _ks, _ar
check("an access row whose note is gone isn't counted: only live ids are asked for",
      (asked, got["private"], got["household"]), ([["note-1", "note-2"]], 1, 1))
print("── a shared screen, and the route ──")
for args in ((None, "admin"), ("house", "household")):
    try:
        M.summary(args[0], args[1], PEOPLE, acl_rows=ACL, w=w, grants=[])
        check(f"{args}: refused", "summarised", 403)
    except M.Refused as e:
        check(f"{args}: refused", e.status, 403)
import household_identity as hh  # noqa: E402
check("adults, kids and the admin may read it; a household screen may not",
      [hh.route_allowed(x, "GET", "/v1/me/sharing") for x in ("admin", "adult", "kid", "household")], [True, True, True, False])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
