#!/usr/bin/env python3
"""Place-triggered routines and the iPhone's arrivals (places.py, routines.on_place_event,
POST /v1/presence/place). Hermetic. Run: python3 scripts/test_place_routines.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-places-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))
import places  # noqa: E402
import proactive  # noqa: E402
import routines  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


print("── the store ──")
check("an event is stored once", (places.record("u1", "zach", "arrived", "home", "2026-10-01T17:00-07:00"),
                                  places.record("u1", "zach", "arrived", "home", None)), (True, False))
try:
    places.record("u2", "zach", "teleported", "home", None); check("bad event raises", False, True)
except ValueError:
    check("bad event raises", True, True)
for bad in ("", "x" * 41):
    try:
        places.clean_place(bad); check(f"place {len(bad)} chars refused", False, True)
    except ValueError:
        check(f"place {len(bad)} chars refused", True, True)
check("'Home' is home", places.clean_place("Home"), "home")
check("a person sees only their own", ([e["place"] for e in places.recent("zach")], places.recent("p_mir")), (["home"], []))

print("\n── routines fire on their place ──")
cards = []
take = lambda r, to: cards.append((r["name"], to))  # noqa: E731
mine = routines.save({"name": "Evening wind-down", "steps": ["Dim the lights"], "audience": "private",
                      "trigger": {"place": "Home", "on": "arrive"}}, person="zach", role="admin")
porch = routines.save({"name": "Porch light", "steps": ["Turn on the porch light"],
                       "trigger": {"place": "home", "on": "arrived"}}, person="zach", role="admin")
work = routines.save({"name": "Leaving work", "steps": ["Text Robin"], "audience": "private",
                      "trigger": {"place": "Work", "on": "leave"}}, person="zach", role="admin")
rob = routines.save({"name": "Robin's arrival", "steps": ["Start the kettle"], "audience": "private",
                     "trigger": {"place": "home", "on": "arrive"}}, person="p_mir", role="adult")
check("triggers saved normalised", (mine["trigger"], porch["trigger"]["on"]), ({"type": "place", "place": "home", "on": "arrive"}, "arrive"))
n = routines.on_place_event("zach", "admin", "arrived", "home", card=take, now=1000.0)
check("Zach arrives home: his own and the household one", (n, sorted(cards)),
      (2, sorted([("Evening wind-down", ["zach"]), ("Porch light", "household")])))
check("…the household card names nobody", [c for c in cards if c[0] == "Porch light"][0][1], "household")
check("Robin's private routine didn't fire for Zach", any(c[0] == "Robin's arrival" for c in cards), False)
cards.clear()
check("within 10 minutes, no repeat", routines.on_place_event("zach", "admin", "arrived", "home", card=take, now=1300.0), 0)
check("after 10 minutes, again", routines.on_place_event("zach", "admin", "arrived", "home", card=take, now=1700.0), 2)
cards.clear()
check("leaving work fires only the leave trigger", (routines.on_place_event("zach", "admin", "left", "Work", card=take, now=5000.0), cards),
      (1, [("Leaving work", ["zach"])]))
cards.clear()
routines.on_place_event("p_mir", "adult", "arrived", "home", card=take, now=9000.0)
check("Robin's arrival: hers and the household's, never Zach's private one",
      sorted(cards), sorted([("Robin's arrival", ["p_mir"]), ("Porch light", "household")]))
try:
    routines.save({"name": "x", "steps": ["y"], "trigger": {"place": "home", "on": "sideways"}}, person="zach", role="admin")
    check("bad trigger refused", False, True)
except ValueError:
    check("bad trigger refused", True, True)

print("\n── the endpoint ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}
s = c.post("/v1/pair/start", json={"new_person": {"name": "Robin", "role": "adult"}}, headers=M).json()
claim = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": "Robin's iPhone", "platform": "ios"}).json()
MIR = {"X-Agent-Token": claim["token"]}
mid = s["person"]["id"]
routines.save({"name": "Kettle", "steps": ["Start the kettle"], "audience": "private",
               "trigger": {"place": "home", "on": "arrive"}}, person=mid, role="adult")
r = c.post("/v1/presence/place", json={"id": "e-1", "event": "arrived", "place": "home", "at": "2026-10-01T18:00-07:00"}, headers=MIR)
check("204 on an arrival", r.status_code, 204)
got = [x for x in proactive.list_cards(person=mid, role="adult") if x["title"] == "Time for Kettle"]
check("her routine's card reached her", len(got), 1)
check("Zach's admin view doesn't get Robin's personal card", any(x["title"] == "Time for Kettle"
      for x in proactive.list_cards(person="zach", role="admin")), False)
check("the retry with the same id is harmless", c.post("/v1/presence/place", json={"id": "e-1", "event": "arrived", "place": "home"}, headers=MIR).status_code, 204)
check("bad event → 400", c.post("/v1/presence/place", json={"id": "e-2", "event": "zoom", "place": "home"}, headers=MIR).status_code, 400)
check("her events are hers only", ([e["place"] for e in places.recent(mid)], [e["id"] for e in places.recent("zach") if e["id"] == "e-1"]), (["home"], []))
dev = claim["device"]["id"]
check("removing her device forgets her places", (c.delete(f"/v1/devices/{dev}", headers=M).status_code, places.recent(mid)), (200, []))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
