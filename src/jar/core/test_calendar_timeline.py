#!/usr/bin/env python3
# gate: hermetic
"""A re-synced calendar item never comes back as something that happened "today, 7:58" (#636), and a routine
coming-or-going note stays out of a turn's context unless the question is about where or when (#633).

10-02: a 09-30 calendar event "Pickup Leo from school (Zach)", re-synced at 07:58 PT, was on the household
timeline stamped 07:58, and titan said "7:58 AM: you picked up Leo from school". The kids were with their dad.

Run: python3 scripts/test_calendar_timeline.py
"""
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"

import agent_runtime_lib as lib  # noqa: E402
import reflection  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


NOW = datetime(2026, 10, 2, 11, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
SYNC = "2026-10-02T14:58:50.123456789+00:00"     # 07:58 PT, when the calendar synced
PICKUP = {"event_id": "cx-calendar-c4b1413cc302b59e-t", "timestamp": SYNC, "event_type": "household.note",
          "payload": {"text": "Pickup Leo from school (Zach)", "source": "calendar", "kind": "observed",
                      "upstream_id": "6B2CC78F-1@2026-09-30T21:50:00Z", "synced_at": "2026-10-02T14:58:50Z",
                      "observed_at": "2026-08-24T20:59:15Z", "person": "zach", "audience": "private"}}
NO_START = {"event_id": "cx-calendar-0-t", "timestamp": SYNC, "event_type": "household.note",
            "payload": {"text": "Zach CTO (Zach)", "source": "calendar", "upstream_id": "ABC-UID",
                        "person": "zach", "audience": "private"}}
REAL = {"timestamp": "2026-10-02T15:30:00+00:00", "event_type": "household.note",
        "payload": {"text": "Changed the furnace filter", "person": "zach", "audience": "private"}}

print("\n── when a calendar entry is ──")
check("its occurrence, from the upstream id", reflection.calendar_start(PICKUP),
      datetime(2026, 9, 30, 21, 50, tzinfo=ZoneInfo("UTC")).timestamp())
check("never its sync time", reflection._event_ts(PICKUP) != reflection._parse_ts(SYNC), True)
check("one that doesn't say has no time", reflection._event_ts(NO_START), None)
check("starts_at wins when the connector sends it",
      reflection.calendar_start({"payload": {"source": "calendar", "starts_at": "2026-10-05T16:00:00Z"}}),
      datetime(2026, 10, 5, 16, 0, tzinfo=ZoneInfo("UTC")).timestamp())
check("other entries keep their stamp", reflection._event_ts(REAL), reflection._parse_ts("2026-10-02T15:30:00+00:00"))

print("\n── recall_events ──")
out = lib._timeline_listing([PICKUP, NO_START, REAL], now=NOW)
check("not 'today, 7:58'", ("07:58" in out, "Fri Oct 2 07:58" in out), (False, False))
check("at its own time, marked as the calendar's", out.split("\n")[1:], [
    "- Fri Oct 2 08:30 (2 h ago) Changed the furnace filter",
    "- Wed Sep 30 14:50 (2 days ago) [on the calendar: scheduled, not a record that it happened] "
    "Pickup Leo from school (Zach)"])
check("'what happened today' doesn't list it", "Pickup Leo" in lib._timeline_listing([PICKUP, REAL], day="today", now=NOW),
      False)
check("one with no start isn't listed at all", "Zach CTO" in out, False)
future = {**PICKUP, "payload": {**PICKUP["payload"], "upstream_id": "6B2CC78F-1@2026-10-02T23:50:00Z"}}
check("an upcoming one says so", lib._timeline_listing([future], now=NOW).split("\n")[1],
      "- Fri Oct 2 16:50 (in 5 h) [on the calendar: scheduled, not a record that it happened] "
      "Pickup Leo from school (Zach)")

print("\n── the evening letter and the apps' feed ──")
items = reflection._timeline_items(0, 4e9, lambda s: [PICKUP, REAL] if s == "household-timeline" else [], [])
check("the letter leaves calendar entries out", [i.get("title") for i in items], ["Changed the furnace filter"])
saved = lib._timeline_events
lib._timeline_events = lambda stream="household-timeline": [PICKUP, NO_START, REAL]
try:
    feed = lib.timeline_for("zach", "admin")
finally:
    lib._timeline_events = saved
check("the feed places it at its start; one with no start is left to the calendar",
      [(f["text"], f["at"]) for f in feed], [("Changed the furnace filter", "2026-10-02T15:30:00+00:00"),
                                             ("Pickup Leo from school (Zach)", "2026-09-30T21:50:00Z")])

print("\n── comings and goings stay out of unrelated context (#633) ──")
for t in ("Left the house for work", "Zach left home", "Arrived at school", "Heading to Costco",
          "Got home", "On my way back"):
    check(f"a waypoint: {t}", bool(lib._WAYPOINT_RX.match(t)), True)
for t in ("Changed the furnace filter", "Ava's ballet moved to 5:15", "Leftover pizza is in the fridge",
          "Paid the PG&E bill"):
    check(f"not one: {t}", bool(lib._WAYPOINT_RX.match(t)), False)
for q in ("When did Zach leave?", "Where is Ava?", "Is Robin home yet?", "Who picked up Leo?"):
    check(f"about where or when: {q}", bool(lib._PLACE_QUESTION_RX.search(q)), True)
for q in ("What's the wifi password?", "Add milk to the list", "How much is the T-Mobile bill?"):
    check(f"not about it: {q}", bool(lib._PLACE_QUESTION_RX.search(q)), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
