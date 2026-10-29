#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The truth eval's cases (#423), offline: what each new case accepts and refuses, from fixed replies.

    python3 scripts/test_truth_cases.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import tempfile

_TMP = tempfile.mkdtemp(prefix="titan-truth-cases-")
os.environ["WORLD_DB"] = os.path.join(_TMP, "world.db")      # never the live world model
os.environ["WORLD_DIR"] = os.path.join(_TMP, "world")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import truth_eval as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


NOW = datetime(2026, 10, 1, 10, 0, tzinfo=T.TZ)
PEOPLE = [{"id": "zach", "name": "Zach Hale", "role": "admin", "age": 44},
          {"id": "robin", "name": "Robin", "role": "adult", "age": None},
          {"id": "leo", "name": "Leo", "role": "kid", "age": 6},
          {"id": "ava", "name": "Ava", "role": "kid", "age": 12},
          {"id": "kitchen", "name": "Kitchen iPad", "role": "household"}]
cs = {c.name: c for c in T.cases(NOW, [], [], [], PEOPLE)}


def ok(name, reply):
    return cs[name].check(reply) == []


check("leave: 18 min before 9 AM is right", ok("leave_dentist", "Leave by 8:42 AM; it's 18 minutes by car."), True)
check("leave: with a margin is right", ok("leave_dentist", "Head out around 8:30 AM to be safe."), True)
check("leave: at 9 is late", ok("leave_dentist", "Your appointment is at 9:00 AM, leave at 9."), False)
check("leave: an hour early isn't what was asked", ok("leave_dentist", "Leave at 7:45 AM."), False)
check("roster: everyone by first name, not the shared iPad", cs["household_members"].expected, "Zach, Robin, Leo, Ava")
check("roster: a reply missing someone misses them", cs["household_members"].check("Zach, Leo and Ava live there."), ["missed Robin"])
check("age: only people with an age get a case", sorted(n for n in cs if n.startswith("age_")), ["age_ava", "age_leo", "age_zach"])
check("age: the age, or a year less before the birthday", (ok("age_leo", "Leo is 6."), ok("age_leo", "He's 5 until his birthday."),
                                                            ok("age_leo", "Leo is 7.")), (True, True, False))
check("no people known: no roster cases", [n for n in (c.name for c in T.cases(NOW, [], [], [], [])) if n.startswith(("age_", "household_"))], [])
check("the travel case uses the read-only travel_time", "travel_time" in T.READ_TOOLS, True)

# #423: when a note was saved (GET v1/kb's "saved"), asked as "When did I save my note …?"
from datetime import timedelta as _td  # noqa: E402
_saved = (NOW - _td(days=9)).strftime("%Y-%m-%d")
_kb = [{"id": "src-water-1", "title": "Water shutoff", "category": "household", "saved": _saved,
        "body": "The water shutoff is in the garage behind the shelf, next to the furnace."}]
_c = next((c for c in T.cases(NOW, [], _kb, [], PEOPLE) if c.name.startswith("kb_saved_")), None)
check("a saved note gets a when-saved case", _c is not None, True)
if _c:
    _d = NOW - _td(days=9)
    check("its month and day are right", _c.check(f"You saved it on {_d:%B} {_d.day}."), [])
    check("…the short month too", _c.check(f"Saved {_d:%b} {_d.day}."), [])
    check("a wrong day is a miss", bool(_c.check(f"You saved it on {_d:%B} {(_d.day % 27) + 1}.")), True)
_none = [c for c in T.cases(NOW, [], [{**_kb[0], "saved": None}], [], PEOPLE) if c.name.startswith("kb_saved_")]
check("no saved date: no case", _none, [])

# #1144: busy_tomorrow_330 passed a wrong "free at 3:30" reply ("no" matched inside "not scheduled")
busy = next(c for c in T.cases(NOW, [], [], [], PEOPLE) if c.name == "busy_tomorrow_330")
for label, reply, ok in (
        ("'free at 3:30 … not scheduled for anything else' fails (it was passing)",
         "You're free at 3:30 PM tomorrow; you're not scheduled for anything else after Piano lesson.", False),
        ("'No, busy: piano and soccer' passes", "No, you're busy: Piano lesson 3–4 PM and Leo soccer from 3:30.", True),
        ("'a conflict: piano overlaps soccer' passes", "You have a conflict: Piano lesson 3–4 PM overlaps Leo soccer.", True),
        ("'busy' without naming either event fails", "No, you're busy then.", False)):
    check(f"busy_tomorrow_330: {label}", busy.check(reply) == [], ok)

# #1144: --set wants_schedule keeps only the cases that get the schedule digest
_all = T.cases(NOW, [], [], [], PEOPLE)
_ws = [c for c in _all if T.in_set(c, "wants_schedule")]
check("--set wants_schedule: a proper, non-empty subset", 0 < len(_ws) < len(_all), True)
check("…conflicts_tomorrow is in it; the timeline question isn't", ("conflicts_tomorrow" in {c.name for c in _ws},
      any(c.category == "timeline" for c in _ws)), (True, False))
check("…--set all keeps every case", len([c for c in _all if T.in_set(c, "all")]), len(_all))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
