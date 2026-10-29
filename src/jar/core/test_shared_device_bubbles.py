#!/usr/bin/env python3
# gate: hermetic
"""#418 step 1: a shared device (an Apple TV, the hub) is the household's, never a person: anything it observes lands in
the household bubble with the household's audience, never in a bubble named after the screen (which nobody would read).
A person's own observations are untouched. (WORLD_SHARED_DEVICE_ROUTING, on since 10-07, was removed: it's always so.) Fictional household (Robin an adult, Ava a kid, a living-room TV); temporary stores.
Run: python3 scripts/test_shared_device_bubbles.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-shared-dev-"))
for k, v in {"WORLD_DIR": "world", "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import household_identity as hh  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(st)
robin = st.add_person("Robin Hale", "adult")["id"]
ava = st.add_person("Ava Hale", "kid")["id"]
tv = st.add_person("Living room", "household")["id"]


def obs(entity, owner, bubble, audience="private"):
    return W.Obs(entity=entity, etype="org", name=entity.replace("-", " ").title(), attr="phone", value="(555) 0100",
                 observed_at="2026-10-01", kind="household", ref=f"dev#{entity}", origin=f"device:{owner}",
                 label="a device", owner=owner, audience=audience, bubble=bubble)


w2 = W.Bubbles(_TMP / "w-on")
w2.sync("tv-collector", [obs("harbor-cinema", tv, tv), obs("pine-shop", robin, robin)])
check("the TV's observation is the household's", w2.find("Harbor Cinema", ava, "kid"), ["household:harbor-cinema"])
check("…no bubble is made for the screen", W.bubble_name(tv) in w2.bubbles(), False)
check("…a person's own observation stays in their own bubble", (w2.find("Pine Shop", robin, "adult"),
                                                                 w2.find("Pine Shop", ava, "kid")), (["{}:pine-shop".format(
                                                                     W.bubble_name(robin))], []))
check("the shared-device ids are the household-role people only", W.shared_device_ids() >= {tv.lower()} and
      robin.lower() not in W.shared_device_ids(), True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
