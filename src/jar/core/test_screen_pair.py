#!/usr/bin/env python3
# gate: hermetic   (throwaway stores; the real routes through a TestClient, fake source addresses)
"""An Apple TV paired with no typing on the remote (#411, jellybean#434; the contract is on #411): the TV shows a code
and a QR, the admin confirms, and the TV collects a household-role token for a device of kind "tv". The Watch's
hardening, kept: 10 minutes, single use, the secret hashed at rest, a code spent after 3 refusals, per-source and global
limits. Plus: Watch and screen codes never cross, only the admin confirms, re-pairing a reset TV, and the adults' card.
Fictional people only (the seeded admin is renamed Hale).
Run: python3 scripts/test_screen_pair.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-screen-pair-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db", "SERVER_IDENTITY_KEY": "id/key",
             "SITTER_DB": "sitter.db", "HANDOFF_DB": "handoff.db", "TV_UNLOCK_DB": "tv_unlock.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def status(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return 200
    except hh.PairingError as exc:
        return exc.status


import household_identity as hh  # noqa: E402

print("── the watch_pairs migration ──")
old = _TMP / "old.db"
con = sqlite3.connect(old)
con.executescript("""CREATE TABLE watch_pairs (request_id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE,
  secret_sha256 TEXT NOT NULL, device_name TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
  person_id TEXT, confirmed_by TEXT, attempts INTEGER NOT NULL DEFAULT 0, device_id TEXT, closed_at REAL);
INSERT INTO watch_pairs VALUES ('wp_old', '111111', 'x', 'Ava''s Watch', 0, 9e12, NULL, NULL, 0, NULL, NULL);""")
con.commit(); con.close()
hh.HouseholdStore(old); hh.HouseholdStore(old)   # twice: nothing to add the second time
check("an older table gains kind; its rows are Watch requests", [r[0] for r in sqlite3.connect(old).execute(
      "SELECT kind FROM watch_pairs")], ["watch"])

print("── the store ──")
now = [1_800_000_000.0]
store = hh.HouseholdStore(_TMP / "store.db", now=lambda: now[0])
store.update_person(hh.ADMIN_PERSON_ID, name="Hale Morgan")
ADMIN = hh.owner_identity("device")
robin = store.add_person("Robin", "adult")
ROBIN = hh.Identity(robin, {"id": "d_robin", "name": "Robin's iPhone"}, "adult", "device")
ava = store.add_person("Ava", "kid")
store.update_person(ava["id"], parents=[robin["id"]])

r = store.screen_pair_request("Apple TV")
check("a request: sp_ id, 6 digits, a secret", (r["request_id"][:3], len(r["code"]), r["code"].isdigit(), len(r["secret"]) >= 32),
      ("sp_", 6, True, True))
check("the QR holds the code, never the secret", (r["code"] in r["qr"], r["secret"] in r["qr"], r["qr"].startswith("titan://pair-screen?")),
      (True, False, True))
raw = (_TMP / "store.db").read_bytes() + (Path(str(_TMP / "store.db") + "-wal").read_bytes()
                                         if Path(str(_TMP / "store.db") + "-wal").exists() else b"")
check("the secret isn't stored, only its sha256", (r["secret"].encode() in raw, hh.token_sha256(r["secret"]).encode() in raw),
      (False, True))
check("polling before the admin: waiting", store.screen_pair_poll(r["request_id"], r["secret"]), None)
check("an adult can't confirm (403)", status(store.screen_pair_confirm, r["code"], ROBIN, "Living Room TV"), 403)
check("…and that didn't spend the code", store._db.execute("SELECT attempts, closed_at FROM watch_pairs WHERE request_id=?",
      (r["request_id"],)).fetchone()[:], (0, None))
check("a name is required for a new screen (400)", status(store.screen_pair_confirm, r["code"], ADMIN, "  "), 400)
check("a name already in the household (409)", status(store.screen_pair_confirm, r["code"], ADMIN, "robin"), 409)
got = store.screen_pair_confirm(r["code"], ADMIN, "Living Room TV", room="Living room", device_name="Hale's iPhone")
check("the admin confirms", got, {"request_id": r["request_id"], "name": "Living Room TV"})
check("confirming again: used (410)", status(store.screen_pair_confirm, r["code"], ADMIN, "Living Room TV"), 410)
check("the wrong secret: unknown (404)", status(store.screen_pair_poll, r["request_id"], "not-the-secret"), 404)
t = store.screen_pair_poll(r["request_id"], r["secret"])
check("the TV collects its token", (t["device"]["kind"], t["device"]["name"], t["device"]["room"], t["person"]["role"],
      t["person"]["name"]), ("tv", "Living Room TV", "Living room", "household", "Living Room TV"))
check("…and sees who confirmed, on what", t["confirmed_by"], {"first_name": "Hale", "device_name": "Hale's iPhone"})
ident = store.resolve_device(t["token"])
check("the token is household-role, never admin", (ident.role, ident.person_id == hh.ADMIN_PERSON_ID, ident.via),
      ("household", False, "device"))
check("the device record: kind tv, platform tvos, the room", store._db.execute(
      "SELECT kind, platform, room FROM devices WHERE id=?", (t["device"]["id"],)).fetchone()[:], ("tv", "tvos", "Living room"))
check("collecting again: single use (410)", status(store.screen_pair_poll, r["request_id"], r["secret"]), 410)
check("a household token can't reach admin routes", (hh.route_allowed("household", "POST", "/v1/pair/start"),
      hh.route_allowed("household", "POST", "/v1/screen/pair/confirm")), (False, False))

print("── expiry ──")
r2 = store.screen_pair_request(None)
now[0] += hh.CODE_TTL_S + 1
check("an expired code can't be confirmed (410)", status(store.screen_pair_confirm, r2["code"], ADMIN, "Den TV"), 410)
check("…or collected (410)", status(store.screen_pair_poll, r2["request_id"], r2["secret"]), 410)
r3 = store.screen_pair_request(None)
store.screen_pair_confirm(r3["code"], ADMIN, "Den TV")
now[0] += hh.CODE_TTL_S + 1
check("confirmed but not collected in time: expired (410)", status(store.screen_pair_poll, r3["request_id"], r3["secret"]), 410)
check("…and no Den TV was added", [p["name"] for p in store.people() if p["name"] == "Den TV"], [])

print("── Watch and screen codes never cross ──")
w = store.watch_pair_request("Ava's Watch")
s = store.screen_pair_request("Apple TV")
check("a Watch's code at the screen confirm: unknown (404)", status(store.screen_pair_confirm, w["code"], ADMIN, "Kitchen TV"), 404)
check("a screen's code at the Watch confirm: unknown (404)", status(store.watch_pair_confirm, s["code"], ava["id"], ADMIN), 404)
check("a screen request polled as a Watch: unknown (404)", status(store.watch_pair_poll, s["request_id"], s["secret"]), 404)
check("a Watch request polled as a screen: unknown (404)", status(store.screen_pair_poll, w["request_id"], w["secret"]), 404)
check("…and the Watch still pairs its own way", store.watch_pair_confirm(w["code"], ava["id"], ROBIN)["kid_first_name"], "Ava")

print("── pairing a reset TV again ──")
tv = t["person"]["id"]
check("as an adult's person: refused (400)", status(store.screen_pair_confirm, s["code"], ADMIN, person_id=robin["id"]), 400)
check("as a kid: refused (400)", status(store.screen_pair_confirm, s["code"], ADMIN, person_id=ava["id"]), 400)
got = store.screen_pair_confirm(s["code"], ADMIN, person_id=tv, room="Living room")
check("as the household screen it was", got["name"], "Living Room TV")
t2 = store.screen_pair_poll(s["request_id"], s["secret"])
check("…a new device for the same screen", (t2["person"]["id"] == tv, t2["device"]["id"] != t["device"]["id"]), (True, True))
check("…no second person", len([p for p in store.people() if p["name"] == "Living Room TV"]), 1)

print("── a code takes 3 refusals, then it's spent ──")
s3 = store.screen_pair_request("Apple TV")
for _ in range(hh.WATCH_CONFIRM_ATTEMPTS):
    status(store.screen_pair_confirm, s3["code"], ADMIN, person_id=robin["id"])
check("…the right answer now: spent (410)", status(store.screen_pair_confirm, s3["code"], ADMIN, person_id=tv), 410)

print("── the global cap on open codes ──")
for _ in range(hh.MAX_OPEN_CODES):
    store.screen_pair_request(None)
check("MAX_OPEN_CODES screens waiting: the next is refused (429)", status(store.screen_pair_request, None), 429)
check("…a Watch still gets its own code", status(store.watch_pair_request, "Leo's Watch"), 200)

print("── the routes ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import proactive  # noqa: E402

MASTER = "master-screen-pair-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api.db")
hh.reset_store(st)
st.update_person(hh.ADMIN_PERSON_ID, name="Hale Morgan")
M = {"X-Agent-Token": MASTER}
clients: dict = {}


def from_ip(ip):
    if ip not in clients:
        clients[ip] = TestClient(api.app, client=(ip, 50000))
    return clients[ip]


home = from_ip("10.0.0.20")


def paired(pid=None, new=None, device="iPhone"):
    s_ = home.post("/v1/pair/start", json={"person_id": pid} if pid else {"new_person": new}, headers=M).json()
    r_ = home.post("/v1/pair/claim", json={"code": s_["code"], "device_name": device, "platform": "ios"}).json()
    return s_["person"], {"X-Agent-Token": r_["token"]}


_, H = paired(pid=hh.ADMIN_PERSON_ID, device="Hale's iPhone")
p_alex, A = paired(new={"name": "Alex", "role": "adult"}, device="Alex's iPhone")
tvc = from_ip("10.0.0.40")
q = tvc.post("/v1/screen/pair/request", json={"device_name": "Apple TV"})
req = q.json()
check("the TV asks, no token", (q.status_code, sorted(req)), (200, ["code", "expires_at", "qr", "request_id", "secret"]))
poll = lambda: tvc.get(f"/v1/screen/pair/request/{req['request_id']}", headers={"X-Pair-Secret": req["secret"]})  # noqa: E731
check("polling before the admin: 202", poll().status_code, 202)
check("an adult's confirm: 403", home.post("/v1/screen/pair/confirm", json={"code": req["code"], "name": "Living Room TV"},
      headers=A).status_code, 403)
check("no token: 401", home.post("/v1/screen/pair/confirm", json={"code": req["code"], "name": "Living Room TV"}).status_code, 401)
c = home.post("/v1/screen/pair/confirm", json={"code": req["code"], "name": "Living Room TV", "room": "Living room"}, headers=H)
check("the admin's iPhone confirms", (c.status_code, c.json().get("name")), (200, "Living Room TV"))
got = poll()
body = got.json()
check("the TV collects: household token, kind tv, who confirmed, titan's identity",
      (got.status_code, body["person"]["role"], body["device"]["kind"], body["confirmed_by"], "server_identity" in body,
       "secret" in body), (200, "household", "tv", {"first_name": "Hale", "device_name": "Hale's iPhone"}, True, False))
TV = {"X-Agent-Token": body["token"]}
me = home.get("/v1/me", headers=TV).json()
check("/v1/me with the TV's token: household role", (me.get("role") or (me.get("person") or {}).get("role")), "household")
check("…and it can't add another screen", home.post("/v1/screen/pair/confirm", json={"code": "123456", "name": "X"},
      headers=TV).status_code, 403)
check("collecting again: 410", poll().status_code, 410)
cards = proactive.list_cards(person=p_alex["id"], role="adult")
joined = [x for x in cards if x.get("title") == "Living Room TV joined"]
check("the adults get a card: Living Room TV joined", (len(joined), joined[0].get("source") if joined else None), (1, "devices"))
check("…saying who added it, on what", joined[0].get("detail") if joined else None, "Added by Hale on Hale's iPhone.")
kid_p, K = paired(new={"name": "Leo", "role": "kid"}, device="Leo's iPad")
check("…not to a kid", [x for x in proactive.list_cards(person=kid_p["id"], role="kid") if x.get("title") == "Living Room TV joined"], [])

print("── the routes' limits ──")
bad = from_ip("10.0.0.66")
got = [bad.post("/v1/screen/pair/request", json={}).status_code for _ in range(hh.WATCH_CODES_PER_SOURCE + 2)]
check("one source gets WATCH_CODES_PER_SOURCE codes, then 429", got, [200] * hh.WATCH_CODES_PER_SOURCE + [429, 429])
other = from_ip("10.0.0.41").post("/v1/screen/pair/request", json={"device_name": "Apple TV"})
check("…another TV still gets its code", other.status_code, 200)
o = other.json()
for _ in range(hh.WATCH_FAILURES_PER_SOURCE):
    bad.get(f"/v1/screen/pair/request/{o['request_id']}", headers={"X-Pair-Secret": "wrong"})
check("guessing secrets locks out that source", bad.get(f"/v1/screen/pair/request/{o['request_id']}",
      headers={"X-Pair-Secret": "wrong"}).status_code, 429)
check("…not the real TV", from_ip("10.0.0.41").get(f"/v1/screen/pair/request/{o['request_id']}",
      headers={"X-Pair-Secret": o["secret"]}).status_code, 202)
for _ in range(hh.WATCH_FAILURES_PER_SOURCE):
    home.post("/v1/screen/pair/confirm", json={"code": "000000", "name": "Den TV"}, headers=H)
check("wrong codes lock out the confirming device", home.post("/v1/screen/pair/confirm",
      json={"code": o["code"], "name": "Den TV"}, headers=H).status_code, 429)
check("…a Watch's pairing has its own book", from_ip("10.0.0.66").post("/v1/watch/pair/request", json={}).status_code, 200)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
