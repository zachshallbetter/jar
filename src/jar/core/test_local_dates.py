#!/usr/bin/env python3
"""Timeline events are labelled in the household's time, never by their UTC date (09-30: events after
5 PM Pacific read "2026-10-01" in the per-turn context, and "what's on my calendar tomorrow?" asked
the phone for Oct 2).

Run: python3 scripts/test_local_dates.py
"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(__file__))
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


EVENING = {"timestamp": "2026-10-01T00:12:00.123456789+00:00", "event_type": "household.note",
           "payload": {"text": "Ran a shell command on the Mac", "person": "zach", "audience": "private"}}
check("a Pacific-evening event is still that evening", lib._event_when(EVENING), "Wed Sep 30 17:12")
check("a morning one", lib._event_when({"timestamp": "2026-09-30T16:05:00Z"}), "Wed Sep 30 09:05")
check("no stamp: whatever it has", lib._event_when({"created_at": "sometime"}), "sometime")

orig = lib._visible_events
lib._visible_events = lambda stream="household-timeline": [EVENING]
import time as _t  # noqa: E402
real_time = _t.time
_t.time = lambda: 1790817000.0   # 09-30 17:30 Pacific, within 48 h of the event
try:
    out = lib.execute_tool("recall_events", {})
    check("recall_events: local, not UTC", ("Wed Sep 30 17:12" in out, "2026-10-01" in out), (True, False))
    ctx = lib._situational_context("anything new?")
    check("the per-turn context: local, not UTC", ("Wed Sep 30 17:12 (" in ctx and "Ran a shell command" in ctx, "2026-10-01" in ctx), (True, False))
    check("the per-turn context: newest first, and says so", "Recent household activity, newest first" in ctx, True)
finally:
    lib._visible_events = orig
    _t.time = real_time

# recall_events: sorted by time (not arrival), newest first and saying so, with how long ago; one day; about words.
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
NOW = datetime(2026, 10, 1, 9, 30, tzinfo=ZoneInfo("America/Los_Angeles"))   # Thursday
EVS = [  # in arrival order: a connector posted Monday's entry last
    {"timestamp": "2026-10-01T16:00:00+00:00", "payload": {"text": "Leo dropped at school"}},     # Thu 09:00
    {"timestamp": "2026-10-01T02:05:00+00:00", "payload": {"text": "Changed the furnace filter"}},  # Wed 19:05
    {"timestamp": "2026-09-28T17:00:00+00:00", "payload": {"text": "Car service at Pearl Auto"}},   # Mon 10:00
]
out = lib._timeline_listing(EVS, now=NOW).split("\n")
check("recall: says newest first, and now", out[0], "Household timeline, newest first (now Thu Oct 1 09:30):")
check("recall: by time, not arrival; with how long ago", out[1:], [
    "- Thu Oct 1 09:00 (30 min ago) Leo dropped at school",
    "- Wed Sep 30 19:05 (yesterday) Changed the furnace filter",
    "- Mon Sep 28 10:00 (3 days ago) Car service at Pearl Auto"])
check("recall: one day (a weekday is the last one)", lib._timeline_listing(EVS, day="monday", now=NOW).split("\n")[1:],
      ["- Mon Sep 28 10:00 (3 days ago) Car service at Pearl Auto"])
check("recall: yesterday", lib._timeline_listing(EVS, day="yesterday", now=NOW).split("\n")[0],
      "Household timeline on Wednesday Sep 30, newest first (now Thu Oct 1 09:30):")
check("recall: about words", lib._timeline_listing(EVS, about="furnace filter", now=NOW).split("\n")[1:],
      ["- Wed Sep 30 19:05 (yesterday) Changed the furnace filter"])
check("recall: nothing that day says so", lib._timeline_listing(EVS, day="2026-09-29", now=NOW),
      "Nothing on the household timeline on Tuesday Sep 29.")
check("recall: a day it can't read", lib._timeline_listing(EVS, day="someday", now=NOW).startswith("Couldn't read the day"), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
