#!/usr/bin/env python3
# gate: hermetic   (a fake Home Assistant; nothing leaves the process)
"""jellybean#431: "why is it warm / why are the lights on?" from Home Assistant's own history: the change, what did it
(an automation, a household member by first name, a device) and when. Never locks, alarms, garage doors, cameras or
anyone's location. Errors carry titan's own words and a code.
Run: python3 scripts/test_home_why.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TZ"] = "America/Los_Angeles"
sys.path.insert(0, str(Path(__file__).parent))

import home_assistant as ha  # noqa: E402
import home_why as hw  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)   # 8:00 AM PDT
STATES = [
    {"entity_id": "climate.thermostat", "state": "heat", "attributes": {"friendly_name": "Thermostat", "current_temperature": 71,
     "temperature": 72, "temperature_unit": "°F"}},
    {"entity_id": "light.living_room_lamp", "state": "on", "attributes": {"friendly_name": "Living Room Lamp"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen Lights"}},
    {"entity_id": "light.bedroom", "state": "off", "attributes": {"friendly_name": "Bedroom Lamp"}},
    {"entity_id": "media_player.living_room_tv", "state": "playing", "attributes": {"friendly_name": "Living Room TV"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "Garage Door"}},
    {"entity_id": "alarm_control_panel.home", "state": "armed_home", "attributes": {"friendly_name": "Home Security"}},
    {"entity_id": "lock.back_entry", "state": "locked", "attributes": {"friendly_name": "Back Entry"}},
    {"entity_id": "camera.porch", "state": "idle", "attributes": {"friendly_name": "Porch Camera"}},
    {"entity_id": "person.alex", "state": "home", "attributes": {"friendly_name": "Alex Rivera", "user_id": "u-alex"}},
]
AREAS = "\n".join(["climate.thermostat|Hallway", "light.living_room_lamp|Living Room", "light.kitchen|Kitchen",
                   "light.bedroom|Bedroom", "media_player.living_room_tv|Living Room"])
LOGBOOK = {
    "climate.thermostat": [
        {"when": "2026-10-03T13:30:00+00:00", "state": "heat", "entity_id": "climate.thermostat",
         "context_entity_id": "automation.morning_warm_up", "context_entity_id_name": "Morning warm-up"},
    ],
    "light.living_room_lamp": [
        {"when": "2026-10-03T14:05:00+00:00", "state": "on", "entity_id": "light.living_room_lamp", "context_user_id": "u-alex"},
    ],
    "light.kitchen": [
        {"when": "2026-10-03T14:10:00+00:00", "state": "on", "entity_id": "light.kitchen", "context_user_id": "u-stranger"},
    ],
}
HISTORY = [[
    {"entity_id": "climate.thermostat", "state": "heat", "last_updated": "2026-10-03T13:00:00+00:00",
     "attributes": {"temperature": 68, "temperature_unit": "°F"}},
    {"entity_id": "climate.thermostat", "state": "heat", "last_updated": "2026-10-03T13:30:02+00:00",
     "attributes": {"temperature": 72, "temperature_unit": "°F"}},
]]
calls = []


def fake(method, path, body):
    calls.append(path)
    if path == "/api/states":
        return STATES
    if path == "/api/template":
        return AREAS
    if path.startswith("/api/logbook/"):
        eid = path.split("entity=", 1)[1].split("&", 1)[0].replace("%2E", ".")
        return LOGBOOK.get(eid, [])
    if path.startswith("/api/history/period/"):
        return HISTORY
    return []


def ask(target, hours=12):
    ha._AREAS["at"] = 0
    return hw.why(target, hours, http=fake, now=NOW)


print("── the heat ──")
st, b = ask("why is it warm")
check("200, the thermostat", (st, b["target"], b["kind"], b["area"]), (200, "Thermostat", "climate", "Hallway"))
check("now", b["now"], "Heat · 71°F (set 72°F)")
check("events: the mode by the automation, the setpoint matched to it",
      [(e["what"], e["by"]["kind"], e["by"]["name"]) for e in b["events"]],
      [("turned to Heat", "automation", "Morning warm-up"), ("set to 72°F", "automation", "Morning warm-up")])
check("times are ISO with the household's offset", (b["events"][0]["at"], b["since"]),
      ("2026-10-03T06:30:00-07:00", "2026-10-02T20:00:00-07:00"))
check("the answer, built in code", b["answer"],
      "Thermostat was set to 72°F at 6:30 AM by the Morning warm-up automation; before that, turned to Heat at 6:30 AM "
      "by the Morning warm-up automation. Now: Heat · 71°F (set 72°F).")

print("── lights and people ──")
st, b = ask("why are the lights on")
check("two lights on: ask which, as buttons", (st, b["code"], sorted(c["target"] for c in b["candidates"])),
      (409, "ambiguous", ["Kitchen Lights", "Living Room Lamp"]))
check("…each with its room", sorted((c["name"], c["area"]) for c in b["candidates"]),
      [("Kitchen Lights", "Kitchen"), ("Living Room Lamp", "Living Room")])
st, b = ask("living room lights")
check("a room narrows it", (st, b["target"]), (200, "Living Room Lamp"))
check("a household member by first name only", b["events"][0]["by"], {"kind": "person", "name": "Alex"})
st, b = ask("Kitchen Lights")
check("an HA user with no person entity is 'someone'", b["events"][0]["by"], {"kind": "person", "name": "someone"})

print("── never ──")
for t in ("why is the front door locked", "garage", "the porch camera", "where is Alex", "Front Door",
          "why is the alarm armed", "Home Security", "Back Entry", "the garage door opened"):
    st, b = ask(t)
    check(f"hands off: {t!r}", (st, b["code"], "error" in b), (403, "hands_off", True))
check("a hands-off device's history is never fetched",
      any(x in c for c in calls for x in ("lock.", "alarm_control_panel", "camera.", "cover.")), False)
check("no location is ever read: the person entity only names", any("person.alex" in c for c in calls), False)

print("── errors carry words and a code ──")
st, b = ask("the hot tub")
check("not found", (st, b["code"], "hot tub" in b["error"]), (404, "not_found", True))
st, b = ask("")
check("nothing asked", (st, b["code"]), (400, "bad_request"))
st, b = hw.why("the heat", http=lambda m, p, x: (_ for _ in ()).throw(PermissionError("no token")), now=NOW)
check("Home Assistant not connected", (st, b["code"]), (503, "ha_not_connected"))

print("── quiet, and the tool ──")
LOGBOOK["light.bedroom"] = []
st, b = ask("bedroom lamp")
check("no changes: says so", b["answer"], "Bedroom Lamp hasn't changed in the last 12 hours; it's Off.")
orig = hw.why
hw.why = lambda target, hours=12, **k: orig(target, hours, http=fake, now=NOW)
check("home_why tool: the answer in words", hw.tool({"target": "living room lights"}).startswith("Living Room Lamp was turned on"), True)
check("…and a question when it's ambiguous", hw.tool({"target": "lights"}), "Which one: Kitchen Lights (Kitchen), Living Room Lamp (Living Room)?")
hw.why = orig

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
