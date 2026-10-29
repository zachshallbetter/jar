#!/usr/bin/env python3
# gate: hermetic
"""#1490 step 1: what each of a person's mail accounts is for, by titan's label for it (never an address). A facet use
lives in their own facet map ("mailbox:<label>"); a project use is a `mailbox` claim on one of their own projects.
world.mail_uses(owner) reads both, for that owner only: another person's map or projects never apply, a shared screen
has none, and nothing about who sees anything changes. Fictional household (Alex admin, Robin adult, Ava kid, a
screen); temporary stores.
Run: python3 scripts/test_mail_uses.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mail-uses-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "WORLD_DIR": "world", "FENCES_DB": "fences.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.setdefault("AGENT_TZ", "America/Los_Angeles")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import household_identity as hh  # noqa: E402
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
AVA = st.add_person("Ava Quarry", "kid")["id"]
SCREEN = st.add_person("Living room", "household")["id"]
B = W.Bubbles(_TMP / "world")
people = lambda: st.people()  # noqa: E731

print("── the facet map takes mailbox keys ──")
st.set_facets(ALEX, {"mailbox:Personal-iCloud": "personal", "mailbox:school-portal": "school"}, merge=True)
check("stored lowercased, beside calendars and vendors",
      {k: v for k, v in st.facets(ALEX).items() if k.startswith("mailbox:")},
      {"mailbox:personal-icloud": "personal", "mailbox:school-portal": "school"})
try:
    st.set_facets(ALEX, {"inbox:x": "work"}, merge=True)
    refused = False
except hh.PairingError:
    refused = True
check("any other key kind is still refused", refused, True)

print("── a project's mail account ──")
done, _s, _w = W.set_fact(B, "Harbor Works", "role", "client project", ALEX, "admin", etype="project", people=people(),
                          facet="work")
done2, _s2, why = W.set_fact(B, "Harbor Works", "mailbox", "Harbor-Outlook", ALEX, "admin", people=people())
check("recorded as a claim on his project", (done, done2, why), (True, True, "ok"))
uses = W.mail_uses(ALEX, B, people())
proj = (B.find("Harbor Works", ALEX, "admin") or [None])[0]
check("mail_uses: the project's account files as work, under that project; facet uses from his map",
      uses, {"harbor-outlook": {"facet": "work", "project": proj},
             "personal-icloud": {"facet": "personal", "project": None},
             "school-portal": {"facet": "school", "project": None}})
st.set_facets(ALEX, {"mailbox:harbor-outlook": "social"}, merge=True)
check("his facet map can refile a project's account (still that project)", W.mail_uses(ALEX, B, people())["harbor-outlook"],
      {"facet": "social", "project": proj})

print("── owner-only ──")
st.set_facets(ROBIN, {"mailbox:harbor-outlook": "personal", "mailbox:robin-mail": "personal"}, merge=True)
check("Robin's map never reaches Alex's accounts", W.mail_uses(ALEX, B, people())["harbor-outlook"]["facet"], "social")
check("…and Alex's project never reaches Robin's", W.mail_uses(ROBIN, B, people()),
      {"harbor-outlook": {"facet": "personal", "project": None}, "robin-mail": {"facet": "personal", "project": None}})
check("a shared screen has none", W.mail_uses(SCREEN, B, people()), {})
check("nobody: none", W.mail_uses("", B, people()), {})
check("the project itself is still only Alex's (who sees it never changed)",
      [B.find("Harbor Works", p, r) for p, r in ((ROBIN, "adult"), (AVA, "kid"), (None, "household"))], [[], [], []])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
