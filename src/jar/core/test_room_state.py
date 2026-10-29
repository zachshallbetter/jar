#!/usr/bin/env python3
# gate: hermetic
"""A room's state, never a person's (jellybean#433 slice 2): occupied, empty, asleep or unknown, from Home Assistant's
motion and presence sensors by area (never one on a camera's device) and quiet hours (22:00-07:00, or a room's own in
rooms.yaml). A hand-off to a room that's asleep is held (409) until "send anyway". A fake Home Assistant that refuses
any write; a pinned clock; throwaway stores; made-up people.
Run: python3 scripts/test_room_state.py"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-roomstate-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "HANDOFF_DB": "handoff.db", "ROOMS_FILE": "rooms.yaml"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import titan_alert  # noqa: E402
titan_alert.alert = lambda *a, **k: None

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


import room_state as rs  # noqa: E402

# ── a fake Home Assistant: reads only; any other call is a failure ─────────────────────────────────────────────
writes: list[str] = []


def ha(states, cams=(), down=False):
    def http(method, path, body):
        if down:
            raise OSError("Home Assistant down")
        if method == "GET" and path == "/api/states":
            return states
        if method == "POST" and path == "/api/template" and "camera" in body["template"]:
            return "\n".join(cams)
        writes.append(f"{method} {path}")
        raise AssertionError("no writes")
    return http


AREAS = {"binary_sensor.bed_presence": "Bedroom", "binary_sensor.bed_motion": "Bedroom",
         "binary_sensor.hall_motion": "Hall", "binary_sensor.den_presence": "Den",
         "binary_sensor.cam_motion": "Porch", "binary_sensor.door": "Hall"}
areas = lambda http: AREAS   # noqa: E731


def s(eid, cls, state):
    return {"entity_id": eid, "state": state, "attributes": {"device_class": cls}}


def at(h, m=0):
    return datetime(2026, 10, 5, h, m)


def state(room, sensors, now_local, *, cams=(), down=False, now=1_000_000.0, fresh_reset=True):
    if fresh_reset:
        rs.reset_cache()
    return rs.states([room], now=now, now_local=now_local, http=ha(sensors, cams, down), areas=areas)[0]


print("decide: each row of the contract's table")
PRES_OFF, MOT_OFF = s("binary_sensor.bed_presence", "presence", "off"), s("binary_sensor.bed_motion", "motion", "off")
PRES_ON, MOT_ON = s("binary_sensor.bed_presence", "presence", "on"), s("binary_sensor.bed_motion", "motion", "on")
day, night = at(14), at(23, 30)
check("day, a sensor on: occupied", state("Bedroom", [PRES_ON, MOT_OFF], day)["state"], "occupied")
check("day, a presence sensor, all off: empty", state("Bedroom", [PRES_OFF, MOT_OFF], day)["state"], "empty")
check("day, motion-only and off: unknown",
      state("Hall", [s("binary_sensor.hall_motion", "motion", "off")], day)["state"], "unknown")
check("day, no sensors: unknown (no sensor)", {k: state("Kitchen", [], day)[k] for k in ("state", "why")},
      {"state": "unknown", "why": "no sensor"})
check("night, presence on: asleep (sensor)", {k: state("Bedroom", [PRES_ON, MOT_OFF], night)[k] for k in ("state", "why")},
      {"state": "asleep", "why": "sensor"})
check("night, motion on: occupied (someone's up)", state("Bedroom", [PRES_ON, MOT_ON], night)["state"], "occupied")
check("night, a presence sensor, all off: empty", state("Bedroom", [PRES_OFF, MOT_OFF], night)["state"], "empty")
check("night, motion-only and off: asleep (can't tell empty from asleep)",
      state("Hall", [s("binary_sensor.hall_motion", "motion", "off")], night)["state"], "asleep")
row = state("Kitchen", [], night)
check("night, no sensors: asleep by quiet hours, until 7:00", (row["state"], row["why"], row.get("until")),
      ("asleep", "quiet hours", "7:00"))

print("never a camera's sensor, never a non-occupancy one, never a write")
check("a motion sensor on a camera's device is ignored (Porch has none then)",
      state("Porch", [s("binary_sensor.cam_motion", "motion", "on")], day, cams=["binary_sensor.cam_motion"])["why"],
      "no sensor")
check("a door contact isn't occupancy", state("Hall", [s("binary_sensor.door", "door", "on")], day)["why"], "no sensor")
check("only reads: GET /api/states and the camera template", writes, [])

print("stale and unavailable: unknown (or quiet hours)")
check("a room sensor unavailable, by day: unknown (stale)",
      {k: state("Bedroom", [s("binary_sensor.bed_presence", "presence", "unavailable")], day)[k] for k in ("state", "why")},
      {"state": "unknown", "why": "stale"})
check("…at night: the quiet-hours fallback",
      state("Bedroom", [s("binary_sensor.bed_presence", "presence", "unavailable")], night)["state"], "asleep")
check("Home Assistant down, by day: unknown", state("Bedroom", [], day, down=True)["state"], "unknown")
check("Home Assistant down, at night: asleep by quiet hours", state("Bedroom", [], night, down=True)["state"], "asleep")
rs.reset_cache()
good = rs.states(["Bedroom"], now=1000.0, now_local=day, http=ha([PRES_ON]), areas=areas)[0]["state"]
later = rs.states(["Bedroom"], now=1000.0 + 6 * 60, now_local=day, http=ha([], down=True), areas=areas)[0]
check("a good read, then 6 min of failures: stale → unknown", (good, later["state"], later["why"]), ("occupied", "unknown", "stale"))
rs.reset_cache()
rs.states(["Bedroom"], now=1000.0, now_local=day, http=ha([PRES_ON]), areas=areas)
cached = rs.states(["Bedroom"], now=1030.0, now_local=day, http=ha([], down=True), areas=areas)[0]["state"]
check("within a minute, the cached read stands", cached, "occupied")

print("quiet hours: 22:00-07:00 by default, a room's own from rooms.yaml")
check("21:59 isn't quiet, 22:00 is, 06:59 is, 07:00 isn't",
      [rs.in_quiet(at(21, 59), rs.DEFAULT_QUIET), rs.in_quiet(at(22), rs.DEFAULT_QUIET),
       rs.in_quiet(at(6, 59), rs.DEFAULT_QUIET), rs.in_quiet(at(7), rs.DEFAULT_QUIET)], [False, True, True, False])
Path(os.environ["ROOMS_FILE"]).write_text("rooms:\n  Kids room:\n    quiet: \"19:30-07:00\"\n  Den:\n    quiet: \"tea time\"\n"
                                          "  Office:\n    quiet: \"25:00-07:00\"\n")
check("a room's own wins (Kids room asleep at 20:00, until 7:00)",
      {k: state("Kids room", [], at(20))[k] for k in ("state", "until")}, {"state": "asleep", "until": "7:00"})
check("…another room keeps the default at 20:00", state("Kitchen", [], at(20))["state"], "unknown")
check("an invalid override falls back to the default", sorted(rs.overrides()), ["kids room"])
check("…so Den at 20:00 isn't quiet", state("Den", [], at(20))["state"], "unknown")
check("a daytime span works too (13:00-15:00)", rs.in_quiet(at(14), rs.parse_quiet("13:00-15:00")), True)
check("parse: bad, equal, out of range → None",
      [rs.parse_quiet(x) for x in ("", "7-9", "07:00-07:00", "24:00-01:00", "10:60-11:00")], [None] * 5)
os.remove(os.environ["ROOMS_FILE"])
check("no rooms.yaml: the defaults", rs.overrides(), {})

print("the hand-off's hold (409) and send anyway; the route's roles")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios", room=None):
    p = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": p["code"], "device_name": device, "platform": platform}).json()
    h = {"X-Agent-Token": cl["token"]}
    if room:
        c.patch("/v1/devices/self", json={"room": room}, headers=h)
    return h


robin = pair("Robin", "adult", "Robin's iPhone")
ava = pair("Ava", "kid", "Ava's iPad")
tv = pair("Living room TV", "household", "Living room TV", platform="tvos", room="Kids room")

clock = {"local": at(23)}
os.environ.pop("ROOM_HOLD", None)
real_states = rs.states
rs.states = lambda rooms, **kw: real_states(rooms, now_local=clock["local"], http=ha([]), areas=areas, **kw)
rs.reset_cache()
check("ROOM_HOLD unset: off", rs.hold_enabled(), False)
off = c.post("/v1/handoff", json={"to_room": "kids room", "text": "Goodnight!"}, headers=robin)
check("hold off (the default): a hand-off to a room asleep goes through as before, even in quiet hours", off.status_code, 201)
os.environ["ROOM_HOLD"] = "0"
check("ROOM_HOLD=0 is off too", c.post("/v1/handoff", json={"to_room": "kids room", "text": "x"}, headers=robin).status_code, 201)
os.environ["ROOM_HOLD"] = "1"
r = c.post("/v1/handoff", json={"to_room": "kids room", "text": "Goodnight!"}, headers=robin)
check("a hand-off to a room asleep: 409 with the rule that held it",
      (r.status_code, r.json().get("state"), r.json().get("why")), (409, "asleep", "quiet hours in Kids room until 7:00"))
r2 = c.post("/v1/handoff", json={"to_room": "kids room", "text": "Goodnight!", "anyway": True}, headers=robin)
check("send anyway delivers it", r2.status_code, 201)
clock["local"] = at(15)
check("by day (unknown, no sensor) it goes straight through",
      c.post("/v1/handoff", json={"to_room": "kids room", "text": "Snack time"}, headers=robin).status_code, 201)
got = c.get("/v1/rooms/state", headers=robin)
check("GET /v1/rooms/state for an adult: the rooms with a screen",
      (got.status_code, [(x["room"], x["state"]) for x in got.json()["rooms"]]), (200, [("Kids room", "unknown")]))
check("…for the household screen too", c.get("/v1/rooms/state", headers=tv).status_code, 200)
check("…a kid gets 403", c.get("/v1/rooms/state", headers=ava).status_code, 403)
os.environ.pop("ROOM_HOLD", None)
check("…and GET /v1/rooms/state answers with the hold off", c.get("/v1/rooms/state", headers=robin).status_code, 200)
rs.states = real_states
check("still no Home Assistant write", writes, [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
