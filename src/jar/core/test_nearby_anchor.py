#!/usr/bin/env python3
# gate: hermetic   (temp stores, a pinned clock, no model, no network)
"""#1613: a request for things nearby anchors on where the asker is (nearby.py, agent_runtime_lib._nearby_line).

The asker's own fresh arrival somewhere other than home wins; on an iPhone turn with where_am_i and no fresh place, the
model is told to call it first; otherwise home, named when it's set, and said. Location is per person: another
person's arrival never moves the asker's anchor, and a shared household device only ever gets home. Fictional people
(Alex, Robin, Ava, Leo, Hale, Jordan) and places (Cedar Hollow, Larkspur Falls, Brightwater). The clock is pinned to an evening where the
household day and UTC's differ; run it with TZ=UTC and TZ=America/Los_Angeles.
Run: python3 scripts/test_nearby_anchor.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP = Path(tempfile.mkdtemp(prefix="titan-nearby-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TZ"] = "America/Los_Angeles"      # the household's zone; the host's (TZ) must not matter
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import nearby  # noqa: E402
import places  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


Z = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 12, 19, 30, tzinfo=Z)          # 02:30 UTC on the 13th
DB = _TMP / "places.db"


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


n = 0


def ev(person, event, place, hours_ago):
    global n
    n += 1
    places.record(f"e{n}", person, event, place, at(hours_ago), db=DB)


def where(person):
    return places.where_now(person, db=DB)


def say(words, person, role="adult", can_locate=False, home="Cedar Hollow"):
    return nearby.line(words, person, role, NOW, where_now=where, can_locate=can_locate, home_name=home,
                       when_text=places.when_text)


print("── which questions ──")
for q in ("Any indoor activities we could do?", "things to do this afternoon", "Where should we eat?",
          "what's there to do near here", "good restaurants nearby?", "any coffee shops close by",
          "kid-friendly places around here", "rainy day activities for Ava"):
    check(f"nearby: {q}", nearby.asks_nearby(q), True)
for q in ("What's on my calendar today?", "where am I?", "things to do in Brightwater",
          "restaurants near Larkspur Falls", "who's home?", "remind me to call Robin"):
    check(f"not a local-suggestion question (or names its place): {q}", nearby.asks_nearby(q), False)
check("any other question: no line", say("What's on my calendar today?", "alex"), None)

print("── the asker's own fresh place wins ──")
ev("alex", "left", "home", 3)
ev("alex", "arrived", "Larkspur Falls", 2)
got = say("Any indoor activities we could do?", "alex")
check("anchored on Larkspur Falls, not home", ("near Larkspur Falls" in got, "not near home (Cedar Hollow)" in got),
      (True, True))
check("…and it asks for one clause saying so", "say so in one clause" in got and "Near Larkspur Falls, where you are now" in got,
      True)
check("…with when, in the household's day", "at 5:30 PM today" in got, True)
check("an iPhone turn still uses the fresh place", "near Larkspur Falls" in say("where to eat", "alex", can_locate=True), True)
check("a place they name is theirs", "If they name another place, use theirs" in got, True)

print("── per person, never another's ──")
ev("robin", "arrived", "Brightwater", 1)
check("Robin's arrival doesn't move Alex", "Brightwater" in say("things to do", "alex"), False)
check("Robin's own turn uses Robin's", "near Brightwater" in say("things to do", "robin"), True)
got = say("things to do", "ava", role="kid")
check("Ava (no events) gets home, not a parent's or anyone's place",
      ("near home (Cedar Hollow)" in got, "Larkspur" in got, "Brightwater" in got), (True, False, False))
got = say("things to do", None, role="household", can_locate=True)
check("a shared household device: home only, never where_am_i", ("near home (Cedar Hollow)" in got, "where_am_i" in got),
      (True, False))
got = say("things to do", "robin", role="household", can_locate=True)
check("…even when a person id rides along", ("Brightwater" in got, "where_am_i" in got), (False, False))

print("── stale, left, home ──")
ev("leo", "arrived", "Larkspur Falls", nearby.FRESH_HOURS + 1)
got = say("where should we eat", "leo")
check("an arrival older than FRESH_HOURS isn't where they are", ("Larkspur" in got, "near home (Cedar Hollow)" in got),
      (False, True))
check("…and the line says home was used", "say in one clause that you used home" in got, True)
ev("hale", "arrived", "Larkspur Falls", 3)
ev("hale", "left", "Larkspur Falls", 1)
check("left since: not there any more", "Larkspur" in say("things to do", "hale"), False)
ev("jordan", "arrived", "home", 30)
got = say("things to do nearby", "jordan")
check("arrived home (however long ago) and not left: at home", got.startswith("Where you are, for this nearby question: at home"),
      True)
check("FRESH_HOURS matches the graph's 'where you are' window", nearby.FRESH_HOURS, 6)

print("── no fresh place ──")
got = say("things to do", "ava", role="kid", can_locate=True)
check("an iPhone turn with where_am_i: call it first", ("Call where_am_i first" in got, "Only if it gives no location, use home (Cedar Hollow)" in got),
      (True, True))
got = say("things to do", "ava", can_locate=False, home=None)
check("no home town saved: plain 'home'", ("near home," in got, "()" in got), (True, False))

print("── wired into the turn's context ──")
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

io = lib._TURN_IO
real_now, real_where = lib._local_now, places.where_now
lib._local_now = lambda: NOW
places.where_now = lambda person, db=None: real_where(person, db=DB)
os.environ["HOUSEHOLD_PREFS"] = str(_TMP / "household.json")
(_TMP / "household.json").write_text('{"home": {"name": "Cedar Hollow, WA", "latitude": 1.0, "longitude": 2.0}}')
import weather  # noqa: E402
weather.PREFS = _TMP / "household.json"
try:
    io.person, io.role, io.client, io.device_tools, io.remote = {"id": "alex"}, "adult", "iphone", frozenset({"where_am_i"}), None
    got = lib._nearby_line("Any indoor activities we could do?\n\n(This needs a device tool — x)")
    check("Alex on their iPhone: Larkspur Falls", "near Larkspur Falls" in (got or ""), True)
    io.person, io.role = {"id": "ava"}, "kid"
    got = lib._nearby_line("things to do?")
    check("Ava on their iPhone, nowhere fresh: where_am_i, then the saved home town",
          ("Call where_am_i first" in (got or ""), "home (Cedar Hollow, WA)" in (got or "")), (True, True))
    io.client, io.device_tools = "mac", None
    got = lib._nearby_line("things to do?")
    check("Ava on a Mac: no where_am_i, home said", ("where_am_i" in (got or ""), "near home (Cedar Hollow, WA)" in (got or "")),
          (False, True))
    io.person, io.role, io.client = None, "household", "tv"
    got = lib._nearby_line("things to do?")
    check("a shared screen: home", ("near home (Cedar Hollow, WA)" in (got or ""), "Larkspur" in (got or "")), (True, False))
    io.person, io.role = {"id": "alex"}, "adult"
    check("not a nearby question: nothing", lib._nearby_line("What's on my calendar today?"), None)
    src = Path(lib.__file__).read_text().split("def _situational_context_inner", 1)[1].split("\n    # 3. Recent household", 1)[0]
    check("the context builder adds the line", "_nearby_line(query)" in src and "parts.append(near)" in src, True)
finally:
    lib._local_now, places.where_now = real_now, real_where
    io.person = io.role = io.client = io.device_tools = io.remote = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
