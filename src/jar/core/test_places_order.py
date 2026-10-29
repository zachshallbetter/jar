#!/usr/bin/env python3
# gate: hermetic
"""Where someone is (titan's half of jellybean#666, "Left the house" shown wrongly): the latest place event is the one
that happened last (its own `at`, offsets parsed), not the one titan received last; a step out and back within
places.JITTER_S is jitter; the where tool gives the model a household-local time from code, never raw ISO; a departure's
effects wait out the jitter window and an arrival back cancels them. Throwaway stores, made-up people.
Run: python3 scripts/test_places_order.py"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-places-order-"))
for k, v in {"PLACES_DB": "places.db", "HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily",
             "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ.pop("PLACES_JITTER_S", None)
sys.path.insert(0, str(Path(__file__).parent))
import places  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


def rec(eid, person, event, place, at):
    places.record(eid, person, event, place, at)


print("── the latest is what happened last, not what arrived last ──")
rec("r-2", "robin", "arrived", "school", "2026-10-04T08:30:00-07:00")   # the phone sent this first…
rec("r-1", "robin", "left", "home", "2026-10-04T08:12:00-07:00")        # …and retried this one late
check("a late retry of an earlier departure isn't 'latest'", (places.where_now("robin") or {}).get("id"), "r-2")
check("recent() is newest first by the event's own time", [e["id"] for e in places.recent("robin")], ["r-2", "r-1"])

print("── offsets are parsed, not compared as text ──")
rec("a-1", "ava", "arrived", "library", "2026-09-20T15:10:00Z")          # 8:10 AM Pacific
rec("a-2", "ava", "left", "library", "2026-09-20T08:20:00-07:00")        # 8:20 AM Pacific; sorts first as text
check("8:20 -07:00 is after 15:10Z", (places.where_now("ava") or {}).get("id"), "a-2")
rec("a-3", "ava", "arrived", "park", None)                               # no `at`: when titan received it
check("no `at`: titan's receipt time stands in (now, so newest)", (places.where_now("ava") or {}).get("id"), "a-3")

print("── a step out and back within the jitter window isn't a trip ──")
rec("j-1", "jordan", "arrived", "home", "2026-10-04T17:00:00-07:00")
rec("j-2", "jordan", "left", "home", "2026-10-04T18:00:00-07:00")
rec("j-3", "jordan", "arrived", "home", "2026-10-04T18:03:00-07:00")
check("left 6:00, back 6:03: still 'arrived home' from 5:00", (places.where_now("jordan") or {}).get("id"), "j-1")
rec("l-1", "leo", "left", "home", "2026-10-04T18:00:00-07:00")
rec("l-2", "leo", "arrived", "home", "2026-10-04T18:10:00-07:00")
check("left 6:00, back 6:10: a real trip, the arrival stands", (places.where_now("leo") or {}).get("id"), "l-2")
rec("h-1", "hale", "left", "home", "2026-10-04T18:00:00-07:00")
rec("h-2", "hale", "arrived", "gym", "2026-10-04T18:02:00-07:00")
check("left home, arrived somewhere else 2 min later: not jitter", (places.where_now("hale") or {}).get("id"), "h-2")
check("still_away: an arrival back cancels the departure",
      places.still_away("jordan", "home", places.event_ts({"at": "2026-10-04T18:00:00-07:00"})), False)
check("still_away: no arrival back, the departure stands",
      places.still_away("hale", "home", places.event_ts({"at": "2026-10-04T18:00:00-07:00"})), True)

print("── a person-readable time, from code ──")
now = datetime.fromisoformat("2026-10-04T11:15:00-07:00").timestamp()
check("today", places.when_text({"at": "2026-10-04T08:12:00-07:00"}, now), "at 8:12 AM today (3 h ago)")
check("a UTC stamp shown in household time", places.when_text({"at": "2026-10-04T15:10:00Z"}, now), "at 8:10 AM today (3 h ago)")
check("yesterday", places.when_text({"at": "2026-10-03T21:04:00-07:00"}, now), "yesterday at 9:04 PM")
check("older", places.when_text({"at": "2026-10-02T07:10:00-07:00"}, now), "on Fri Oct 2 at 7:10 AM")

print("── the where tool hands the model that, never raw ISO ──")
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402


class _Store:
    def people(self):
        return [{"id": "robin", "name": "Robin Hale", "role": "adult"}]


real_viewer, real_store = lib._viewer, hh.get_store
try:
    lib._viewer = lambda: ("robin", "adult")
    hh.get_store = lambda: _Store()
    out = lib._where_is({})
    check("arrived school, a local time, no ISO", ("Robin Hale: arrived school " in out, "8:30 AM" in out, "2026-10-04T" in out,
                                                    " at at " in out or "at on" in out), (True, True, False, False))
finally:
    lib._viewer, hh.get_store = real_viewer, real_store

print("── a departure's effects wait, and an arrival back cancels them ──")
import agent_api_server as srv  # noqa: E402
ran: list[tuple] = []


async def fake_effects(person, role, event, place, region):
    ran.append((person, event, place))

real_effects = srv._place_effects
srv._place_effects = fake_effects
try:
    left = places.event_ts({"at": "2026-10-04T18:00:00-07:00"})
    check("jordan came back: nothing runs", (asyncio.run(srv._held_departure("jordan", "adult", "home", None, left, wait=0)), ran),
          (False, []))
    check("hale didn't: the departure's effects run", (asyncio.run(srv._held_departure("hale", "adult", "home", None, left, wait=0)), ran),
          (True, [("hale", "left", "home")]))

    print("── a restart inside the window replays the held departure ──")
    import time as _t
    ran.clear()
    now = _t.time()
    places.hold("h-9", "hale", "adult", "home", None, left, now - 1)            # window just ended while titan was down
    places.hold("j-9", "jordan", "adult", "home", None, left, now + 0.2)        # still inside it, and jordan came back
    places.hold("s-9", "hale", "adult", "gym", None, left, now - 2 * 3600)      # ended hours ago: too late to act

    async def restart():
        r = await srv._resume_held_departures(now)
        await asyncio.gather(*list(srv._place_tasks))
        return r
    check("two resumed, one stale", asyncio.run(restart()), {"resumed": 2, "stale": 1})
    check("hale's departure runs; jordan's is cancelled; the stale one never runs", ran, [("hale", "left", "home")])
    check("nothing left held", places.held(), [])

    print("── the live task and a replay never both act ──")
    ran.clear()
    places.hold("d-9", "hale", "adult", "home", None, left, now)
    check("the first claim takes it, the second finds nothing", (places.take_held("d-9"), places.take_held("d-9")),
          (True, False))
    check("a task whose hold was already taken does nothing",
          (asyncio.run(srv._held_departure("hale", "adult", "home", None, left, wait=0, hold_id="d-9")), ran), (False, []))
finally:
    srv._place_effects = real_effects
check("the hold is about five minutes", places.JITTER_S, 300)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
