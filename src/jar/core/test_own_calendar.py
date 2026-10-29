#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A new event goes on the asker's own calendar unless it's a family event (titan-node#629). Zach, 10-02:
"Unless it's a family affair, appointments go into the individual's calendar; that's the default." (His Maple Health
dentist event had landed on a shared calendar.)
  - no calendar named: the asker's own (the first of theirs in Settings › Household);
  - for: household/family: the device's default (the shared one);
  - a calendar the person named: as is;
  - for a kid, by a parent: the kid's; for another adult: the asker's own;
  - the device has no calendar by that name: said, and the model asks; it isn't put elsewhere;
  - the result says which calendar it went on.
Temp stores, a fake device. Run: python3 scripts/test_own_calendar.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-owncal-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


st = hh.HouseholdStore(_TMP / "people.db")
hh.reset_store(st)
zach = hh.ADMIN_PERSON_ID
st.update_person(zach, calendars=["Zach", "Work"])
rob = st.add_person("Robin", "adult")["id"]
st.update_person(rob, calendars=["Robin"])
ava = st.add_person("Ava", "kid")["id"]
st.update_person(ava, calendars=["Ava"], parents=[zach, rob])
jor = st.add_person("Jordan", "adult")["id"]          # no calendars of her own set


def as_(pid, role):
    lib._TURN_IO.person = {"id": pid, "name": st.person(pid)["name"]}
    lib._TURN_IO.role = role


def pick(args):
    a, whose = lib._default_calendar(args)
    return a.get("calendar"), (whose or {}).get("name")


EV = {"title": "Maple Health dentist", "start": "2026-10-09T10:15"}
print("── which calendar ──")
as_(zach, "admin")
check("no calendar named: the asker's own, the first of theirs", pick(EV), ("Zach", "Zach"))
check("for: me — the same", pick({**EV, "for": "me"}), ("Zach", "Zach"))
check("their own first name — the same", pick({**EV, "for": "Zach"}), ("Zach", "Zach"))
for fam in ("household", "family", "Everyone"):
    check(f"for: {fam} — left to the device's default (the shared calendar)", pick({**EV, "for": fam}), (None, None))
check("a calendar the person named: as is, nothing chosen", pick({**EV, "calendar": "Family"}), ("Family", None))
check("for Ava, by her parent: Ava's calendar", pick({**EV, "for": "Ava"}), ("Ava", "Ava"))
as_(rob, "adult")
check("Robin's own", pick(EV), ("Robin", "Robin"))
check("Robin for Ava (her parent): Ava's", pick({**EV, "for": "ava"}), ("Ava", "Ava"))
check("Robin for Jordan (another adult): her own", pick({**EV, "for": "Jordan"}), ("Robin", "Robin"))
as_(ava, "kid")
check("a kid's own", pick(EV), ("Ava", "Ava"))
check("a kid for her mom: her own, not Robin's", pick({**EV, "for": "Robin"}), ("Ava", "Ava"))
as_(jor, "adult")
check("someone with no calendar set: one named for them (their first name)", pick(EV), ("Jordan", "Jordan"))
check("…marked as a guess", lib._default_calendar(EV)[1].get("guessed"), True)
lib._TURN_IO.person = None
check("no person (a background turn): unchanged", pick(EV), (None, None))

print("── through the device ──")
sent: list[dict] = []
CALS = ["Zach", "Family", "Ava"]


def device(req):
    if req["tool"] == "list_calendar_events":
        return {"result": "• Fri Oct 9, 10:15 AM–11:15 AM — Maple Health dentist [calendar: Zach]"}
    sent.append(req["args"])
    cal = req["args"].get("calendar") or "Family"
    if cal not in CALS:
        return {"result": f"Not created: there's no calendar called “{cal}”. The calendars it can go on: "
                          + ", ".join(CALS) + "."}
    return {"result": f"Created 'Maple Health dentist' on Fri Oct 9, 10:15 AM on the {cal} calendar."}


lib._TURN_IO.device = device
lib._TURN_IO.client = None
lib._TURN_IO.device_tools = None
as_(zach, "admin")
out = lib._execute_tool("create_calendar_event", dict(EV))
check("the device gets the asker's calendar", sent[-1].get("calendar"), "Zach")
check("the result names it, and says why", ("on the Zach calendar" in out,
      "[calendar] It went on “Zach”, the person's own calendar" in out), (True, True))
out = lib._execute_tool("create_calendar_event", {**EV, "for": "household"})
check("a family event: no calendar sent, the device's default", ("calendar" in sent[-1], "Family calendar" in out,
      "[calendar]" in out), (False, True, False))
out = lib._execute_tool("create_calendar_event", {**EV, "for": "Ava"})
check("for Ava: on Ava's, said so", (sent[-1].get("calendar"), "Ava's calendar" in out), ("Ava", True))
as_(rob, "adult")
out = lib._execute_tool("create_calendar_event", dict(EV))
check("a device without that calendar: not created, and the model is told to ask, not pick",
      (out.startswith("Not created: there's no calendar called “Robin”"), "Ask which calendar to use" in out,
       len([a for a in sent if a.get("calendar") == "Robin"])), (True, True, 1))
# No calendar set: the guess. The device has one called Jordan? No: it refuses, and the model asks and offers to save.
as_(jor, "adult")
out = lib._execute_tool("create_calendar_event", dict(EV))
check("a guessed calendar the device lacks: not created; ask from the list; offer to remember it, only on a yes",
      (sent[-1].get("calendar"), out.startswith("Not created"), "No calendar is set as theirs" in out,
       "only on their yes, call set_preference with name calendar" in out), ("Jordan", True, True, True))
CALS.append("Jordan")
out = lib._execute_tool("create_calendar_event", dict(EV))
check("…and one it has: created there", ("on the Jordan calendar" in out, "the person's own calendar" in out), (True, True))
CALS.remove("Jordan")

print("── saving their calendar, only on a yes ──")
lib._TURN_IO.said = "put it on Family I guess"
check("no yes, not named: not saved", lib._execute_tool("set_preference", {"name": "calendar", "value": "Home"}).startswith(
      "Not saved: they haven't said yes"), True)
check("…nothing stored", st.person_facts(jor)["calendars"], [])
lib._TURN_IO.said = "Yes, use Home from now on"
out = lib._execute_tool("set_preference", {"name": "calendar", "value": "Home"})
check("their yes: saved as theirs, first", (out.startswith("Saved: “Home” is Jordan's calendar"), st.person_facts(jor)["calendars"]),
      (True, ["Home"]))
check("…and the next event goes there", pick(EV), ("Home", "Jordan"))
lib._TURN_IO.said = "make it the Work one"
as_(zach, "admin")
lib._execute_tool("set_preference", {"name": "calendar", "value": "Work"})
check("naming the calendar counts; it moves to the front, no duplicate", st.person_facts(zach)["calendars"], ["Work", "Zach"])
st.update_person(zach, calendars=["Zach", "Work"])
lib._TURN_IO.said = None

# A held card's Do it (#610) still matches though titan added the calendar after it was shown.
as_(zach, "admin")
lib._TURN_IO.said = "put it in my calendar"
held = {"title": "Maple Health dentist", "start": "2026-10-09T10:15"}
import json as _json  # noqa: E402
lib._TURN_IO.confirmed_call = ("create_calendar_event", _json.dumps(held, sort_keys=True, default=str))
check("a confirmed held event isn't refused for its time once the calendar is added",
      lib._guessed_time("create_calendar_event", {**held, "calendar": "Zach"}), None)
lib._TURN_IO.confirmed_call = None
check("…while an unconfirmed one with no time said still is",
      (lib._guessed_time("create_calendar_event", {**held, "calendar": "Zach"}) or "").startswith("Not created"), True)
lib._TURN_IO.said = None
lib._TURN_IO.device = None
lib._TURN_IO.person = None
lib._TURN_IO.role = None

print("── the model is told ──")
spec = lib._DEVICE_TOOLS["create_calendar_event"]["parameters"]
check("calendar: only when the user names one; otherwise their own", "only when the user names one" in spec["calendar"]
      and "person's own calendar" in spec["calendar"], True)
check("for: 'household' for a family event", "'household' for a family event" in spec["for"], True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
