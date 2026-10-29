#!/usr/bin/env python3
"""Offline tests for routines: validation, save/update, the due window, once-a-day nudges, the
run prompt, and the agent tools. Uses a temporary ROUTINES_DB.

Run: python3 scripts/test_routines.py
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["ROUTINES_DB"] = str(Path(tempfile.mkdtemp()) / "routines.db")
sys.path.insert(0, os.path.dirname(__file__))
import routines as rt  # noqa: E402

fails = 0


def check(label, got, expected):
    global fails
    ok = got == expected
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


def err(fn):
    try:
        fn()
    except ValueError as e:
        return str(e)
    return None


check("name required", err(lambda: rt.validate({"steps": ["x"]})), "a routine needs a name")
check("steps required", err(lambda: rt.validate({"name": "x", "steps": []})), "a routine needs at least one step")
check("bad time rejected", err(lambda: rt.validate({"name": "x", "steps": ["a"], "schedule": {"time": "25:00"}})),
      "schedule needs a time like 22:30")
v = rt.validate({"name": "  Good   night ", "steps": "1. Lock the front door\n- Lights off\n\n", "icon": "rocket"})
check("normalised", (v["name"], v["steps"], v["icon"], v["schedule"]), ("Good night", ["Lock the front door", "Lights off"], "sparkles", None))

a = rt.save({"name": "Goodnight", "steps": ["Lock up", "Lights off"], "schedule": {"time": "22:30", "days": [0, 1, 2, 3, 4]}, "icon": "moon"})
b = rt.save({"name": "goodnight", "steps": ["Lock up", "Lights off", "Set the alarm for 6:45"], "schedule": {"time": "22:30"}})
check("same name updates in place", (a["id"] == b["id"], len(rt.list_all()), b["steps"][-1]), (True, 1, "Set the alarm for 6:45"))
check("no days = every day", b["schedule"]["days"], [0, 1, 2, 3, 4, 5, 6])
check("get by name", rt.get("GOODNIGHT")["id"], a["id"])
check("run prompt lists the steps in order", "1. Lock up\n2. Lights off\n3. Set the alarm" in rt.run_prompt(b), True)

wed = datetime(2026, 9, 30, 22, 30)   # a Wednesday
check("due at its minute", [r["name"] for r in rt.due(wed)], ["Goodnight"])
check("due up to 10 min late (after a restart)", len(rt.due(datetime(2026, 9, 30, 22, 40))), 1)
check("not before, not too late", (len(rt.due(datetime(2026, 9, 30, 22, 29))), len(rt.due(datetime(2026, 9, 30, 22, 41)))), (0, 0))
cards = []
check("nudged once", rt.nudge_due(wed, card=cards.append), 1)
check("not twice the same day", rt.nudge_due(datetime(2026, 9, 30, 22, 33), card=cards.append), 0)
check("again the next day", rt.nudge_due(datetime(2026, 10, 1, 22, 30), card=cards.append), 1)
rt.save({"id": a["id"], "name": "Goodnight", "steps": ["Lock up"], "schedule": {"time": "22:30", "days": [5, 6]}})
check("weekday filter", len(rt.due(datetime(2026, 10, 2, 22, 30))), 0)
rt.save({"name": "Paused", "steps": ["x"], "schedule": {"time": "22:30"}, "enabled": False})
check("disabled routines never nudge", [r["name"] for r in rt.due(datetime(2026, 10, 3, 22, 30))], ["Goodnight"])

import agent_runtime_lib as lib  # noqa: E402
out = lib.execute_tool("save_routine", {"name": "Leaving home", "steps": "Lights off\nLock the door", "time": "08:15", "days": "0,1,2,3,4"})
check("tool saves with schedule", out, "Saved routine “Leaving home” with 2 steps, nudging you at 08:15.")
listed = lib.execute_tool("list_routines", {})
check("tool lists: weekdays (not 'daily'), when it runs next, numbered steps",
      "- Leaving home (weekdays at 08:15 · next " in listed and "): 1. Lights off; 2. Lock the door" in listed, True)
from zoneinfo import ZoneInfo  # noqa: E402
tz = ZoneInfo("America/Los_Angeles")
thu = datetime(2026, 10, 1, 14, 0, tzinfo=tz)   # Thursday 2 PM
wk = {"name": "Leaving home", "steps": ["Lights off", "Lock the door"], "schedule": {"time": "08:15", "days": [0, 1, 2, 3, 4]}, "enabled": True}
check("describe: weekdays, next is tomorrow (Fri)", rt.describe(wk, thu),
      "- Leaving home (weekdays at 08:15 · next tomorrow, 8:15 AM): 1. Lights off; 2. Lock the door")
check("describe: after Friday's run, next is Monday", rt.describe(wk, datetime(2026, 10, 2, 9, 0, tzinfo=tz)).split(" · ")[1],
      "next Mon Oct 5, 8:15 AM): 1. Lights off; 2. Lock the door")
gn = {"name": "Goodnight", "steps": ["Lock up"], "schedule": {"time": "22:30", "days": list(range(7))}, "enabled": True}
check("describe: later today says how long", rt.describe(gn, thu), "- Goodnight (daily at 22:30 · next today, 10:30 PM, in 8 h 30 min): 1. Lock up")
check("describe: paused", rt.describe({**gn, "enabled": False}, thu), "- Goodnight (daily at 22:30 · paused): 1. Lock up")
check("describe: weekends and a place trigger", rt.describe({"name": "Home", "steps": ["Lights on"], "schedule": {"time": "09:00", "days": [5, 6]},
                                                               "enabled": True, "trigger": {"place": "Home", "on": "arrive"}}, thu),
      "- Home (weekends at 09:00 · next Sat Oct 3, 9:00 AM · when you arrive at Home): 1. Lights on")
check("describe: not scheduled", rt.describe({"name": "Tidy", "steps": ["a", "b"], "schedule": None, "enabled": True}, thu),
      "- Tidy (not scheduled): 1. a; 2. b")
check("days words", [rt.days_text(d) for d in ([0, 2, 4], [5, 6], None)], ["Mon, Wed, Fri", "weekends", "daily"])
# 10-03: a phone edit of a private routine came in under a shorter name and made a second,
# household routine. An edit names what it changes, keeps what it leaves out, and a near-miss
# name never quietly creates a duplicate.
lib.execute_tool("save_routine", {"name": "Nightlight dinner log", "steps": "Ask what we had for dinner", "time": "22:15",
                                  "icon": "moon", "for": "me"})
night = rt.get("Nightlight dinner log")
n0 = len(rt.list_all())
out = lib.execute_tool("save_routine", {"name": "Dinner Log", "steps": "Ask what we had for dinner"})
check("near-miss name: no second routine", (out.startswith("Didn't save: “Nightlight dinner log” already exists"), len(rt.list_all())),
      (True, n0))
out = lib.execute_tool("save_routine", {"change": "nightlight dinner log", "name": "Dinner Log"})
dl = rt.get("Dinner Log")
check("change renames in place, keeping steps, time, icon and privacy",
      (out, dl["id"] == night["id"], dl["steps"], dl["schedule"]["time"], dl["icon"], dl["audience"], len(rt.list_all())),
      ("Updated routine “Dinner Log” with 1 steps, nudging you at 22:15.", True, ["Ask what we had for dinner"], "22:15",
       "moon", "private", n0))
lib.execute_tool("save_routine", {"name": "dinner log", "steps": "Ask what we had\nLog it"})
check("same-name edit keeps the schedule and the first spelling",
      (rt.get("Dinner Log")["name"], rt.get("Dinner Log")["schedule"]["time"], len(rt.get("Dinner Log")["steps"])), ("Dinner Log", "22:15", 2))
lib.execute_tool("save_routine", {"change": "Dinner Log", "time": "off"})
check("time off unschedules", rt.get("Dinner Log")["schedule"], None)
check("change to an unknown routine says so",
      lib.execute_tool("save_routine", {"change": "Breakfast", "name": "x"}).startswith("Couldn't save the routine: there's no routine called “Breakfast”"), True)
out = lib.execute_tool("save_routine", {"name": "Dinner", "steps": "Set the table", "new": "yes"})
check("new='yes' makes a separate one", (out.startswith("Saved routine “Dinner”"), len(rt.list_all())), (True, n0 + 1))
check("similar: words inside either way, filler ignored",
      ([r["name"] for r in rt.similar("my dinner log routine")], rt.similar("Breakfast"), rt.similar("routine")),
      (["Dinner", "Dinner Log"], [], []))
# 10-03 audit (#802's shape, the store itself): the Mac's editor sends {id, name, icon, steps, enabled, schedule} and has
# no trigger field; an update by id kept nothing it left out, so every Mac edit cleared a place trigger.
home = rt.save({"name": "Home again", "steps": ["Lights on"], "trigger": {"type": "place", "place": "Home", "on": "arrive"},
                "audience": "private"}, person="p_alex", role="adult")
mac = rt.save({"id": home["id"], "name": "Home again", "icon": "house", "steps": ["Lights on", "Kettle"], "enabled": True,
               "schedule": None}, person="p_alex", role="adult")
check("an update by id keeps a field it leaves out (the place trigger), and its privacy",
      (mac["trigger"], mac["steps"], mac["icon"], mac["audience"]),
      ({"type": "place", "place": "home", "on": "arrive"}, ["Lights on", "Kettle"], "house", "private"))
check("an explicit null still clears", rt.save({"id": home["id"], "trigger": None}, person="p_alex", role="adult")["trigger"], None)
check("…and an id-only update keeps everything else", (rt.get(home["id"], person="p_alex", role="adult") or {}).get("steps"),
      ["Lights on", "Kettle"])
check("tool reports bad input", lib.execute_tool("save_routine", {"name": "x"}).startswith("Couldn't save the routine"), True)
check("delete", (rt.delete(a["id"]), rt.delete(a["id"])), (True, False))

print(f"\n{'All tests passed.' if not fails else f'{fails} FAILED.'}")
sys.exit(1 if fails else 0)
