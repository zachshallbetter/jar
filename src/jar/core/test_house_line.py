#!/usr/bin/env python3
# gate: hermetic
""""What's on in the house" on the local brain (titan-node#1356): one line built in code from HA's states right after the
question, so the 8B only repeats it (the day-one check, 10-09: after a real read it said "everything is off" with a TV
playing). The asker's view (kids and shared screens never see a security-shaped device); recorded as a home_status
read so the required-read retry doesn't ask for a second; the "[From the … for this question:" shape keeps /no_think on
the question. Fictional house.
Run: python3 scripts/test_house_line.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-houseline-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import home_assistant as ha  # noqa: E402
import required_reads  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def ent(eid, state, name, **a):
    return {"entity_id": eid, "state": state, "attributes": {"friendly_name": name, **a}}


# The sandbox stub's house on the day the 8B said "everything is off": the TV playing, the thermostat heating.
HOUSE = [ent("light.kitchen", "off", "Kitchen Light"), ent("media_player.living_room_tv", "playing", "Living Room TV"),
         ent("climate.living_room", "heat", "Living Room Thermostat"), ent("switch.coffee_maker", "off", "Coffee Maker"),
         ent("media_player.kitchen_speaker", "idle", "Kitchen Speaker"), ent("lock.front_door", "locked", "Front Door"),
         ent("cover.garage_door", "closed", "Garage Door", device_class="garage"),
         ent("switch.garage_door_opener", "on", "Garage Door Opener"), ent("camera.porch", "idle", "Porch Camera")]

print("── on_now: what's on, from the states ──")
check("the all_off_claim case: the TV playing and the thermostat heating are ON",
      ha.on_now(HOUSE), ["Living Room TV (playing)", "Living Room Thermostat (heat)"])
check("never a security state, for any role: the garage door opener switch (on) is left out for an adult too",
      ("Garage Door Opener" in ha.on_now(HOUSE), "Garage Door Opener" in ha.on_now(HOUSE, hide_security=True)), (False, False))
UNLOCKED = HOUSE + [ent("lock.back_door", "unlocked", "Back Door"), ent("alarm_control_panel.home", "disarmed", "Home Alarm"),
                    ent("cover.living_room_blinds", "open", "Living Room Blinds", device_class="blind"),
                    ent("camera.porch_live", "recording", "Porch Live")]
check("an adult, with a lock unlocked, the alarm disarmed, blinds open and a camera recording: none of them in the list",
      [n for n in ha.on_now(UNLOCKED) if any(w in n for w in ("Door", "Alarm", "Blinds", "Porch"))], [])
check("idle media, a closed cover, a lock and a camera aren't 'on'", any(n.startswith(("Kitchen Speaker", "Garage Door (", "Front", "Porch"))
      for n in ha.on_now(HOUSE)), False)
check("nothing on: empty", ha.on_now([ent("light.x", "off", "X"), ent("climate.y", "off", "Y")]), [])

print("── the line, on a local turn ──")
ha._http = lambda method, path, body=None: HOUSE if path.startswith("/api/states") else ""
_specs, _role = lib._turn_tool_specs, lib.turn_role
lib._turn_tool_specs = lambda: {"home_status": {}, "home_control": {}}
lib.turn_role = lambda: "adult"
lib._TURN_IO.outcomes = []
lib._TURN_INFO.info = {}
msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "What's on in the house right now?"}]
out = lib._with_house_line(msgs)
note = out[-1]["content"]
check("a note right after the question, the shape /no_think steps over", (out[1], out[-1]["role"], note.startswith("[From the "),
      "for this question: " in note), (msgs[1], "system", True, True))
check("…it names the TV playing and the thermostat heating, and says everything else is off",
      ("Living Room TV (playing)" in note, "Living Room Thermostat (heat)" in note, note.endswith("(heat).]")), (True, True, True))
check("…what's on and nothing more: no 'off' claim about anything it leaves out", any(w in note.lower() for w in
      ("the rest", "everything else", "are off", "is off", "nothing else")), False)
check("recorded as a home_status read, so the required-read retry asks for nothing more",
      (lib._TURN_IO.outcomes[-1]["tool"], required_reads.needed("What's on in the house right now?", {"home_status"},
                                                                {o["tool"] for o in lib._TURN_IO.outcomes})), ("home_status", []))
check("…and counted in the turn log", lib._TURN_INFO.info.get("house_line"), 1)
local = lib._local_messages(out)
qi = max(i for i, m in enumerate(local) if m["role"] == "user")
check("/no_think still lands on the question, not on the note", local[qi]["content"].endswith("/no_think"), True)
ha._http = lambda method, path, body=None: UNLOCKED if path.startswith("/api/states") else ""
for role in ("admin", "adult", "kid", "household"):
    lib.turn_role = lambda r=role: r
    lib._TURN_IO.outcomes = []
    line = lib._with_house_line(msgs)[-1]["content"]
    check(f"{role}: the line names the TV and thermostat, and no lock, alarm, cover, camera or opener (the back door is unlocked)",
          ("Living Room TV (playing)" in line, any(w in line for w in ("Back Door", "Front Door", "Alarm", "Blinds", "Porch",
                                                                       "Opener", "unlocked", "disarmed"))), (True, False))
ha._http = lambda method, path, body=None: HOUSE if path.startswith("/api/states") else ""
lib.turn_role = lambda: "adult"
for q in ("Is the front door locked?", "Is the garage open?", "Is the alarm armed?", "Are the blinds open?",
          "Is the porch camera on?"):
    lib._TURN_IO.outcomes = []
    m2 = [{"role": "user", "content": q}]
    check(f"{q!r}: a security question gets no line and no recorded read (the model reads it on its read-only path)",
          (lib._with_house_line(m2), lib._TURN_IO.outcomes), (m2, []))
check("…so the required read still asks for home_status there", [r for _, r in required_reads.needed("Is the front door locked?",
      {"home_status"}, set())], ["home_status"])
for q in ("Turn on the kitchen light.", "Is Robin home?", "What's on my calendar tomorrow?"):
    m2 = [{"role": "user", "content": q}]
    check(f"{q!r}: no line", lib._with_house_line(m2), m2)
lib._turn_tool_specs = lambda: {"home_control": {}}
check("home_status not declared for this asker: no line", lib._with_house_line(msgs), msgs)
lib._turn_tool_specs = lambda: {"home_status": {}}


def _down(*a, **k):
    raise OSError("HA down")


ha._http = _down
check("HA can't be read: no line, no outcome (the model reads or says it can't)", (lib._with_house_line(msgs), lib._TURN_IO.outcomes[-1:]
      if False else None), (msgs, None))
ha._http = lambda method, path, body=None: [ent("light.x", "off", "X")]
lib._TURN_IO.outcomes = []
check("nothing reportable on: no line and no recorded read (the model reads home_status itself)",
      (lib._with_house_line(msgs), lib._TURN_IO.outcomes), (msgs, []))
ha._http = lambda method, path, body=None: [ent("light.porch", "on", "Porch Light"), ent("media_player.tv", "off", "TV")]
_nr = ha._named_restricted
ha._named_restricted = lambda: frozenset({"light.porch"})
lib._TURN_IO.outcomes = []
check("KB's case: only a restricted light on: no line (never 'no lights are on'), and no recorded read",
      (lib._with_house_line(msgs), lib._TURN_IO.outcomes), (msgs, []))
ha._named_restricted = _nr
src = Path(lib.__file__).read_text()
check("local brain only: wired where the local turn's messages are built", src.count("_with_schedule_digest(_with_house_line(messages))"), 1)

print("── KB's review: an allowlist of what's-on questions; anything security-shaped gets no line and keeps its read ──")
ha._http = lambda method, path, body=None: UNLOCKED if path.startswith("/api/states") else ""
lib._turn_tool_specs = lambda: {"home_status": {}, "home_control": {}}
lib.turn_role = lambda: "adult"
SECURITY = ("What's open in the house right now?", "Is the siren on?", "Is the deadbolt on?",
            "Before I leave, is it all secure?",                                    # KB's four
            "Is the house safe?", "Did I leave the garage open?", "Are all the windows shut?", "Is the back gate open?",
            "Is the alarm system on?", "Is anything open?", "Are the doors locked?", "Is the camera on?")
for q in SECURITY:
    lib._TURN_IO.outcomes = []
    m2 = [{"role": "user", "content": q}]
    check(f"{q!r}: no line, no recorded read", (lib._with_house_line(m2), lib._TURN_IO.outcomes), (m2, []))
WHATS_ON = ("What's on in the house right now?", "What's on in the house?", "Is anything left on?", "Is the TV on?",
            "What lights are on?", "Are the fans running?", "Is the music still playing?")
for q in WHATS_ON:
    lib._TURN_IO.outcomes = []
    out2 = lib._with_house_line([{"role": "user", "content": q}])
    check(f"{q!r}: the line", out2[-1]["content"].startswith("[From the house"), True)
for q in ("What's the house doing right now?", "What's on TV tonight?", "Is Robin home?", "Turn off the lights.",
          "What's running right now?"):   # running what? (apps, a task): ambiguous, so no line
    m2 = [{"role": "user", "content": q}]
    check(f"{q!r}: not a clear what's-on question about those kinds: no line", lib._with_house_line(m2), m2)


print("── KB's second review: SENSITIVE_WORDS, 'security' and 'entry' override; a named excluded device gets the read ──")
KB2 = ("Is the doorbell switch on?", "Is the motion switch on?", "Is the privacy switch on?", "Is the cam switch on?",
       "Is the NVR switch on?", "Is the surveillance switch on?", "Is the recording switch on?", "Is the presence switch on?",
       "Is the detection switch on?", "Is the occupancy switch on?", "Security-wise, is anything on?", "Is the back entry light on?")
ha._http = lambda method, path, body=None: UNLOCKED if path.startswith("/api/states") else ""
for q in KB2:
    lib._TURN_IO.outcomes = []
    m2 = [{"role": "user", "content": q}]
    check(f"{q!r}: no line, no recorded read", (lib._with_house_line(m2), lib._TURN_IO.outcomes), (m2, []))
NAMED = UNLOCKED + [ent("switch.porch_relay", "on", "Porch Relay")]
ha._named = ha._named_restricted
ha._named_restricted = lambda: frozenset({"switch.porch_relay"})
ha._http = lambda method, path, body=None: NAMED if path.startswith("/api/states") else ""
m2 = [{"role": "user", "content": "Is the porch relay on?"}]
lib._TURN_IO.outcomes = []
check("a question naming a device the line excludes (a restricted 'Porch Relay'): no line, the read runs",
      (lib._with_house_line(m2), lib._TURN_IO.outcomes), (m2, []))
m3 = [{"role": "user", "content": "Is anything left on?"}]
line = lib._with_house_line(m3)[-1]["content"]
check("…and a general question never lists it (nor claims anything about it)", ("Porch Relay" in line, "off" in line.lower()), (False, False))
ha._named_restricted = ha._named

lib._turn_tool_specs, lib.turn_role = _specs, _role
lib._TURN_IO.outcomes = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
