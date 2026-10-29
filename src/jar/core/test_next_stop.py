#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Next stop on leaving a place (titan-node#513): only the person's OWN located event in the next 3 hours (never a
shared, household, someone else's or hidden calendar), one card to them only, once per event, not in quiet hours, not
when a trip Live Activity already covers it; dry logs never a title or place; adults only. Fictional household, a
pinned clock.
Run: python3 scripts/test_next_stop.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-nextstop-"))
os.environ["PLACES_DB"] = str(_TMP / "places.db")
os.environ.pop("NEXT_STOP_CARDS", None)
sys.path.insert(0, str(Path(__file__).parent))

import next_stop as N  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)   # 1:00 PM in UTC-7
PT = timezone(timedelta(hours=-7))
PEOPLE = [{"id": "alex", "role": "admin", "calendars": ["Alex"], "calendars_ignored": ["Old work"]},
          {"id": "robin", "role": "adult", "calendars": ["Robin"]}]
HOUSEHOLD = {"family"}


def ev(nid, title, minutes, loc="12 Elm St", cal="Alex", owner="alex", shared=False, all_day=False, sid=None):
    return {"node_id": nid, "data": {"title": title, "start": (NOW + timedelta(minutes=minutes)).isoformat(),
            "location": loc, "calendar": cal, "calendar_owner": "shared" if shared else "", "all_day": all_day,
            "provenance": {"owner": owner, "source_id": sid or f"{nid}@x"}}}


SENT, LOGS = [], []


def send(title, detail="", **kw):
    SENT.append({"title": title, "detail": detail, **kw})
    return {"id": "c"}


def go(nodes, person="alex", role="admin", how="1", quiet=False, trip=lambda p, s: False, now=NOW):
    return N.on_left(person, role, now=now, nodes=nodes, people=PEOPLE, household=HOUSEHOLD, trip_running=trip,
                     quiet=quiet, send=send, log=LOGS.append, how=how, tz=PT)


print("── which event ──")
check("off by default: nothing", (N.on_left("alex", "admin", nodes=[ev("e1", "Dentist", 60)], people=PEOPLE,
                                            household=HOUSEHOLD, send=send), SENT),
      ({"events": 0, "sent": 0, "would_send": 0, "already": 0, "quiet": 0}, []))
st = go([ev("e5", "Late thing", 300), ev("e2", "Dentist", 120), ev("e3", "Lunch", 60, loc="")])
check("the soonest own event with a location within 3 hours", (st["sent"], SENT[-1]["title"], SENT[-1]["detail"]),
      (1, "Next: Dentist at 3 PM", "12 Elm St"))
check("…to them only, with the travel-time prompt", (SENT[-1]["to"], SENT[-1]["actions"][0]["kind"],
      SENT[-1]["actions"][0]["value"]), (["alex"], "prompt", "How long will it take me to get to 12 Elm St?"))
SENT.clear()
for label, node in (("a calendar merely shared with them", ev("s1", "Team sync", 60, shared=True)),
                    ("the household calendar", ev("s2", "Family dinner", 60, cal="Family")),
                    ("a calendar assigned to Robin", ev("s3", "Robin's yoga", 60, cal="Robin")),
                    ("a calendar they've hidden", ev("s4", "Old standup", 60, cal="Old work")),
                    ("someone else's synced event", ev("s5", "Robin's thing", 60, owner="robin", cal="Robin")),
                    ("an all-day event", ev("s6", "Conference", 60, all_day=True)),
                    ("one already started", ev("s7", "Now", -10)),
                    ("one 4 hours away", ev("s8", "Later", 240))):
    check(f"never {label}", (go([node])["sent"], SENT), (0, []))
check("not when a trip Live Activity covers it", go([ev("t1", "Flight", 90)], trip=lambda p, s: True)["sent"], 0)

print("\n── who, how often, when ──")
check("a kid: nothing", go([ev("k1", "Practice", 60, owner="ava")], person="ava", role="kid")["sent"], 0)
check("a shared screen: nothing", go([ev("h1", "Thing", 60)], person="household", role="household")["sent"], 0)
SENT.clear()
nodes = [ev("o1", "Haircut", 45)]
check("once per event", ([go(nodes)["sent"], go(nodes)["already"]], len(SENT)), ([1, 1], 1))
check("quiet hours: waits", go([ev("q1", "Late call", 30)], quiet=True), {"events": 1, "sent": 0, "would_send": 0,
                                                                         "already": 0, "quiet": 1})
check("…and comes when it's not quiet", go([ev("q1", "Late call", 30)])["sent"], 1)
check("Robin, her own calendar", (go([ev("r1", "Yoga", 60, cal="Robin", owner="robin")], person="robin", role="adult")["sent"],
                                  SENT[-1]["to"]), (1, ["robin"]))

print("\n── dry ──")
SENT.clear()
st = go([ev("d1", "Doctor Hale", 70, loc="400 Birch Rd")], how="dry")
check("dry: a log line, no card", (st["would_send"], SENT, len(LOGS)), (1, [], 1))
check("…never the title or the place", any(w in LOGS[-1] for w in ("Doctor", "Hale", "Birch")), False)

print("\n── kept: only a hashed key, dropped after the event ──")
import sqlite3  # noqa: E402
con = sqlite3.connect(os.environ["PLACES_DB"])
rows = con.execute("SELECT * FROM next_stop_cards").fetchall()
con.close()
check("no title, place or event id kept", any(any(w in str(r) for w in ("Haircut", "Elm", "o1@x")) for r in rows), False)
later = ev("z1", "Tomorrow", 60 + 3 * 24 * 60)   # an event an hour after the later clock
go([later], now=NOW + timedelta(days=3))
con = sqlite3.connect(os.environ["PLACES_DB"])
left = con.execute("SELECT COUNT(*) FROM next_stop_cards").fetchone()[0]
con.close()
check("old keys go a day after their event; only the new one stays", left, 1)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
