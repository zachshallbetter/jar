#!/usr/bin/env python3
# gate: hermetic
"""The house's locks, doors and cameras (titan-node#413): read-only status for admin, adult and household screens;
camera stills from memory, home network only, at most every 30 s; Lock up, lock only, confirmed on the asker's own
iPhone. Never a kid's device; nothing unlocks, by any path; both switches off unless exactly "1". A fake Home Assistant
fails the run on any service but lock.lock, and on lock.lock outside a confirmed tap. Throwaway stores, made-up people.
Run: python3 scripts/test_home_security.py"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-homesec-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "UPLOADS_DB": "uploads.db", "UPLOADS_DIR": "uploads",
             "TV_UNLOCK_DB": "tv_unlock.db", "LOCK_UP_DB": "lock_up.db", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL",
          "HA_LOCK_UP", "HA_CAMERA_SNAPSHOTS"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import titan_alert  # noqa: E402
titan_alert.alert = lambda *a, **k: None   # never a real alert from a test

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import home_assistant as ha  # noqa: E402
import home_security as hs  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402

# ── a fake Home Assistant ──
STATES = [
    {"entity_id": "lock.front_door", "state": "unlocked", "attributes": {"friendly_name": "Front door"}},
    {"entity_id": "lock.back_door", "state": "locked", "attributes": {"friendly_name": "Back door"}},
    {"entity_id": "binary_sensor.patio_door", "state": "off", "attributes": {"friendly_name": "Patio door", "device_class": "door"}},
    {"entity_id": "binary_sensor.motion_hall", "state": "on", "attributes": {"friendly_name": "Hall motion", "device_class": "motion"}},
    {"entity_id": "cover.garage_door", "state": "closed", "attributes": {"friendly_name": "Garage door", "device_class": "garage"}},
    {"entity_id": "cover.blinds", "state": "open", "attributes": {"friendly_name": "Blinds", "device_class": "blind"}},
    {"entity_id": "alarm_control_panel.house", "state": "disarmed", "attributes": {"friendly_name": "House alarm"}},
    {"entity_id": "camera.porch", "state": "idle", "attributes": {"friendly_name": "Porch"}},
    {"entity_id": "camera.driveway", "state": "idle", "attributes": {"friendly_name": "Driveway"}},
    {"entity_id": "camera.shed", "state": "idle", "attributes": {"friendly_name": "Shed"}},
    {"entity_id": "camera.back_yard", "state": "idle", "attributes": {"friendly_name": "Back yard"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
]
STATE = {s["entity_id"]: s for s in json.loads(json.dumps(STATES))}
CALLS: list[tuple[str, str, object]] = []
VIOLATIONS: list[str] = []
JAM: set[str] = set()
# The porch is read on the LAN through ONVIF and the back yard through Generic Camera on an RTSP sub stream (how the Lorex
# recorder will be added); the driveway goes through a maker's cloud (Blink); the shed's integration is none titan
# knows, so it's not local.
INTEGRATIONS = {"onvif": ["camera.porch"], "generic": ["camera.back_yard"], "blink": ["camera.driveway"],
                "someone_elses": ["camera.shed"]}


def _confirmed_tap_running() -> bool:
    """lock.lock is allowed only while a request is in 'locking', which only a confirmed own-iPhone tap sets."""
    import sqlite3
    try:
        con = sqlite3.connect(os.environ["LOCK_UP_DB"])
        n = con.execute("SELECT COUNT(*) FROM requests WHERE state='locking'").fetchone()[0]
        con.close()
        return n > 0
    except sqlite3.Error:
        return False


def fake_ha(method, path, body):
    CALLS.append((method, path, body))
    if method == "GET" and path == "/api/states":
        return list(json.loads(json.dumps(list(STATE.values()))))
    if method == "POST" and path.startswith("/api/services/"):
        if path != "/api/services/lock/lock":
            VIOLATIONS.append(f"service {path}")
            raise AssertionError(f"Home Assistant was asked for {path}")
        if not _confirmed_tap_running():
            VIOLATIONS.append("lock.lock outside a confirmed tap")
            raise AssertionError("lock.lock outside a confirmed tap")
        eid = body["entity_id"]
        STATE[eid]["state"] = "jammed" if eid in JAM else "locked"
        return []
    if method == "POST" and path == "/api/template":
        tpl = body["template"]
        if "integration_entities" in tpl:
            asked = re.findall(r"'([a-z_]+)': integration_entities", tpl)
            return json.dumps({n: [e for e in INTEGRATIONS.get(n, []) if e in STATE] for n in asked})
        return "{}"
    VIOLATIONS.append(f"{method} {path}")
    raise AssertionError(f"unexpected {method} {path}")


ha._http = fake_ha
hs._proxy = lambda eid: (_ for _ in ()).throw(AssertionError("no real camera proxy in a test"))
JPEG = b"\xff\xd8\xff\xe0" + b"ZebraQuixoticLantern-still" * 40 + b"\xff\xd9"

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app, client=("10.0.0.21", 50000))
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios", person_id=None):
    body = {"person_id": person_id} if person_id else {"new_person": {"name": name, "role": role}}
    s = c.post("/v1/pair/start", json=body, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ALEX_MAC, _ = pair(None, None, "Alex's Mac", platform="mac", person_id=alex)
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")

ROUTES = [("GET", "/v1/home/security"), ("GET", "/v1/home/cameras"), ("GET", "/v1/home/cameras/camera.porch/snapshot"),
          ("POST", "/v1/home/lock-up"), ("GET", "/v1/home/lock-up/0123456789abcdef")]


def call(method, path, h, **kw):
    return c.request(method, path, headers=h, **kw)


def tap(card_id, action, h):
    return c.post(f"/v1/proactive/{card_id}/action", json={"action_id": action}, headers=h).json()


def lock_card():
    return next((x for x in proactive.list_cards(include_acked=True) if x.get("source") == "lock-up"
                 and x.get("acked_at") is None), None)


def reset_locks():
    for eid, s in STATE.items():
        s["state"] = next(x["state"] for x in STATES if x["entity_id"] == eid)
    JAM.clear()


print("── both switches are off unless exactly \"1\" ──")
for v in (None, "0", "true", "yes", " 1"):
    if v is None:
        os.environ.pop("HA_LOCK_UP", None); os.environ.pop("HA_CAMERA_SNAPSHOTS", None)
    else:
        os.environ["HA_LOCK_UP"] = v; os.environ["HA_CAMERA_SNAPSHOTS"] = v
    check(f"HA_LOCK_UP={v!r}: off", (hs.lock_up_on(), hs.snapshots_on()), (False, False))
os.environ.pop("HA_LOCK_UP", None); os.environ.pop("HA_CAMERA_SNAPSHOTS", None)
r = call("POST", "/v1/home/lock-up", ALEX)
check("unset: the route says Locking from Jellybean is turned off", (r.status_code, r.json().get("said")), (403, hs.OFF))
check("unset: the chat tool says the same", hs.tool({}, alex, "adult"), hs.OFF)
r = call("GET", "/v1/home/cameras", ALEX)
check("unset: cameras are off", (r.status_code, r.json().get("error")), (403, "off"))
r = call("GET", "/v1/home/cameras/camera.porch/snapshot", ALEX)
check("unset: no still", r.status_code, 403)
s = call("GET", "/v1/home/security", ALEX).json()
check("status works with both off, and says they're off", (s["lock_up"], s["snapshots"]), (False, False))

print("\n── read-only status ──")
check("locks, doors and the garage; never the alarm, motion, blinds, cameras or lights",
      ({k: [x["id"] for x in s[k]] for k in ("locks", "doors", "garage")}),
      {"locks": ["lock.front_door", "lock.back_door"], "doors": ["binary_sensor.patio_door"], "garage": ["cover.garage_door"]})
check("a door sensor reads open or closed", s["doors"][0]["state"], "closed")
check("the summary says what's unlocked", s["summary"], "Front door is unlocked · Garage closed")
check("…and reading it called no service", [p for m, p, _ in CALLS if m == "POST" and "/services/" in p], [])
for who, h, code in (("an adult", ALEX, 200), ("the household TV", TV, 200), ("a kid's iPhone", AVA, 403)):
    check(f"status for {who}: {code}", call("GET", "/v1/home/security", h).status_code, code)

print("\n── a kid's device gets none of it ──")
os.environ["HA_LOCK_UP"] = "1"; os.environ["HA_CAMERA_SNAPSHOTS"] = "1"
for m, p in ROUTES:
    check(f"kid: {m} {p} → 403", call(m, p, AVA).status_code, 403)
check("lock_up: adults and the admin only (the tool table)", [r for r in ("admin", "adult", "kid", "household")
      if hh.role_allows_tool(r, "lock_up")], ["admin", "adult"])

print("\n── camera stills: memory only, home network only, every 30 s ──")
before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in _TMP.rglob("*") if p.is_file()}
code, body, headers = hs.snapshot("camera.porch", "dev-tv", "10.0.0.31", fetch=lambda e: JPEG, now=1000.0)
check("a TV on the home Wi-Fi gets the still, no-store", (code, body == JPEG, headers.get("Cache-Control")), (200, True, "no-store"))
check("again inside 30 s: 429 with Retry-After",
      hs.snapshot("camera.porch", "dev-tv", "10.0.0.31", fetch=lambda e: JPEG, now=1010.0)[0::2], (429, {"Retry-After": "21"}))
check("after 30 s: the next one", hs.snapshot("camera.porch", "dev-tv", "10.0.0.31", fetch=lambda e: JPEG, now=1031.0)[0], 200)
for host in ("100.101.2.3", "fd7a:115c:a1e0::1", "8.8.8.8", "testclient", None):
    check(f"a client at {host}: 403, never sent off the home network",
          hs.snapshot("camera.porch", f"dev-{host}", host, fetch=lambda e: JPEG, now=2000.0)[0], 403)
for host in ("192.168.1.9", "172.20.0.4", "127.0.0.1", "fe80::1", "fd12:3456::1"):
    check(f"a client at {host}: on the home network", hs.on_home_network(host), True)
check("not a camera: 404", hs.snapshot("lock.front_door", "dev-x", "10.0.0.31", fetch=lambda e: JPEG, now=3000.0)[0], 404)
leaked = [str(p) for p in _TMP.rglob("*") if p.is_file() and b"ZebraQuixoticLantern" in p.read_bytes()]
check("the still's bytes are in no file under the state dir", leaked, [])
changed = sorted(str(p.relative_to(_TMP)) for p in _TMP.rglob("*") if p.is_file()
                 and before.get(p) != (p.stat().st_size, p.stat().st_mtime_ns))
check("…the only file that changed is the counts database", all(x.startswith("lock_up.db") for x in changed), True)
import sqlite3  # noqa: E402
con = sqlite3.connect(os.environ["LOCK_UP_DB"])
check("counted: two stills of the porch, numbers only", con.execute("SELECT camera, count FROM snapshot_counts").fetchall(),
      [("camera.porch", 2)])
con.close()
api_still = TestClient(api.app, client=("100.101.2.3", 50000))
check("through the API from a Tailscale address: 403", api_still.get("/v1/home/cameras/camera.porch/snapshot", headers=TV).status_code, 403)
hs._proxy = lambda eid: JPEG
r = c.get("/v1/home/cameras/camera.porch/snapshot", headers=TV)
check("through the API on the LAN: the JPEG, no-store", (r.status_code, r.content == JPEG, r.headers.get("cache-control")),
      (200, True, "no-store"))
hs._proxy = lambda eid: (_ for _ in ()).throw(AssertionError("no real camera proxy in a test"))
check("the camera list, each with where its picture comes from", call("GET", "/v1/home/cameras", TV).json(),
      {"cameras": [{"id": "camera.porch", "name": "Porch", "source": "local"},
                   {"id": "camera.driveway", "name": "Driveway", "source": "cloud"},
                   {"id": "camera.shed", "name": "Shed", "source": "unknown"},
                   {"id": "camera.back_yard", "name": "Back yard", "source": "local"}], "refresh_s": 30})
check("the exact integration names: generic and onvif are local, blink is cloud",
      (hs.sources(fake_ha).get("camera.back_yard"), hs.sources(fake_ha).get("camera.porch"),
       hs.sources(fake_ha).get("camera.driveway")), ("local", "local", "cloud"))
for cam, src in (("camera.driveway", "cloud"), ("camera.shed", "unknown")):
    code, body, _ = hs.snapshot(cam, "dev-tv-src", "10.0.0.31", fetch=lambda e: JPEG, now=5000.0)
    check(f"a {src} camera's still is refused, even on the LAN", (code, body.get("source")), (403, src))
check("…and the local ones are served (the control): ONVIF and Generic Camera",
      [hs.snapshot(c_, "dev-tv-src", "10.0.0.31", fetch=lambda e: JPEG, now=5000.0)[0] for c_ in ("camera.porch", "camera.back_yard")],
      [200, 200])

print("\n── Lock up: asked from the TV, confirmed on an adult's own iPhone ──")
reset_locks()
r = call("POST", "/v1/home/lock-up", TV)
out = r.json()
check("the TV's ask: 202, the unlocked lock only, both adults asked (nobody known at home)",
      (r.status_code, out["locks"], out["asked"]), (202, ["Front door"], 3))
card = lock_card()
check("a card to the adults: Lock up?, exactly Lock and Not now",
      (card["title"], [a["id"] for a in card["actions"]], card["data"]["confirm_on"]), ("Lock up?", ["lock", "decline"], "iphone"))
check("…saying what and from where", card["detail"], "Lock Front door. Asked from the Living room TV.")
check("no card action anywhere unlocks", [a["value"].split(":")[0] for a in card["actions"]], ["lock", "decline"])
check("nothing has called Home Assistant's services yet", [p for m, p, _ in CALLS if "/services/" in p], [])
res = tap(card["id"], "lock", ALEX_MAC)
check("Alex's tap on the Mac: refused, nothing locked", (res["ok"], res["said"], STATE["lock.front_door"]["state"]),
      (False, "Confirm Lock Up on your iPhone.", "unlocked"))
check("…the card is still there for the iPhone", lock_card() is not None, True)
res = tap(card["id"], "lock", ALEX)
check("Alex's tap on his iPhone: locked, and titan says what it read back",
      (res["ok"], res["said"], STATE["lock.front_door"]["state"]), (True, "Front door locked.", "locked"))
check("lock.lock was called once, for that lock only", [b for m, p, b in CALLS if p == "/api/services/lock/lock"],
      [{"entity_id": "lock.front_door"}])
check("the first tap wins: Robin's later tap does nothing", tap(card["id"], "lock", ROBIN).get("said"), "Already answered.")
check("the TV sees how it went", call("GET", f"/v1/home/lock-up/{out['request_id']}", TV).json(),
      {"state": "locked", "results": [{"name": "Front door", "state": "locked"}], "said": "Front door locked."})
check("…and nobody else's device can follow it", call("GET", f"/v1/home/lock-up/{out['request_id']}", ALEX).status_code, 404)
con = sqlite3.connect(os.environ["LOCK_UP_DB"])
check("an audit row for the person who confirmed", [(r[0], json.loads(r[1])) for r in con.execute("SELECT person_id, results FROM audit")],
      [(alex, [{"name": "Front door", "state": "locked"}])])
con.close()
r = call("POST", "/v1/home/lock-up", TV)
check("everything locked: 200 already, no card", (r.status_code, r.json()), (200, {"already": True, "said": "Everything's locked."}))

print("\n── a jammed lock is said as jammed ──")
STATE["lock.front_door"]["state"] = "unlocked"; STATE["lock.back_door"]["state"] = "unlocked"; JAM.add("lock.back_door")
out = call("POST", "/v1/home/lock-up", ALEX).json()
check("an adult asking from their phone confirms it themselves", out["asked"], 1)
res = tap(lock_card()["id"], "lock", ALEX)
check("one locked, one didn't", res["said"], "Front door locked. Back door didn't lock (jammed).")
check("…the request is partly", call("GET", f"/v1/home/lock-up/{out['request_id']}", ALEX).json()["state"], "partly")

print("\n── a stale card never locks ──")
reset_locks()
code, out = hs.request({"role": "household", "person_id": None, "device_id": "dev-tv2", "device_name": "Den TV"},
                       hh.get_store().people(), now=time.time())
card = lock_card()
ok, said = hs.answer(card["actions"][0]["value"], alex, "adult", {"platform": "ios"}, now=time.time() + 600)
check("tapped 10 minutes later: inert", (ok, said, STATE["lock.front_door"]["state"]), (False, "That request ran out. Ask again.", "unlocked"))
ok, said = hs.answer(card["actions"][0]["value"], alex, "adult", {"platform": "ios"}, now=time.time() + 601)
check("…and stays inert", (ok, STATE["lock.front_door"]["state"]), (False, "unlocked"))

print("\n── the switch at the tap ──")
reset_locks()
code, out = hs.request({"role": "household", "person_id": None, "device_id": "dev-tv3", "device_name": "Den TV"},
                       hh.get_store().people())
card = lock_card()
os.environ.pop("HA_LOCK_UP")
res = tap(card["id"], "lock", ALEX)
check("turned off after the ask: the tap says it's off and nothing locks", (res["said"], STATE["lock.front_door"]["state"]),
      (hs.OFF, "unlocked"))
os.environ["HA_LOCK_UP"] = "1"
res = tap(card["id"], "decline", ROBIN)
check("Not now: nothing locked", (res["said"], STATE["lock.front_door"]["state"]), ("OK, nothing was locked.", "unlocked"))

print("\n── a tap relayed from the Watch never locks (jellybean#900) ──")
reset_locks()
code, out = hs.request({"role": "household", "person_id": None, "device_id": "dev-tv-w", "device_name": "Den TV"},
                       hh.get_store().people())


def card_for(rid):
    return next(x for x in proactive.list_cards(include_acked=True) if x.get("source") == "lock-up"
                and any(a["value"].endswith(":" + rid) for a in x["actions"]))


card = card_for(out["request_id"])


def tap_via(card_id, action, h, via):
    return c.post(f"/v1/proactive/{card_id}/action", json={"action_id": action, "via": via}, headers=h).json()


calls_before = len([1 for m, p_, b in CALLS if p_ == "/api/services/lock/lock"])
con = sqlite3.connect(os.environ["LOCK_UP_DB"])
n_audit = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
con.close()
res = tap_via(card["id"], "lock", ALEX, "watch")
check("Lock from the Watch through Alex's own iPhone: the fixed line, nothing locked",
      (res["ok"], res["said"], STATE["lock.front_door"]["state"]), (False, hs.WATCH_LOCK, "unlocked"))
check("…the line is the Watch card's own", hs.WATCH_LOCK, "Lock it on your iPhone.")
check("…no lock service was called", len([1 for m, p_, b in CALLS if p_ == "/api/services/lock/lock"]), calls_before)
res = tap_via(card["id"], "lock", ALEX, "WATCH")
check("…any spelling of watch", (res["ok"], res["said"]), (False, hs.WATCH_LOCK))
check("…and still no lock call", len([1 for m, p_, b in CALLS if p_ == "/api/services/lock/lock"]), calls_before)
check("…the card stays for the iPhone", card_for(out["request_id"]).get("acked_at"), None)
con = sqlite3.connect(os.environ["LOCK_UP_DB"])
rows = con.execute("SELECT person_id, results, via FROM audit ORDER BY rowid").fetchall()[n_audit:]
con.close()
check("the refused wrist taps are audit rows with via watch and nothing sent",
      [(r[0], json.loads(r[1]), r[2]) for r in rows], [(alex, [], "watch"), (alex, [], "watch")])
for via in ("phone", "", None, 7):
    check(f"an unknown or missing via ({via!r}) is today's tap: it isn't the Watch refusal",
          proactive.card_action(card["id"], "nope", person=alex, role="adult", device={"platform": "ios"}, via=via).get("said")
          != hs.WATCH_LOCK, True)
res = tap_via(card["id"], "lock", ALEX, "phone")
check("an unknown via on the iPhone's own tap locks, as today", (res["ok"], STATE["lock.front_door"]["state"]),
      (True, "locked"))
con = sqlite3.connect(os.environ["LOCK_UP_DB"])
check("…its audit row has no via", con.execute("SELECT via FROM audit ORDER BY rowid DESC LIMIT 1").fetchone()[0], None)
con.close()
reset_locks()
code, out = hs.request({"role": "household", "person_id": None, "device_id": "dev-tv-w2", "device_name": "Den TV"},
                       hh.get_store().people())
card = card_for(out["request_id"])
res = tap_via(card["id"], "decline", ROBIN, "watch")
check("Not now from the Watch still works", (res["ok"], res["said"], STATE["lock.front_door"]["state"]),
      (True, "OK, nothing was locked.", "unlocked"))
con = sqlite3.connect(os.environ["LOCK_UP_DB"])
check("an older lock_up.db gains the via column", "via" in {r[1] for r in con.execute("PRAGMA table_info(audit)")}, True)
con.close()
old_db = _TMP / "old_lock_up.db"
con = sqlite3.connect(old_db)
con.execute("CREATE TABLE audit (request_id TEXT, person_id TEXT, at REAL, results TEXT)")
con.execute("INSERT INTO audit VALUES ('r1', 'p1', 1.0, '[]')")
con.commit(); con.close()
saved_db = os.environ["LOCK_UP_DB"]
os.environ["LOCK_UP_DB"] = str(old_db)
hs._db().close()
con = sqlite3.connect(old_db)
check("…migrated in place, old rows kept with via empty", con.execute("SELECT request_id, via FROM audit").fetchall(), [("r1", None)])
con.close()
os.environ["LOCK_UP_DB"] = saved_db

print("\n── unlocking is refused at every layer ──")
check("no route unlocks, opens or disarms", [p for p in docs if re.search(r"unlock|disarm|open", p)]
      if (docs := [ln.split("`")[1] for ln in (Path(__file__).parent.parent / "docs" / "api-routes.md").read_text().splitlines()
                   if "/v1/home" in ln and "`" in ln]) else ["no routes read"], [])
code, out = hs.request({"role": "household", "person_id": None, "device_id": "dev-tv4", "device_name": "Den TV"},
                       hh.get_store().people())
rid = out["request_id"]
for v in (f"unlock:{rid}", f"open:{rid}", f"disarm:{rid}", "unlock:", ""):
    ok, said = hs.answer(v, alex, "adult", {"platform": "ios"})
    check(f"a card value {v!r} does nothing", (ok, said), (False, "titan didn't understand that."))
for action in ("unlock", "open", "off", "toggle"):
    said = ha.home_control({"device": "Front door", "action": action}, http=fake_ha)
    check(f"home_control {action} on the front door: hands-off, as before", said.startswith("Front door is a lock"), True)
said = ha.home_control({"device": "Garage door", "action": "open"}, http=fake_ha)
check("…and the garage door stays status-only", said.startswith("Garage door is a cover"), True)
src = (Path(__file__).parent / "home_security.py").read_text()
check("home_security.py has one services path, lock/lock", re.findall(r"/api/services/[a-z_/]+", src), ["/api/services/lock/lock"])
import agent_runtime_lib as lib  # noqa: E402
check("the lock_up tool takes no arguments to aim it elsewhere", lib._TOOL_REGISTRY["lock_up"]["parameters"], {})
check("no chat tool unlocks", [n for n in lib._TOOL_REGISTRY if "unlock" in n], [])

print("\n── nothing connected yet (titan's Home Assistant today: 0 locks, 0 cameras) ──")
saved = dict(STATE)
for eid in [e for e in STATE if e.startswith(("lock.", "camera."))]:
    del STATE[eid]
s = call("GET", "/v1/home/security", ALEX).json()
check("status: no locks and nothing said about them", (s["locks"], "lock" in s["summary"].lower(), s["summary"]),
      ([], False, "Garage closed"))
cards_before = {x["id"] for x in proactive.list_cards(include_acked=True)}
r = call("POST", "/v1/home/lock-up", TV)
check("Lock up: says no locks are connected, and makes no card",
      (r.status_code, r.json().get("said"), {x["id"] for x in proactive.list_cards(include_acked=True)} - cards_before),
      (200, "No locks are connected to Jellybean yet.", set()))
check("…the chat tool says the same", hs.tool({}, alex, "adult"), "No locks are connected to Jellybean yet.")
r = call("GET", "/v1/home/cameras", TV)
check("cameras: an empty list, no error", (r.status_code, r.json()), (200, {"cameras": [], "refresh_s": 30}))
for eid in [e for e in STATE if e.startswith("cover.")] + ["binary_sensor.patio_door"]:
    del STATE[eid]
check("nothing at all: an empty summary, still 200", (call("GET", "/v1/home/security", ALEX).status_code,
      call("GET", "/v1/home/security", ALEX).json()["summary"]), (200, ""))
STATE.clear(); STATE.update(saved)

print("\n── the Home Assistant fake saw nothing it shouldn't ──")
check("no service but lock.lock, and lock.lock only inside a confirmed tap", VIOLATIONS, [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
