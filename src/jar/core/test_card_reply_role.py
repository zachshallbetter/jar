#!/usr/bin/env python3
# gate: hermetic
"""A card reply's agent turn runs with the person's own role (proactive._agent_turn): a trigger that carried no role
used to read as "adult", so a kid's reply could see what only adults may (review on #1735). Now the role comes from
the household store; someone it doesn't know gets a kid's, the narrowest. Fictional people (Robin an adult, Ava a
kid); the agent turn stubbed. Run: python3 scripts/test_card_reply_role.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-card-reply-role-"))
os.environ.update({"HOUSEHOLD_DB": str(_TMP / "household.db"), "PROACTIVE_DB": str(_TMP / "proactive.db"),
                   "SPACES_DB": str(_TMP / "spaces.db"), "AGENT_TURN_SOURCE": "test"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


store = hh.HouseholdStore(_TMP / "store.db")
hh.reset_store(store)
robin = store.add_person("Robin Hale", "adult")["id"]
ava = store.add_person("Ava Hale", "kid")["id"]
SEEN = []
lib._run_agent_turn = lambda *a, **k: SEEN.append(lib._TURN_IO.role) or "ok"


def role_for(person, role=None):
    proactive._agent_turn("a card reply", person, role)
    return SEEN[-1]


check("Ava's reply with no role: a kid's turn, never an adult's", role_for(ava), "kid")
check("Robin's with no role: an adult's", role_for(robin), "adult")
check("someone the store doesn't know: a kid's, the narrowest", role_for("p_nobody"), "kid")
check("a role the trigger carried is kept", role_for(robin, "adult"), "adult")
real = hh.get_store
hh.get_store = lambda: (_ for _ in ()).throw(OSError("store down"))
check("the store can't be read: a kid's", role_for(robin), "kid")
hh.get_store = real
check("…and the turn's person and role are cleared after", (lib._TURN_IO.person, lib._TURN_IO.role), (None, None))
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
