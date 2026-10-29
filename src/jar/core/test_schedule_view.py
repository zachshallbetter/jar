#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for schedule_view (jellybean#219, docs/schedule-api.md): who sees whose events.

    python3 scripts/test_schedule_view.py
"""
from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_view as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


PEOPLE = [{"id": "zach", "name": "Zach", "role": "admin", "calendars": []},
          {"id": "robin", "name": "Robin", "role": "adult", "calendars": ["Robin"]},
          {"id": "jordan", "name": "Jordan", "role": "adult", "calendars": []},
          {"id": "ava", "name": "Ava", "role": "kid", "parents": ["zach", "robin"], "calendars": ["Ava"]},
          {"id": "leo", "name": "Leo", "role": "kid", "parents": ["zach", "robin"], "calendars": []}]


def ev(nid, title, start, cal, owner="zach", aud="private"):
    return {"node_id": nid, "data": {"title": title, "start": start, "end": None, "calendar": cal,
                                     "provenance": {"owner": owner, "audience": aud}}}


NODES = [ev("a1", "Ballet", "2026-10-05T16:30:00", "Ava"),                 # Ava's calendar, synced by Zach's Mac
         ev("a2", "Take out garbage", "2026-10-07T20:00:00", "Family"),     # a household calendar
         ev("a3", "Dentist", "2026-10-06T09:00:00", "Work"),                # Zach's own
         ev("a4", "Coffee with Amanda", "2026-10-06T10:00:00", "Robin"),  # Robin's calendar, on Zach's Mac
         ev("a5", "Shift", "2026-10-05T12:00:00", "Home", owner="jordan"),  # Jordan's device, private
         ev("a2b", "Take out garbage", "2026-10-07T20:00:00", "Family", owner="jordan"),   # the same, twice
         ev("a6", "Later thing", "2026-10-20T09:00:00", "Family")]
HH = {"Family"}
W = (date(2026, 10, 5), date(2026, 10, 11))


def who(view):
    return {p["id"]: [e["title"] for e in p["events"]] for p in view["people"]} | \
        {"household": [e["title"] for e in view["household"]["events"]]}


check("Zach (a parent): his own, Ava's, Robin's calendar on his Mac, the household; not Jordan's",
      who(S.view(NODES, *W, "zach", "admin", PEOPLE, HH)),
      {"zach": ["Dentist"], "ava": ["Ballet"], "robin": ["Coffee with Amanda"], "household": ["Take out garbage"]})
check("Robin (a parent): Ava's and her own calendar, the household; not Zach's work or Jordan's",
      who(S.view(NODES, *W, "robin", "adult", PEOPLE, HH)),
      {"ava": ["Ballet"], "robin": ["Coffee with Amanda"], "household": ["Take out garbage"]})
check("Jordan: her own and the household only", who(S.view(NODES, *W, "jordan", "adult", PEOPLE, HH)),
      {"jordan": ["Shift"], "household": ["Take out garbage"]})
check("Ava: what's on her calendar, and the household", who(S.view(NODES, *W, "ava", "kid", PEOPLE, HH)),
      {"ava": ["Ballet"], "household": ["Take out garbage"]})
check("Leo: the household only", who(S.view(NODES, *W, "leo", "kid", PEOPLE, HH)),
      {"household": ["Take out garbage"]})
check("a shared household device: the household calendars only", who(S.view(NODES, *W, None, "household", PEOPLE, HH)),
      {"household": ["Take out garbage"]})
check("the people filter", who(S.view(NODES, *W, "zach", "admin", PEOPLE, HH, only={"ava"})), {"ava": ["Ballet"], "household": []})
check("only the window", "Later thing" in str(S.view(NODES, *W, "zach", "admin", PEOPLE, HH)), False)
v = S.view(NODES, *W, "zach", "admin", PEOPLE, HH)
check("each person row says who they are and whether it's you",
      [(p["id"], p["role"], p["you"]) for p in v["people"]], [("zach", "admin", True), ("robin", "adult", False),
                                                            ("ava", "kid", False)])
fence = lambda v_, r_, o, *, text, item: text != "Coffee with Amanda" or v_ == "robin"
check("a fence the event touches decides too: hidden from Zach, still Robin's",
      ("Coffee with Amanda" in str(S.view(NODES, *W, "zach", "admin", PEOPLE, HH, fence=fence)),
       "Coffee with Amanda" in str(S.view(NODES, *W, "robin", "adult", PEOPLE, HH, fence=fence))), (False, True))

# managed_by (jellybean#206/#172): who runs it, per event.
def evo(nid, title, start, cal, owner="zach", organizer=None, is_me=None):
    e = ev(nid, title, start, cal, owner=owner)
    if organizer is not None:
        e["data"]["organizer"] = organizer
    if is_me is not None:
        e["data"]["organizer_is_me"] = is_me
    return e


MNODES = [evo("m1", "Therapy", "2026-10-06T15:00:00", "Ava", organizer="Robin Hale", is_me=False),   # booked by Robin
          evo("m2", "Ballet", "2026-10-05T16:30:00", "Ava", organizer="Zach Hale", is_me=True),        # booked by Zach
          evo("m3", "Ballet recital", "2026-10-08T18:00:00", "Ava"),                                          # no organizer
          evo("m4", "Garbage", "2026-10-07T20:00:00", "Family", organizer="Robin", is_me=False),            # household calendar
          evo("m5", "Dentist", "2026-10-06T09:00:00", "Work", organizer="Zach", is_me=True),                  # Zach's own
          evo("m6", "School board", "2026-10-06T19:00:00", "Work", organizer="Pat Jones", is_me=False),       # outside the household
          evo("m7", "Lunch with Robin", "2026-10-09T12:00:00", "Work", organizer="robin", is_me=False),   # first name
          evo("m8", "Coffee", "2026-10-06T10:00:00", "Robin", organizer="Zach", is_me=False),               # Robin's calendar
          evo("m9", "Errand", "2026-10-10T10:00:00", "Work")]                                                  # no organizer at all
PEOPLE2 = [dict(p, name=("Robin Hale" if p["id"] == "robin" else p["name"])) for p in PEOPLE]
mv = S.view(MNODES, *W, "zach", "admin", PEOPLE2, HH)
mb = {e["title"]: (e["managed_by"], e["managed_by_you"]) for row in mv["people"] for e in row["events"]} | \
     {e["title"]: (e["managed_by"], e["managed_by_you"]) for e in mv["household"]["events"]}
check("Ava's therapy Robin booked is Robin's to run, not Zach's (the custody case)", mb["Therapy"], ("robin", False))
check("Ava's ballet Zach booked is his", mb["Ballet"], ("zach", True))
check("a kid's event with no organizer: the kid", mb["Ballet recital"], ("ava", False))
check("a household calendar: the household's", mb["Garbage"], ("household", False))
check("his own event he organized: his", mb["Dentist"], ("zach", True))
check("organized by someone outside the household: nobody here", mb["School board"], (None, False))
check("an organizer named by first name only (one person has it)", mb["Lunch with Robin"], ("robin", False))
check("an adult's calendar stays theirs", mb["Coffee"], ("robin", False))
check("no organizer info: the person whose device synced it", mb["Errand"], ("zach", True))
mm = S.view(MNODES, *W, "robin", "adult", PEOPLE2, HH)
mbm = {e["title"]: e["managed_by_you"] for row in mm["people"] for e in row["events"]}
check("…and for Robin, the therapy is hers to run", mbm.get("Therapy"), True)
check("an ambiguous first name matches nobody", S._member("Ava Smith", PEOPLE2 + [{"id": "kate2", "name": "Ava Other"}]), None)
check("a whole name wins over a shared first name", S._member("Ava Other", PEOPLE2 + [{"id": "kate2", "name": "Ava Other"}]), "kate2")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
