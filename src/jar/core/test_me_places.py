#!/usr/bin/env python3
# gate: hermetic
"""Your places (#1133): GET /v1/me/places is the caller's own arrivals only (named places, never scheduled regions,
never coordinates), newest first, with when; mode is their phone's location permission as it last reported it (null
until then); a kid sees their own (never a parent's), a parent never sees a kid's here (the route is self-only), a
shared screen gets 403. Through the real routes and stores. Fictional people (Robin and Alex, adults; Ava, Robin's kid;
a living-room screen); temporary stores.
Run: python3 scripts/test_me_places.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-meplaces-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "HOUSEHOLD_PREFS": "household.json",
             "PLACES_DB": "places.db", "PROACTIVE_DB": "proactive.db", "PUSH_DB": "push.db", "KB_ACL_DB": "kb_acl.db",
             "AGENT_TURN_LOG": "turns.jsonl", "SPACES_DB": "spaces.db", "SITTER_DB": "sitter.db",
             "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import places  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
c.patch(f"/v1/people/{ava}", json={"parents": [robin]}, headers=M)

places.record("e1", robin, "arrived", "Harbor Gym", "2026-10-05T08:00:00-07:00")
places.record("e2", robin, "left", "Harbor Gym", "2026-10-05T09:00:00-07:00")
places.record("e3", robin, "arrived", "home", "2026-10-05T09:30:00-07:00")
places.record("e4", robin, "arrived", "Harbor Gym", "2026-10-06T07:45:00-07:00")      # the gym again, later
places.record("e5", robin, "arrived", "Pickup lane", "2026-10-06T15:00:00-07:00", region="rg_sa-1")   # a region
places.record("e6", alex, "arrived", "Lakeview Office", "2026-10-06T08:00:00-07:00")
places.record("e7", ava, "arrived", "River School", "2026-10-06T08:10:00-07:00")


def mine(h):
    r = c.get("/v1/me/places", headers=h)
    return r.status_code, (r.json() if r.status_code == 200 else None)


print("── each person's own places only ──")
s, b = mine(ROBIN)
check("Robin: his own places, newest first, one row per place at its latest arrival; no region, no one else's",
      (s, [x["place"] for x in b["places"]], b["count"]), (200, ["Harbor Gym", "home"], 2))
check("…the latest arrival and a spoken when", (b["places"][0]["last_arrived"], bool(b["places"][0]["when"])),
      ("2026-10-06T14:45:00Z", True))
check("never coordinates (only place, last_arrived, when)", sorted(b["places"][0]), ["last_arrived", "place", "when"])
check("Alex sees only his own", [x["place"] for x in mine(ALEX)[1]["places"]], ["Lakeview Office"])
check("Ava (a kid) sees her own, never her parent's", [x["place"] for x in mine(AVA)[1]["places"]], ["River School"])
check("Robin, her parent, doesn't see Ava's here (this route is self-only)",
      "River School" in json.dumps(mine(ROBIN)[1]), False)
check("a shared screen: 403", mine(TV)[0], 403)

print("── mode: the phone's own report ──")
check("null until a phone reports it", mine(ROBIN)[1]["mode"], None)
r = c.post("/v1/me/places/mode", json={"value": "Always", "precise": True}, headers=ROBIN)
check("Robin's iPhone reports Always, precise", (r.status_code, r.json()["mode"]["value"], r.json()["mode"]["precise"],
                                                 r.json()["mode"]["device"]), (200, "always", True, "Robin's iPhone"))
check("…and it shows in his places", mine(ROBIN)[1]["mode"]["value"], "always")
check("Alex's mode stays null (one phone's report is its own person's)", mine(ALEX)[1]["mode"], None)
c.post("/v1/me/places/mode", json={"value": "while using", "precise": False}, headers=ROBIN)
check("a newer report replaces it", (mine(ROBIN)[1]["mode"]["value"], mine(ROBIN)[1]["mode"]["precise"]),
      ("while_using", False))
for label, h, body, status in (("a bad value: 400", ROBIN, {"value": "sometimes"}, 400),
                               ("precise must be a boolean: 400", ROBIN, {"value": "off", "precise": "yes"}, 400),
                               ("a shared screen can't report: 403", TV, {"value": "off"}, 403),
                               ("no device (the master token): 403", M, {"value": "off"}, 403)):
    check(label, c.post("/v1/me/places/mode", json=body, headers=h).status_code, status)

print("── forgetting ──")
dev = next(d["id"] for p in hh.get_store().people() if p["id"] == robin for d in p["devices"])
places.forget_device(robin, dev)
check("removing his phone forgets its mode", mine(ROBIN)[1]["mode"], None)
check("routes: adults and kids; never a shared screen",
      [hh.route_allowed(r, "GET", "/v1/me/places") for r in ("admin", "adult", "kid", "household")],
      [True, True, True, False])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
