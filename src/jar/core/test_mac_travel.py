#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#739: a trip needs travel_time, the iPhone's Maps. On a Mac turn (no travel_time) the reply says so and gives no
number: the tool's not-available answer, one line in the Mac's tool rules, and a check that takes out an invented
drive time or leave-by time. A person's own numbers ("it's 20 minutes away", "remind me to leave by 8:30") stay.
Run: python3 scripts/test_mac_travel.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


# A Mac that doesn't run travel_time (before jellybean#547, or one that doesn't report it): #739's not-available path.
MAC_NOW = sorted(n for n, spec in lib._DEVICE_TOOLS.items() if not spec.get("internal") and n not in lib.PHONE_ONLY_TOOLS)
OLD_MAC = lib.device_tools_for_client("mac", sorted(set(MAC_NOW) - {"travel_time"}))
NEW_MAC = lib.device_tools_for_client("mac", MAC_NOW)


def turn(client=None, said="", outcomes=None, tools=OLD_MAC):
    io = lib._TURN_IO
    io.device = lambda req: {"result": "ok"}
    io.client, io.device_tools, io.said, io.outcomes = client, (None if client else tools), said, list(outcomes or [])
    lib._TURN_INFO.info = {}


U = lib.TRIP_UNAVAILABLE
GUESS = "Your dentist appointment is tomorrow at 9:00 AM. It's about 25 minutes away, so leave by 8:30 AM."

print("── an older Mac's turn: no travel_time ──")
turn(said="When do I need to leave for my dentist appointment tomorrow?")
check("travel_time isn't available on the Mac", lib._device_tool_allowed("travel_time"), False)
r = lib.execute_tool("travel_time", {"to": "dentist"})
check("calling it anyway: the plain sentence, no number", (U in r, "never guess" in r), (True, True))
check("…and that refused call doesn't count as the trip", lib._trip_unavailable(), True)
check("the Mac's tool rules say so", U in lib.build_tool_system_prompt(), True)
check("a guessed trip is taken out", lib._with_backed_trip(GUESS),
      "Your dentist appointment is tomorrow at 9:00 AM.\n\n" + U)
check("…and counted", lib._TURN_INFO.info.get("trip_unbacked"), 1)
check("only a leave-by: just the sentence", lib._with_backed_trip("Leave by 8:30 AM to be on time."), U)
for label, text in [("a drive time", "The drive is about 25 minutes."), ("'20-30 min away'", "It's 20-30 min away."),
                    ("'leave around 8'", "You should leave around 8 AM.")]:
    check(f"caught: {label}", lib._with_backed_trip(text).endswith(U), True)
check("no trip in the reply: unchanged", lib._with_backed_trip("Your dentist appointment is tomorrow at 9:00 AM."),
      "Your dentist appointment is tomorrow at 9:00 AM.")

print("\n── the person's own numbers stay ──")
turn(said="The dentist is about 20 minutes away. When should I leave for my 9 AM?")
check("they gave the trip", lib._with_backed_trip("Leave by 8:30 AM: 20 minutes plus a little slack."),
      "Leave by 8:30 AM: 20 minutes plus a little slack.")
turn(said="Remind me to leave by 8:30 tomorrow.")
check("their own leave-by time", lib._with_backed_trip("I'll remind you to leave by 8:30 AM tomorrow."),
      "I'll remind you to leave by 8:30 AM tomorrow.")

print("\n── where travel_time ran or exists: untouched ──")
turn(client="iphone", said="When do I need to leave?")
check("an iPhone turn has it", lib._device_tool_allowed("travel_time"), True)
check("an iPhone turn's reply is untouched", lib._with_backed_trip(GUESS), GUESS)
check("the iPhone's tool rules don't carry the Mac line", U in lib.build_tool_system_prompt(), False)
turn(said="When do I need to leave?", outcomes=[{"tool": "travel_time", "ok": True, "text": "Dentist: 24 min by car"}])
check("a turn where travel_time ran is untouched", lib._with_backed_trip(GUESS), GUESS)

print("\n── a current Mac (jellybean#547) runs travel_time itself ──")
turn(said="When do I need to leave for my dentist appointment tomorrow?", tools=NEW_MAC)
check("it's offered", ("travel_time" in lib._turn_device_tools(), lib._device_tool_allowed("travel_time")), (True, True))
check("the Mac's tool rules don't carry the not-available line", U in lib.build_tool_system_prompt(), False)
MAC_ANSWER = "Dentist (tomorrow 9:00 AM) at Pearl Dental: 18 min by car, 7.2 mi, from this Mac at 10:41 PM. Leave by 8:32 AM."
turn(said="When do I need to leave for my dentist appointment tomorrow?", tools=NEW_MAC,
     outcomes=[{"tool": "travel_time", "ok": True, "text": MAC_ANSWER}])
check("a real Mac travel_time answer backs the reply", lib._with_backed_trip("Leave by 8:32 AM: it's 18 min by car."),
      "Leave by 8:32 AM: it's 18 min by car.")
check("…the check doesn't apply (the tool is there)", lib._trip_unavailable(), False)

print("\n── wired into the reply ──")
src = Path(lib.__file__).read_text()
check("the reply pipeline runs it", "_with_backed_trip(_with_backed_claims(" in src, True)

lib._TURN_IO.device = lib._TURN_IO.client = lib._TURN_IO.said = lib._TURN_IO.outcomes = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
