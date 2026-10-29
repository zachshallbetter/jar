#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""A town or an address as a place trigger (titan-node#616): scheduled.resolve_place's regions (radius
tiers, an address without coordinates, a tie asks, the budget of 8), GET /v1/presence/regions (own only,
403 for a household device, fired rows gone), and POST /v1/presence/place with `region` (fires that row
once, owner-checked, never a known place). The geocoder is a stub: no Open-Meteo calls.
Run: python3 scripts/test_place_regions.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-regions-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "SCHEDULED_DB": "scheduled.db", "PUSH_DB": "push.db", "KB_ACL_DB": "kb_acl.db",
             "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha-token")
os.environ.pop("TITAN_DISCOVERY", None)
os.environ.pop("GEMINI_API_KEY", None)
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import places  # noqa: E402
import scheduled as sc  # noqa: E402
import weather  # noqa: E402
import household_identity as _hh  # noqa: E402

ADMIN_ID = _hh.ADMIN_PERSON_ID   # the seeded admin's id (a system constant); every other fixture person is fictional

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")
    return good


# ── the geocoder, stubbed (Open-Meteo's shape after weather.geocode(every=True)) ──────────────────
def town(name, pop, score=0, lat=47.65, lon=-117.42):
    return {"name": name, "latitude": lat, "longitude": lon, "timezone": "auto", "score": score, "population": pop}


TOWNS = {
    "Spokane, WA": [town("Spokane, Washington", 228_989, 1), town("Spokane, Missouri", 180, 0)],
    "Spokane": [town("Spokane, Washington", 228_989), town("Spokane, Missouri", 180)],
    "Portland": [town("Portland, Oregon", 652_503), town("Portland, Maine", 68_408)],
    "Springfield, US": [town("Springfield, Illinois", 114_394, 1), town("Springfield, Missouri", 169_176, 1)],
    "Cheney, WA": [town("Cheney, Washington", 12_000, 1)],
    "Tiny, WA": [town("Tiny, Washington", None, 1)],
    "Medical Lake, WA": [town("Medical Lake, Washington", 5_000, 1)],
    "Seattle": [town("Seattle, Washington", 737_015)],
}
asked: list[str] = []


def fake_geocode(name, fetch=None, *, every=False):
    asked.append(name)
    return list(TOWNS.get(name, []))


weather.geocode = fake_geocode
# The real geocoder still answers every=True from Open-Meteo's shape (population, score), offline.
hits = [{"name": "Spokane", "admin1": "Washington", "country_code": "US", "latitude": 47.6, "longitude": -117.4,
         "population": 228989},
        {"name": "Spokane", "admin1": "Missouri", "country_code": "US", "latitude": 36.8, "longitude": -93.3}]
import importlib  # noqa: E402
_w = importlib.reload(weather)
got = _w.geocode("Spokane, WA", lambda u: {"results": hits}, every=True)
check("weather.geocode(every=True): population and score, best first",
      [(g["name"], g["population"], g["score"]) for g in got],
      [("Spokane, Washington", 228989, 1), ("Spokane, Missouri", None, 0)])
check("…and the one-answer form is unchanged", _w.geocode("Spokane, WA", lambda u: {"results": hits}),
      {"name": "Spokane, Washington", "latitude": 47.6, "longitude": -117.4, "timezone": "auto"})
weather.geocode = fake_geocode
_w.geocode = fake_geocode

print("── resolve_place ──")
places.record("ev-z1", ADMIN_ID, "arrived", "home", None)
places.record("ev-z2", ADMIN_ID, "arrived", "Work", None)
R = lambda raw: sc.resolve_place(ADMIN_ID, raw)   # noqa: E731
check("a known place: unchanged, no region, no geocoder", (R("work"), asked), (("Work", None), []))
check("home too", R("my house"), ("home", None))
name, reg = R("Spokane, WA")
check("a town: a region with its circle by population (under 500k → 10 km)", (name, reg),
      ("Spokane, Washington", {"kind": "town", "label": "Spokane, Washington", "lat": 47.65, "lon": -117.42,
                               "radius_m": 10_000}))
check("radius tiers: under 10k 3 km, under 100k 6 km, otherwise 15 km; unknown is small",
      [sc.radius_for(p) for p in (5_000, 12_000, 228_989, 652_503, None)], [3_000, 6_000, 10_000, 15_000, 3_000])
check("…as resolved", [R(t)[1]["radius_m"] for t in ("Medical Lake, WA", "Cheney, WA", "Tiny, WA")], [3_000, 6_000, 3_000])
check("one town far bigger than the next is the one meant", R("Spokane")[0], "Spokane, Washington")
check("an address: no coordinates, the words as said, 150 m", R("1200 W Main Ave, Spokane"),
      ("1200 W Main Ave, Spokane", {"kind": "address", "label": "1200 W Main Ave, Spokane",
                                    "query": "1200 W Main Ave, Spokane", "radius_m": 150}))
check("…a house number never goes to the geocoder", "1200 W Main Ave, Spokane" in asked, False)
long_addr = "12345 North Very Long Boulevard Name, Spokane Valley, WA 99216"
check("a long address: the label fits a place name, the query keeps it all",
      (len(R(long_addr)[0]) <= 40, R(long_addr)[1]["query"]), (True, long_addr))
check("no town by that name: an address the iPhone looks up", R("Riverfront Park Pavilion")[1]["kind"], "address")


def refused(raw):
    try:
        R(raw)
    except ValueError as exc:
        return str(exc)
    return None


check("two towns tie at the top score: ask, don't schedule", refused("Springfield, US"),
      "which one: Springfield, Missouri or Springfield, Illinois? Say it with the state or country")
check("two towns of a size with no state: ask", refused("Portland"),
      "which one: Portland, Oregon or Portland, Maine? Say it with the state or country")
check("a kind of place is never a town: the known-places answer", refused("school").startswith(
      "titan doesn't know a place called “school” for you. It knows: home, Work"), True)


def down(name, fetch=None, *, every=False):
    raise OSError("offline")


weather.geocode = down
check("the geocoder down: ask again later, never guess", refused("Cheney, WA"),
      "titan couldn't look up “Cheney, WA” just now; try again in a minute")
weather.geocode = fake_geocode
check("an eval turn: any name, no region, no geocoder", (sc.resolve_place(ADMIN_ID, "Seattle", check=False), asked.count("Seattle")),
      (("Seattle", None), 0))

print("── scheduling ──")
LAMP = {"device": "Porch Light", "action": "on"}
cards: list = []
card = lambda r, title, detail, **k: cards.append((r["id"], title))   # noqa: E731
row, reply = sc.schedule("home_control", LAMP, None, "Porch light", owner=ADMIN_ID, role="admin",
                         place="Spokane, WA", on="arrive", card=card)
check("the reply says which town and how wide", reply.startswith(
      "Scheduled “Porch light” when you get to Spokane, Washington (about 10 km around town)."), True)
check("the row: the label as place, the region on the trigger", (row["trigger"]["place"], row["trigger"]["region"]["kind"]),
      ("Spokane, Washington", "town"))
check("…stored as JSON in the region column", sc.get(row["id"])["trigger"]["region"]["radius_m"], 10_000)
_, reply = sc.schedule("home_control", LAMP, None, "Porch light", owner=ADMIN_ID, role="admin",
                       place="Portland", on="arrive", card=card)
check("a tie: nothing scheduled, the question in the reply", reply,
      "Not scheduled: which one: Portland, Oregon or Portland, Maine? Say it with the state or country.")
_, reply = sc.schedule("home_control", LAMP, None, "Porch light", owner=ADMIN_ID, role="admin",
                       place="1200 W Main Ave, Spokane", on="leave", card=card)
check("an address's reply", "when you leave 1200 W Main Ave, Spokane (within about 150 m of it; your iPhone finds "
      "the address)" in reply, True)
for i in range(6):
    sc.schedule("home_control", LAMP, None, f"Porch {i}", owner=ADMIN_ID, role="admin", place="Cheney, WA", on="arrive",
                card=card)
check("eight pending", len(sc.regions_for(ADMIN_ID)), 8)
_, reply = sc.schedule("home_control", LAMP, None, "Ninth", owner=ADMIN_ID, role="admin", place="Tiny, WA", on="arrive",
                       card=card)
check("the budget refuses the 9th", reply,
      "Not scheduled: you have 8 place reminders for towns or addresses waiting. Cancel one first.")
r2, _ = sc.schedule("home_control", LAMP, None, "Known still fine", owner=ADMIN_ID, role="admin", place="work", on="arrive",
                    card=card)
check("…a known place isn't counted against it", (r2 is not None, r2["trigger"]), (True, {"place": "Work", "on": "arrive"}))

print("── migration ──")
import sqlite3  # noqa: E402
old = _TMP / "old-scheduled.db"
con = sqlite3.connect(str(old))
con.execute(sc._SCHEMA.format(name="scheduled"))
con.execute("ALTER TABLE scheduled ADD COLUMN repeat TEXT")
con.execute("INSERT INTO scheduled (id, owner, audience, what, tool, args, state, created_at, updated_at, place, on_event, "
            "expires_at) VALUES ('sa-0ld00001',?,'private','Old place','count_chars','{}','scheduled',1,1,'home',"
            "'arrive',?)", (ADMIN_ID, time.time() + 600))
con.commit()
con.close()
check("a live table without the column: added in place, rows kept",
      (sc.get("sa-0ld00001", db=old)["trigger"], "region" in [r[1] for r in sqlite3.connect(str(old)).execute(
          "PRAGMA table_info(scheduled)")]), ({"place": "home", "on": "arrive"}, True))

print("── the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(person, device_name, platform="ios"):
    body = {"person_id": person} if isinstance(person, str) else {"new_person": person}
    code = c.post("/v1/pair/start", json=body, headers=M).json()
    r = c.post("/v1/pair/claim", json={"code": code["code"], "device_name": device_name, "platform": platform}).json()
    return {"X-Agent-Token": r["token"]}, r["person"]["id"]


ADMIN, _ = pair(ADMIN_ID, "The admin's iPhone")
ROBIN, rob = pair({"name": "Robin", "role": "adult"}, "Robin's iPhone")
AVA, ava = pair({"name": "Ava", "role": "kid", "parents": [ADMIN_ID, rob]}, "Ava's iPhone")
KITCHEN, _ = pair({"name": "Kitchen", "role": "household"}, "Kitchen iPad", "ipados")
places.record("ev-k1", ava, "left", "home", None)
kr, _ = sc.schedule("home_control", LAMP, None, "Ava's", owner=ava, role="kid", place="Cheney, WA", on="arrive", card=card)

got = c.get("/v1/presence/regions", headers=ADMIN).json()["regions"]
check("GET: the caller's own, eight", (len(got), {g["id"] for g in got} >= {f"rg_{row['id']}"}), (8, True))
first = next(g for g in got if g["id"] == f"rg_{row['id']}")
check("…a town's fields", {k: first[k] for k in ("label", "kind", "lat", "lon", "radius_m", "on")},
      {"label": "Spokane, Washington", "kind": "town", "lat": 47.65, "lon": -117.42, "radius_m": 10_000, "on": "arrive"})
addr = next(g for g in got if g["kind"] == "address")
check("…an address's: a query, no coordinates", ("query" in addr, "lat" in addr, addr["on"]), (True, False, "leave"))
check("…soonest-expiring first", [g["expires_at"] for g in got] == sorted(g["expires_at"] for g in got), True)
check("a parent doesn't get a kid's", f"rg_{kr['id']}" in {g["id"] for g in got}, False)
check("the kid gets their own", [g["id"] for g in c.get("/v1/presence/regions", headers=AVA).json()["regions"]],
      [f"rg_{kr['id']}"])
check("someone with none: an empty list", c.get("/v1/presence/regions", headers=ROBIN).json(), {"regions": []})
r = c.get("/v1/presence/regions", headers=KITCHEN)
check("a household device: 403", r.status_code, 403)

runs: list = []
_real = sc.on_place_event
sc.on_place_event = lambda *a, **k: _real(*a, **{**k, "execute": lambda r, role, name: (runs.append(r["id"]) or
                                                                                         (True, "Done."))})


# The place route's own action tasks (#1241). TestClient (no `with`) runs each request on a short-lived event loop and
# cancels a background task still pending when the response returns: on a busy titan the route's _place_actions was
# cut off before it ran, and the check read "scheduled" (no clock length fixes that). So the real action runs on this
# test's own long-lived loop, each with an Event set when it finishes, and every check waits on those, not a clock.
import asyncio  # noqa: E402
import threading  # noqa: E402

PLACE_RUNS: list[threading.Event] = []
_real_place_actions = api._place_actions
_BG = asyncio.new_event_loop()
threading.Thread(target=_BG.run_forever, daemon=True).start()


async def _place_actions(*a, **k):   # the route looks _place_actions up at call time
    done = threading.Event()
    PLACE_RUNS.append(done)
    asyncio.run_coroutine_threadsafe(_real_place_actions(*a, **k), _BG).add_done_callback(lambda _f: done.set())


api._place_actions = _place_actions
DEADMAN_S = 180.0   # only so a broken route can't hang the gate; never what decides a check


def settle() -> None:
    """Every place action the route has started so far has finished (a departure's 300 s jitter hold starts none
    until it ends, so it isn't waited for, by design)."""
    end = time.time() + DEADMAN_S
    for done in list(PLACE_RUNS):
        if not done.wait(max(0.0, end - time.time())):
            raise AssertionError("a place task never finished")


def wait_done(sid, started_before: int):
    """The row's state once the place action(s) the POST started have finished. Fails loudly (not with a wrong state)
    if no action started or one never finished within the deadman."""
    end = time.time() + DEADMAN_S
    while len(PLACE_RUNS) <= started_before:
        if time.time() >= end:
            raise AssertionError("the place route never started its action task")
        time.sleep(0.02)
    for done in PLACE_RUNS[started_before:]:
        if not done.wait(max(0.0, end - time.time())):
            raise AssertionError("the place task never finished")
    return sc.get(sid)["state"]


r = c.post("/v1/presence/place", headers=ROBIN, json={"id": "ev-m1", "event": "arrived", "place": "Spokane, Washington",
                                                    "region": f"rg_{row['id']}"})
settle()
check("another person's region id: nothing fires", (r.status_code, sc.get(row["id"])["state"], runs), (204, "scheduled", []))
r = c.post("/v1/presence/place", headers=ADMIN, json={"id": "ev-z3", "event": "left", "place": "Spokane, Washington",
                                                     "region": f"rg_{row['id']}"})
settle()
check("the wrong event (left, for an arrive): nothing", (r.status_code, sc.get(row["id"])["state"]), (204, "scheduled"))
before = len(PLACE_RUNS)
r = c.post("/v1/presence/place", headers=ADMIN, json={"id": "ev-z4", "event": "arrived", "place": "Spokane, Washington",
                                                     "region": f"rg_{row['id']}"})
check("the admin's arrival with its id fires that row", (r.status_code, wait_done(row["id"], before), runs.count(row["id"])),
      (204, "done", 1))
r = c.post("/v1/presence/place", headers=ADMIN, json={"id": "ev-z5", "event": "arrived", "place": "Spokane, Washington",
                                                     "region": f"rg_{row['id']}"})
settle()
check("…once: a second arrival finds nothing", runs.count(row["id"]), 1)
check("…and it's gone from the phone's list", f"rg_{row['id']}" in {g["id"] for g in c.get(
      "/v1/presence/regions", headers=ADMIN).json()["regions"]}, False)
check("the arrival is in place_events (shown like any other)", places.where_now(ADMIN_ID)["place"], "Spokane, Washington")
check("…but a town never becomes a known place", "Spokane, Washington" in places.known(ADMIN_ID), False)
check("a malformed region id: 400", c.post("/v1/presence/place", headers=ADMIN, json={
    "id": "ev-z6", "event": "arrived", "place": "x", "region": "sa-1; DROP"}).status_code, 400)
sc.on_place_event = _real

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
