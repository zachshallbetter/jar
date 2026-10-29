#!/usr/bin/env python3
"""home_status / home_control against a fake Home Assistant: rooms, names, the thermostat,
safe ranges, hands-off domains, ambiguity, and no token. No network."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["HA_TOKEN_FILE"] = str(Path(tempfile.mkdtemp()) / "none")
import home_assistant as ha  # noqa: E402

ok = True


def check(label, got, expected):
    global ok
    good = got == expected
    ok &= good
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


STATES = [
    {"entity_id": "climate.thermostat", "state": "heat", "attributes": {"friendly_name": "Thermostat", "hvac_action": "heating",
     "current_temperature": 68, "temperature": 70, "current_humidity": 41, "hvac_modes": ["off", "heat", "cool", "auto"], "temperature_unit": "°F"}},
    {"entity_id": "light.living_room_lamp", "state": "on", "attributes": {"friendly_name": "Living Room Lamp", "brightness": 153}},
    {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen Lights"}},
    {"entity_id": "switch.porch", "state": "off", "attributes": {"friendly_name": "Porch Light"}},
    {"entity_id": "media_player.living_room_tv", "state": "playing", "attributes": {"friendly_name": "Living Room TV", "media_title": "Bluey"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "cover.garage", "state": "closed", "attributes": {"friendly_name": "Garage Door"}},
    {"entity_id": "media_player.main_bedroom", "state": "idle", "attributes": {"friendly_name": "Main Bedroom", "media_title": "Thunderstorm"}},
    {"entity_id": "remote.main_bedroom", "state": "on", "attributes": {"friendly_name": "Main Bedroom"}},
    {"entity_id": "weather.forecast_home", "state": "partlycloudy", "attributes": {"friendly_name": "Forecast Home"}},
    {"entity_id": "sensor.outdoor_temp", "state": "54", "attributes": {"friendly_name": "Outdoor Temperature", "unit_of_measurement": "°F"}},
    {"entity_id": "light.bedroom_1", "state": "off", "attributes": {"friendly_name": "Bedroom Lamp Left"}},
    {"entity_id": "light.bedroom_2", "state": "off", "attributes": {"friendly_name": "Bedroom Lamp Right"}},
]
AREAS = "\n".join(["climate.thermostat|Hallway", "light.living_room_lamp|Living Room", "media_player.living_room_tv|Living Room",
                   "light.kitchen|Kitchen", "light.bedroom_1|Bedroom", "light.bedroom_2|Bedroom", "lock.front_door|Entry"])
calls = []


def fake(method, path, body):
    calls.append((method, path, body))
    if path == "/api/states":
        return STATES
    if path == "/api/template":
        return AREAS
    return []


print("── status ──")
ha._AREAS["at"] = 0
out = ha.home_status({}, http=fake)
check("grouped by room", out.splitlines()[0], "[Bedroom]")
check("thermostat line", "• Thermostat: heating, 68°F inside, set to 70, mode heat, 41% humidity" in out, True)
check("brightness as a percent", "• Living Room Lamp: on, 60%" in out, True)
check("what's playing", "Living Room TV: playing “Bluey”" in out, True)
check("sensors only when asked", "Outdoor" in out, False)
check("idle isn't 'playing'", "• Main Bedroom: idle" in out and "Thunderstorm" not in out, True)
check("remotes and weather only when asked", ("Main Bedroom: on" in out, "Forecast" in out), (False, False))
check("a remote when asked", "Main Bedroom: on" in ha.home_status({"kind": "remote"}, http=fake), True)
check("one room", ha.home_status({"area": "living room"}, http=fake).count("•"), 2)
check("a kind, by its everyday name", ha.home_status({"kind": "thermostat"}, http=fake).count("•"), 1)
check("sensors when asked", "Outdoor Temperature: 54 °F" in ha.home_status({"kind": "sensor"}, http=fake), True)
check("nothing matches", ha.home_status({"name": "sauna"}, http=fake), "No matching devices in Home Assistant.")

print("── control ──")
calls.clear()
r = ha.home_control({"device": "living room lamp", "action": "off"}, http=fake)
check("light off", (r, calls[-1][1], calls[-1][2]), ("Done: Living Room Lamp → turn off.", "/api/services/light/turn_off", {"entity_id": "light.living_room_lamp"}))
r = ha.home_control({"device": "Thermostat", "action": "set_temperature", "value": "72°F"}, http=fake)
check("thermostat to 72", (calls[-1][1], calls[-1][2]["temperature"], "Honeywell" in r), ("/api/services/climate/set_temperature", 72.0, True))
n = len(calls)
check("out of range refused", ha.home_control({"device": "Thermostat", "action": "set_temperature", "value": 95}, http=fake).startswith("95° is outside"), True)
check("…and nothing sent", [c for c in calls[n:] if c[0] == "POST" and "services" in c[1]], [])
check("unknown mode refused", "modes are" in ha.home_control({"device": "Thermostat", "action": "set_mode", "value": "turbo"}, http=fake), True)
ha.home_control({"device": "Thermostat", "action": "set_mode", "value": "cool"}, http=fake)
check("mode cool", calls[-1][2], {"entity_id": "climate.thermostat", "hvac_mode": "cool"})
ha.home_control({"device": "kitchen lights", "action": "brightness", "value": "40%"}, http=fake)
check("brightness 40%", calls[-1][2], {"entity_id": "light.kitchen", "brightness_pct": 40})
ha.home_control({"device": "Living Room TV", "action": "pause"}, http=fake)
check("tv pause", calls[-1][1], "/api/services/media_player/media_pause")
print("── Apple TV: apps, remote, mute ──")
STATES[4]["attributes"]["source_list"] = ["App\u00a0Store", "Disney+", "Music", "NPR One", "YouTube"]
r = ha.home_control({"device": "Living Room TV", "action": "open", "value": "disney plus"}, http=fake)
check("open an app by name (Disney+ from 'disney plus')", (calls[-1][1], calls[-1][2]),
      ("/api/services/media_player/select_source", {"entity_id": "media_player.living_room_tv", "source": "Disney+"}))
ha.home_control({"device": "Living Room TV", "action": "open", "value": "youtube"}, http=fake)
check("case-insensitive", calls[-1][2]["source"], "YouTube")
r = ha.home_control({"device": "Living Room TV", "action": "open", "value": "Netflix"}, http=fake)
check("an app it doesn't have lists the ones it does", ("no app called “Netflix”" in r, "Disney+" in r, "App Store" in r), (True, True, True))
ha.home_control({"device": "Living Room TV", "action": "press", "value": "Home"}, http=fake)
check("a remote button goes to the TV's remote entity", (calls[-1][1], calls[-1][2]),
      ("/api/services/remote/send_command", {"entity_id": "remote.living_room_tv", "command": "home"}))
ha.home_control({"device": "Living Room TV", "action": "press", "value": "back"}, http=fake)
check("'back' is the menu button", calls[-1][2]["command"], "menu")
check("an unknown button lists them", "remote's buttons are" in ha.home_control({"device": "Living Room TV", "action": "press", "value": "turbo"}, http=fake), True)
ha.home_control({"device": "Living Room TV", "action": "mute"}, http=fake)
check("mute", (calls[-1][1], calls[-1][2].get("is_volume_muted")), ("/api/services/media_player/volume_mute", True))
n = len(calls)
check("a lock is hands-off", ha.home_control({"device": "Front Door", "action": "off"}, http=fake).startswith("Front Door is a lock"), True)
check("a garage door is hands-off", "cover" in ha.home_control({"device": "Garage Door", "action": "on"}, http=fake), True)
check("…and nothing sent to either", [c for c in calls[n:] if c[0] == "POST"], [])
amb = ha.home_control({"device": "bedroom lamp", "action": "on"}, http=fake)
check("ambiguous → asks", amb.startswith("Several devices match") and "Bedroom Lamp Left" in amb, True)
check("unknown device", ha.home_control({"device": "hot tub", "action": "on"}, http=fake), "No device called “hot tub” in Home Assistant.")
check("unsupported action", "isn't something titan can do" in ha.home_control({"device": "Porch Light", "action": "brightness", "value": 5}, http=fake), True)

print("── auto (heat_cool) holds a range ──")
STATES.append({"entity_id": "climate.honeywell", "state": "heat_cool", "attributes": {"friendly_name": "Hallway Thermostat",
               "current_temperature": 73, "target_temp_low": 69, "target_temp_high": 73, "hvac_modes": ["off", "heat", "cool", "heat_cool"],
               "temperature_unit": "°F"}})
r = ha.home_control({"device": "Hallway Thermostat", "action": "set_temperature", "value": 72}, http=fake)
check("one number on Auto centres the range, same width", calls[-1][2],
      {"entity_id": "climate.honeywell", "target_temp_low": 70.0, "target_temp_high": 74.0})
check("… keeps the mode and says so", ("70–74°" in r, "(was 69–73)" in r, "mode is unchanged" in r,
                                        "_note" in str(calls[-1][2])), (True, True, True, False))
check("… never a mode change", [c for c in calls if "hvac_mode" in str(c) and "honeywell" in str(c)], [])
n_before = len(calls)
r = ha.home_control({"device": "Hallway Thermostat", "action": "set_temperature", "value": 72}, http=fake, dry_run=True)
check("a dry run plans the real call and sends nothing",
      (r.startswith("Done (eval: not sent): Hallway Thermostat"), "70–74°" in r, [c for c in calls[n_before:] if c[0] == "POST"]),
      (True, True, []))
r = ha.home_control({"device": "Hallway Thermostat", "action": "set_temperature", "value": 89}, http=fake)
check("a range around it out of bounds is refused", "outside the safe range" in r, True)
ha.home_control({"device": "Hallway Thermostat", "action": "set_temperature", "value": "68-72"}, http=fake)
check("a range sets low and high", calls[-1][2], {"entity_id": "climate.honeywell", "target_temp_low": 68.0, "target_temp_high": 72.0})
ha.home_control({"device": "Hallway Thermostat", "action": "set_temperature", "value": "72 to 67"}, http=fake)
check("a range in either order", (calls[-1][2]["target_temp_low"], calls[-1][2]["target_temp_high"]), (67.0, 72.0))
check("too narrow a range is refused", "at least 2" in ha.home_control({"device": "Hallway Thermostat", "action": "set_temperature", "value": "70-71"}, http=fake), True)
check("a range out of bounds is refused", "outside the safe range" in ha.home_control({"device": "Hallway Thermostat", "action": "set_temperature", "value": "60-95"}, http=fake), True)

print("── not connected ──")
check("no token says how to connect", ha.home_status({}).startswith("Home Assistant isn't connected"), True)
check("control too", ha.home_control({"device": "x", "action": "on"}).startswith("Home Assistant isn't connected"), True)

print("── wired into the agent ──")
import agent_runtime_lib as lib  # noqa: E402
check("tools registered", all(t in lib._TOOL_REGISTRY for t in ("home_status", "home_control")), True)
check("status labels", (lib._TOOL_STATUS["home_status"], lib._TOOL_STATUS["home_control"]), ("Checking the house…", "Adjusting the house…"))
check("through execute_tool", lib.execute_tool("home_status", {}).startswith("Home Assistant isn't connected"), True)

print("\nAll tests passed" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
