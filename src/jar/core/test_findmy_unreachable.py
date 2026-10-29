#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#769: "where's <someone / their watch>?" on the iPhone needs Find My, which runs on the Mac. With the Mac unreachable
(nothing live answered), a reply that answers with a place anyway gets "I can't reach Find My: your Mac isn't
reachable.", and what the calendar says is kept labelled as a plan. A Find My or where_is answer, a reply that
already says so, a Mac turn, and a question about a place are untouched.
Run: python3 scripts/test_findmy_unreachable.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["WORLD_DIR"] = str(Path(tempfile.mkdtemp()) / "world")   # hermetic: the allowlist reads the asker's world places
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


lib._household_names = lambda: frozenset({"ava", "leo", "robin"})   # noqa: E731 - fictional household


def turn(asked, client="iphone", outcomes=None):
    io = lib._TURN_IO
    io.client, io.said, io.outcomes = client, asked, list(outcomes or [])
    lib._TURN_INFO.info = {}


U = lib.FINDMY_UNREACHABLE
CAL = {"tool": "list_calendar_events", "ok": True, "text": "Ava and Leo: with Robin until Thu 9:00 AM"}
GUESS = "Ava and Leo are with Robin until Thursday at 9 AM, so her watch is over there."

print("── the 10-03 nightly's shape: answered from the calendar ──")
turn("Where's Ava's watch right now?", outcomes=[CAL])
r = lib._with_backed_whereabouts(GUESS)
check("starts with the plain line", r.startswith(U), True)
check("…keeps the calendar, labelled as a plan", ("a plan, not where they are" in r, GUESS in r), (True, True))
check("…and counted", lib._TURN_INFO.info.get("whereabouts_unbacked"), 1)
turn("Where is Leo?", outcomes=[])
check("no calendar read: just the line", lib._with_backed_whereabouts("Leo is at school."), U)
turn("where are my keys", outcomes=[])
check("a tracked thing, no name", lib._with_backed_whereabouts("Your keys are probably in the car."), U)

print("\n── untouched ──")
turn("Where's Ava's watch right now?", outcomes=[{"tool": "find_my_locate", "ok": True,
     "text": "Find My: Ava's Apple Watch — Lincoln Elementary · 3 min ago (1.2 mi)"}])
check("Find My answered", lib._with_backed_whereabouts("Ava's watch is at Lincoln Elementary, 3 minutes ago."),
      "Ava's watch is at Lincoln Elementary, 3 minutes ago.")
turn("Where is Leo?", outcomes=[{"tool": "where_is", "ok": True, "text": "Leo: arrived at school 8:02 AM"}])
check("titan's own place events answered", lib._with_backed_whereabouts("Leo arrived at school at 8:02."),
      "Leo arrived at school at 8:02.")
turn("Where is Leo?", outcomes=[{"tool": "where_is", "ok": True, "text": "No place events for Leo yet."}])
check("…but 'no place events' isn't an answer", lib._with_backed_whereabouts("Leo is at school."), U)
turn("Where's Ava's watch right now?", outcomes=[CAL])
already = "I can't reach Find My right now because your Mac isn't reachable. Her calendar has her at Robin's."
check("the reply already says Find My can't be reached", lib._with_backed_whereabouts(already), already)
turn("Where's Ava's watch right now?", client=None, outcomes=[CAL])
check("a Mac turn", lib._with_backed_whereabouts(GUESS), GUESS)
for q, a in [("Where's the dentist?", "Pearl Dental is at 12 Main St."),
             ("Where is the party on Saturday?", "The party is at the community center.")]:
    turn(q, outcomes=[CAL])
    check(f"a place, not someone: {q!r}", lib._with_backed_whereabouts(a), a)

print("\n── the allowlist's world places are the asker's own visible ones ──")
import world as W  # noqa: E402
_real_visible = lib._visible_place_names
w = W.World(Path(tempfile.mkdtemp()) / "w.db")
w.sync("notes", [W.Obs(entity="harbor-pilates", etype="activity", name="Harbor Pilates", attr="place", value="9 Dock St",
                       observed_at="2026-09-20", kind="note", ref="n1", origin="note:robin", label="Robin's note",
                       owner="robin", audience="private", bubble="robin", facet="personal"),
                 W.Obs(entity="ballet-modern", etype="activity", name="Ballet + Modern", attr="place",
                       value="Maple Dance Studio, 112 S Adams St", observed_at="2026-09-20", kind="household", ref="h1",
                       origin="household.yaml", label="the household file", owner=None, audience="household",
                       bubble="household", facet="home"),
                 # bills vendors: orgs, visible to the household (JEV's #1253 gap): never a place
                 W.Obs(entity="uber", etype="org", name="Uber", attr="address", value="1515 3rd St, San Francisco",
                       observed_at="2026-09-20", kind="bill", ref="b1", origin="bill:uber", label="a bill", owner=None,
                       audience="household", bubble="household", facet="home"),
                 W.Obs(entity="delta", etype="org", name="Delta", attr="address", value="1030 Delta Blvd, Atlanta",
                       observed_at="2026-09-20", kind="bill", ref="b2", origin="bill:delta", label="a bill", owner=None,
                       audience="household", bubble="household", facet="home"),
                 W.Obs(entity="acme-dental-billing", etype="org", name="Acme Billing", attr="address", value="5 Elm St",
                       observed_at="2026-09-20", kind="bill", ref="b3", origin="bill:acme", label="a bill", owner=None,
                       audience="household", bubble="household", facet="home")])
_get = W.get
W.get = lambda: w
for who, role, place in (("robin", "adult", True), ("zach", "admin", False), ("ava", "kid", False), ("tv", "household", False)):
    lib._TURN_IO.person = {"id": who}
    _tr = lib.turn_role
    lib.turn_role = lambda r=role: r
    check(f"{who} ({role}): \"Where is Robin's Harbor Pilates?\" {'is a place' if place else 'stays whereabouts (not their place to see)'}",
          lib._asks_whereabouts("Where is Robin's Harbor Pilates?"), not place)
    lib.turn_role = _tr
lib._TURN_IO.person = {"id": "zach"}
_tr = lib.turn_role
lib.turn_role = lambda: "admin"
for q, whereabouts in (("Where's Leo's Uber?", True), ("Where is Ava's Delta flight?", True),
                       ("Where is Ava's Acme Billing?", True),       # an org without a moving word: still never a place
                       ("Where is Ava's Ballet + Modern?", False)):  # an activity with a place: passes
    check(f"real world store: {q!r} {'stays whereabouts' if whereabouts else 'is a place'}",
          lib._asks_whereabouts(q), whereabouts)
check("…the asker's places are activities with a place only (orgs, vendors never)",
      sorted(lib._visible_place_names()), ["ballet modern"])
lib.turn_role = _tr
W.get = _get
lib._TURN_IO.person = None

print("\n── a person's place isn't the person: an allowlist (#1252, JEV's review) ──")
lib._visible_place_names = lambda: {"ballet modern", "soccer practice"}   # the asker's own visible world places (fictional)
for q, a in [("Where is Ava's Ballet + Modern?", "Ava's Ballet + Modern is at Maple Dance Studio, 112 S Adams St."),
             ("Where's Leo\u2019s soccer practice?", "Leo's soccer practice is at Riverside Park field 2."),
             ("Where is Ava's school?", "Ava's school is Lincoln Elementary, 400 W Main."),
             ("Where is Robin's dentist?", "Robin's dentist is Pearl Dental, 12 Main St.")]:
    turn(q, outcomes=[CAL])
    check(f"a fixed place: {q!r} keeps the reply", lib._with_backed_whereabouts(a), a)
lib._visible_place_names = lambda: set()
turn("Where's Leo\u2019s soccer practice?", outcomes=[CAL])
check("…'soccer practice' passes only as the asker's own world place: not visible → whereabouts",
      lib._asks_whereabouts("Where's Leo\u2019s soccer practice?"), True)
lib._visible_place_names = lambda: {"ballet modern", "soccer practice"}
for q in ("Where is Ava?", "Where are Ava and Leo right now?", "Where's Ava's phone?", "Where is Ava's friend Leo?",
          # JEV's moving things and the bare possessive: never "a person's place"
          "Where is Ava's bus?", "Where's Ava's ride?", "Where is Ava's carpool?", "Where's Ava's flight?",
          "Where is Ava's train?", "Where's Ava's bike?", "Where's Ava's now?", "Where is Ava's school bus?",
          # Coordination's list: a person's own whereabouts or a thing they carry
          "Where is Ava's location?", "Where is Ava's car?", "Where are Ava's keys?", "Where is Ava's bag?",
          "Where is Ava's AirTag?", "Where is Ava's iPad?", "Where is Ava's laptop?", "What are Ava's whereabouts? Where is she?",
          "Where is Ava\u2019s current location?"):
    turn(q, outcomes=[CAL])
    check(f"still whereabouts: {q!r}", lib._asks_whereabouts(q), True)
for q in ("Where is Ava's location?", "Where is Ava's car?", "Where are Ava's keys?", "Where is Ava's bus?",
          "Where's Ava's now?"):
    turn(q, outcomes=[CAL])
    check(f"…and the filter still applies to an unbacked answer: {q!r}",
          lib._with_backed_whereabouts("It's probably at school.").startswith(U), True)

print("\n── wired into the reply ──")
check("the pipeline runs it", "_with_backed_whereabouts(_with_backed_trip(" in Path(lib.__file__).read_text(), True)

lib._TURN_IO.client = lib._TURN_IO.said = lib._TURN_IO.outcomes = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
