#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A person's own room hint (titan-node#433): their own iPhone or Watch says which room they're in, for their OWN
requests only. Adults and the admin only; a shared device, the master token, a Mac, a TV or a kid's device can't send
one. Kept in memory for 10 minutes, never stored or logged, no GET; another person never sees or uses it; no shared
surface imports it. Fake Home Assistant, fictional household.
Run: python3 scripts/test_room_hint.py
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-roomhint-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "PUSH_DB": "push.db", "CONTINUITY_DB": "continuity.db", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import home_assistant as ha  # noqa: E402
import room_hint as RH  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ROOMS = ["Kitchen", "Living Room", "Den"]
NOW = time.time()

print("── the store ──")
check("kept, and read back by its own person", (RH.put("p_a", "d1", "kitchen", 0.9, NOW, ROOMS, now=NOW),
                                               RH.get("p_a", now=NOW + 5)["room"]), (True, "Kitchen"))
check("another person has none", RH.get("p_b", now=NOW + 5), None)
check("…and no person at all has none", RH.get(None), None)
check("it lapses after ten minutes", (RH.get("p_a", now=NOW + 599) is not None, RH.get("p_a", now=NOW + 600)), (True, None))
RH.put("p_a", "d1", "Den", 0.9, NOW, ROOMS, now=NOW)
check("a weak hint isn't kept, and doesn't replace a good one",
      (RH.put("p_a", "d2", "Kitchen", 0.4, NOW + 1, ROOMS, now=NOW + 1), RH.get("p_a", now=NOW + 2)["room"]), (False, "Den"))
RH.put("p_a", "watch", "Kitchen", 0.8, NOW + 10, ROOMS, now=NOW + 10)
RH.put("p_a", "phone", "Den", 0.9, NOW + 5, ROOMS, now=NOW + 11)
check("the newest wins between phone and Watch (a late older one doesn't)", RH.get("p_a", now=NOW + 12)["room"], "Kitchen")
for label, args, status in (("not one of the household's rooms", ("Garage", 0.9, NOW), 400),
                            ("a stale ts", ("Den", 0.9, NOW - 200), 400), ("a ts from the future", ("Den", 0.9, NOW + 60), 400),
                            ("confidence above 1", ("Den", 1.5, NOW), 400), ("not a number", ("Den", "high", NOW), 400)):
    try:
        RH.put("p_a", "d1", *args, ROOMS, now=NOW)
        check(f"{label}: refused", "kept", status)
    except RH.HintError as exc:
        check(f"{label}: refused", exc.status, status)
try:
    RH.put("p_a", "d1", "Garage", 0.9, NOW, ROOMS, now=NOW)
except RH.HintError as exc:
    check("…an unknown room is answered with the household's rooms, and the message never echoes it",
          (exc.rooms, "Garage" in str(exc)), (sorted(ROOMS), False))
try:
    RH.put("p_a", "d1", "Den", 0.9, NOW, [], now=NOW)
except RH.HintError as exc:
    check("no rooms known (Home Assistant down): 503", exc.status, 503)
RH.clear("p_a")
check("clear: gone now", RH.get("p_a", now=NOW), None)
check("who may send one",
      [RH.refusal(r, d, v) is None for r, d, v in (
          ("adult", {"platform": "ios"}, "device"), ("admin", {"platform": "watchos"}, "device"),
          ("kid", {"platform": "ios"}, "device"), ("kid", {"platform": "watchos"}, "watch"),
          ("household", {"platform": "tvos"}, "device"), ("adult", {"platform": "mac"}, "device"),
          ("adult", {"platform": "tvos"}, "device"), ("admin", None, "master"))],
      [True, True, False, False, False, False, False, False])

print("\n── through the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios", person_id=None):
    body = {"person_id": person_id} if person_id else {"new_person": {"name": name, "role": role}}
    s = c.post("/v1/pair/start", json=body, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
ROBIN_WATCH, _ = pair(None, None, "Robin's Watch", platform="watchos", person_id=robin)
ROBIN_MAC, _ = pair(None, None, "Robin's Mac", platform="mac", person_id=robin)
JORDAN, jordan = pair("Jordan Hale", "adult", "Jordan's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
AREAS = {"light.kitchen_ceiling": "Kitchen", "light.kitchen_counter": "Kitchen", "light.den_lamp": "Den",
         "media_player.den_tv": "Den", "media_player.living_room_tv": "Living Room", "fan.den_fan": "Den"}
real_areas = ha._areas
ha._areas = lambda http: dict(AREAS)


def post(h, room="Den", conf=0.9, ts=None):
    return c.post("/v1/presence/room", json={"room": room, "confidence": conf, "ts": ts or time.time()}, headers=h)


check("an adult's own iPhone: 204", post(ROBIN).status_code, 204)
check("…their own Watch: 204", post(ROBIN_WATCH, "Kitchen").status_code, 204)
check("…kept for them", RH.get(robin)["room"], "Kitchen")
check("never read back over HTTP: no GET for anyone, the admin included",
      (c.get("/v1/presence/room", headers=ROBIN).status_code in (403, 404, 405),
       c.get("/v1/presence/room", headers=M).status_code in (404, 405)), (True, True))
for who, h in (("their Mac", ROBIN_MAC), ("a kid's iPhone", AVA), ("a shared TV", TV), ("the master token", M)):
    check(f"{who}: 403", post(h).status_code, 403)
r = post(ROBIN, "Garage")
check("an unknown room: 400 with the household's rooms", (r.status_code, r.json().get("rooms")),
      (400, ["Den", "Kitchen", "Living Room"]))
check("a stale ts: 400", post(ROBIN, ts=time.time() - 300).status_code, 400)
check("Jordan's hint is Jordan's alone", (post(JORDAN, "Living Room").status_code, RH.get(jordan)["room"],
                                          RH.get(robin)["room"]), (204, "Living Room", "Kitchen"))
check("DELETE clears it", (c.delete("/v1/presence/room", headers=ROBIN).status_code, RH.get(robin)), (204, None))
check("…and a shared device can't clear anyone's", c.delete("/v1/presence/room", headers=TV).status_code, 403)

print("\n── in a turn: \"here\" is the asker's own room, never anyone else's, never a guess ──")
import agent_runtime_lib as lib  # noqa: E402

STATES = [{"entity_id": e, "state": "on", "attributes": {"friendly_name": e.split(".")[1].replace("_", " ").title()}}
          for e in AREAS]
SENT: list = []


def fake_http(method, path, body=None):
    if method == "GET" and path == "/api/states":
        return STATES
    SENT.append((method, path, body))
    return []


ha._http = fake_http
ha._token = lambda: "fake"


def turn(person, role):
    lib._TURN_IO.person, lib._TURN_IO.role = person, role


post(ROBIN, "Den")
turn({"id": robin, "name": "Robin"}, "adult")
out = lib._execute_tool("home_control", {"device": "lights here", "action": "off"})
check("Robin: \"lights here\" is the Den's lamp", (SENT[-1][1], SENT[-1][2].get("entity_id")) if SENT else out,
      ("/api/services/light/turn_off", "light.den_lamp"))
SENT.clear()
out = lib._execute_tool("home_control", {"device": "the TV in here", "action": "pause"})
check("…\"the TV in here\" is the Den's TV", SENT[-1][2].get("entity_id") if SENT else out, "media_player.den_tv")
turn({"id": jordan, "name": "Jordan"}, "adult")
RH.clear(jordan)
SENT.clear()
out = lib._execute_tool("home_control", {"device": "lights here", "action": "off"})
check("Jordan (no hint of their own) is asked, never given Robin's room", (out, SENT), (ha.NO_ROOM, []))
turn(None, "household")
check("a shared screen's turn has no room", lib._my_room(), None)
turn({"id": robin, "name": "Robin"}, "kid")
check("a kid's turn never reads a hint", lib._my_room(), None)
turn({"id": robin, "name": "Robin"}, "adult")
check("Robin asking from the Mac uses the phone's hint (same person)", lib._my_room()["room"], "Den")
SENT.clear()
out = lib._execute_tool("home_control", {"device": "Kitchen Ceiling", "action": "off"})
check("a device named outright doesn't use the hint", SENT[-1][2].get("entity_id") if SENT else out, "light.kitchen_ceiling")

print("\n── never kept, never shown elsewhere ──")
lib._TURN_INFO.info = {}
said = []
lib._TURN_INFO.info = {"_where_said": said}
lib._device_bridge_enabled = lambda: True
lib._device_tool_allowed = lambda n: True
lib._turn_client = lambda: "iphone"
lib._device_request = lambda name, args: "You're near 100 Elm St, Springfield (within 20 m)."
out = lib._execute_tool("where_am_i", {})
check("where am I: the address and the asker's room", "Room: the Den" in out, True)
check("…and the kept conversation leaves the room out, like the address",
      lib.without_location("You're in the Den, at 100 Elm St.", lib._TURN_INFO.info["_where_said"]).count("Den"), 0)
check("where_am_i stays a sensitive tool (no facts extracted, nothing saved)", "where_am_i" in lib.SENSITIVE_TOOLS, True)
log = Path(os.environ["AGENT_TURN_LOG"])
check("no turn log line holds a room", any(r in (log.read_text() if log.exists() else "") for r in ROOMS), False)
here = Path(__file__).parent
for mod in ("room_state.py", "handoff.py", "home_feed.py", "home_security.py"):
    p = here / mod
    if not p.exists():
        continue
    names = {a.name.split(".")[0] for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in (n.names if isinstance(n, ast.Import) else [type("x", (), {"name": n.module or ""})])}
    check(f"{mod} never imports room_hint", "room_hint" in names, False)
src = (here / "room_hint.py").read_text()
check("room_hint never writes a file or prints", ("open(" in src, "print(" in src, "sqlite3" in src), (False, False, False))
api_src = (here / "agent_api_server.py").read_text()
start = api_src.index('@app.post("/v1/presence/room")')
block = api_src[start:api_src.index("@app.post(", start + 10)]
check("the routes log nothing", ("print(" in block, "_log" in block), (False, False))
check("only the turn path reads a hint", sorted(p.name for p in here.glob("*.py")
      if "room_hint.get(" in p.read_text() and not p.name.startswith("test_")), ["agent_runtime_lib.py"])
ha._areas = real_areas

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
