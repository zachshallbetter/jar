#!/usr/bin/env python3
# gate: hermetic
"""Our year (jellybean#430, slice 1): the household's past year for the TV. Only household-calendar events (title and
place name), trips, milestones and household-visible birthdays; photo counts summed from people's own phones; never a
private item, never where anyone was, never the future.
Run: python3 scripts/test_our_year.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP = Path(tempfile.mkdtemp(prefix="titan-ouryear-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "OUR_YEAR_DB": "our_year.db", "AGENT_MEMORY_DIR": "memory",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "WORLD_DIR": "world", "PUSH_DB": "push.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
sys.path.insert(0, str(Path(__file__).parent))
import our_year as oy  # noqa: E402
import local_time  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.240} expected={expected!r:.240}")


TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 3, 19, 0, tzinfo=TZ)


def node(title, start, end=None, cal="Family", loc="", **kw):
    return {"data": {"title": title, "start": start, "end": end or start, "calendar": cal, "location": loc, **kw}}


NODES = [
    node("Fall festival", "2026-10-02T10:00:00-07:00", "2026-10-02T13:00:00-07:00", loc="Riverfront Park, Spokane WA"),
    node("Trip to the coast", "2026-08-14T00:00:00-07:00", "2026-08-17T00:00:00-07:00", loc="Cannon Beach", all_day=True),
    node("Ava's first day of school", "2026-09-02T08:00:00-07:00", "2026-09-02T09:00:00-07:00"),
    node("Therapy (Ava)", "2026-09-10T15:00:00-07:00", cal="Robin"),                 # a private calendar
    node("Dentist", "2026-09-11T09:00:00-07:00", cal="Work"),                        # someone's own
    node("Halloween party", "2026-10-31T18:00:00-07:00"),                            # still ahead
    node("See https://example.com/invite for details", "2026-10-01T18:00:00-07:00"),
    node("Pumpkin patch", "2025-10-03T10:00:00-07:00"),                              # a year ago today
    node("Fall festival", "2026-10-02T10:00:00-07:00", "2026-10-02T13:00:00-07:00", cal="family"),   # the same twice
]
HOUSE = {"family"}

print("── what's in a year: household moments only ──")
ms = oy.household_moments(NODES, HOUSE, TZ, 2026, NOW.date())
check("household-calendar events, past ones, once each", [(m["date"], m["title"], m["kind"]) for m in ms],
      [("2026-08-14", "Trip to the coast", "trip"), ("2026-09-02", "Ava's first day of school", "milestone"),
       ("2026-10-01", "See for details", "event"), ("2026-10-02", "Fall festival", "event")])
trip = ms[0]
check("a trip: its end and its place name", (trip["end"], trip["where"]), ("2026-08-16", "Cannon Beach"))
check("a place is the event's own name only, the first part", ms[-1]["where"], "Riverfront Park")
check("never a private calendar's or someone's own event", any(m["title"] in ("Therapy (Ava)", "Dentist") for m in ms), False)
check("never the future", any(m["title"] == "Halloween party" for m in ms), False)
check("never a link", any("http" in m["title"] for m in ms), False)
check("only titles, places and dates: no attendees, notes or calendar names",
      {k for m in ms for k in m} <= {"date", "end", "title", "kind", "where"}, True)

print("── birthdays the household may see ──")
bs = oy.birthday_moments([("Ava Hale", "2014-09-20"), ("Leo", "October 1, 2020"), ("Robin", "Nov 30"), ("Jordan", "soon")],
                         2026, NOW.date())
check("past ones this year, with the age when the year is known",
      [(b["date"], b["title"]) for b in bs], [("2026-09-20", "Ava turns 12"), ("2026-10-01", "Leo turns 6")])

print("── photo counts: a person's own phone, summed for the household ──")
oy.set_photo_days("p-robin", {"2026-10-02": 12, "2026-09-02": 30}, today=NOW.date())
oy.set_photo_days("p-alex", {"2026-10-02": 5}, today=NOW.date())
oy.set_photo_days("p-alex", {"2026-10-02": 7}, today=NOW.date())   # replaces Alex's own for that day
check("summed per day across people, a later report replacing that person's", oy.photo_totals(2026),
      {"2026-09-02": 30, "2026-10-02": 19})
for bad in ({"2026-12-01": 1}, {"not-a-day": 1}, {"2026-10-01": -1}, {"2026-10-01": True}, ["2026-10-01"]):
    try:
        oy.set_photo_days("p-x", bad, today=NOW.date())
        check(f"refused: {bad!r}", "stored", "ValueError")
    except ValueError:
        check(f"refused: {bad!r}", "ValueError", "ValueError")

print("── the answer ──")
out = oy.gather(2026, NOW, nodes=NODES, household=HOUSE, birthdays=[("Ava Hale", "2014-09-20")])
check("months newest first, each with moments and photo counts",
      [(m["month"], len(m["moments"]), m["photos"]) for m in out["months"]],
      [("2026-10", 2, {"count": 19, "days": 1}), ("2026-09", 2, {"count": 30, "days": 1}),
       ("2026-08", 1, {"count": 0, "days": 0})])
check("a year ago today", [(m["date"], m["title"]) for m in out["a_year_ago_today"]], [("2025-10-03", "Pumpkin patch")])
check("…and nothing in it names who took a photo", "p-robin" in json.dumps(out) or "p-alex" in json.dumps(out), False)

print("── remembered once they've happened (the calendar sync looks ahead) ──")
r = oy.remember(NODES, HOUSE, TZ, NOW)
check("past household moments kept (this year and last)", r, {"kept": 5, "dropped": 0})
out = oy.gather(2026, NOW, nodes=[], household=HOUSE, birthdays=[])
check("…the year still shows them after the sync drops the events", [m["title"] for mo in out["months"] for m in mo["moments"]],
      ["See for details", "Fall festival", "Ava's first day of school", "Trip to the coast"])
check("…a year ago today too", [m["title"] for m in out["a_year_ago_today"]], ["Pumpkin patch"])
check("…never with the calendar's name", "_cal" in json.dumps(out) or "family" in json.dumps(out).lower(), False)
check("the adults un-mark the calendar: what was kept from it goes", oy.remember([], set(), TZ, NOW)["dropped"], 5)
check("…and the year is empty again", oy.gather(2026, NOW, nodes=[], household=set(), birthdays=[])["months"][-1]["moments"]
      if oy.gather(2026, NOW, nodes=[], household=set(), birthdays=[])["months"] else [], [])

print("── the routes ──")
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform):
    sp = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": sp["code"], "device_name": name, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}


TV = pair("Living room TV", "household", "tvos")
PH = pair("Robin", "adult", "ios")
saved = oy.gather
oy.gather = lambda year=None: {"year": year or 2026, "months": [], "a_year_ago_today": []}
try:
    r = c.get("/v1/our-year", headers=TV)
    check("the TV (a household device) may read it", (r.status_code, r.json()["year"]), (200, 2026))
    check("a bad year: 400", c.get("/v1/our-year?year=1900", headers=TV).status_code, 400)
finally:
    oy.gather = saved
check("a person's own phone sends photo counts",
      c.post("/v1/our-year/photos", json={"days": {local_time.today().isoformat(): 3}}, headers=PH).json(), {"days": 1})
check("the TV can't (refused by the route table)", c.post("/v1/our-year/photos", json={"days": {}}, headers=TV).status_code, 403)
check("the master token (no device) can't", c.post("/v1/our-year/photos", json={"days": {}}, headers=M).status_code, 400)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
