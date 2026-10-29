#!/usr/bin/env python3
# gate: hermetic
"""A box's first run (titan-node#411): off on titan (every setup route 404, the page too); on a fresh box the page
and /v1/setup/* answer only with the one-time setup key (401 without it, a wrong key counts against the limiter);
name the owner, add people (never a second admin), whose calendar is whose, a #157 pairing code the phone claims;
the owner's first paired device closes setup and deletes the key; finish closes it too. Fictional people; throwaway
stores. Run: python3 scripts/test_box_setup.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-boxsetup-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "BOX_SETUP_DIR": "box-setup", "BOX_BACKUP_PUB": "backup.pub.json", "BOX_BACKUP_SIGN_KEY": "backup.sign.key"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ["TITAN_PAIR_URLS"] = "http://10.0.0.9:7770"
os.environ.pop("BOX_SETUP", None)
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
    print(f"  [{'ok' if ok else 'XX'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import box_setup  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
KEY_FILE = _TMP / "box-setup" / "setup-key"

print("── titan: no setup ──")
check("page 404", c.get("/setup").status_code, 404)
check("state 404 even with a key-shaped header", c.get("/v1/setup/state", headers={"X-Setup-Key": "x" * 32}).status_code, 404)
api._box_setup_at_start()
check("no key file made, not even at startup", KEY_FILE.exists(), False)

print("── a fresh box ──")
os.environ["BOX_SETUP"] = "1"
box_setup.limiter = hh.ClaimLimiter()
api._box_setup_at_start()
check("startup makes the key (the sticker's)", KEY_FILE.exists(), True)
page = c.get("/setup")
check("the page is served, holds no key and no names", (page.status_code, "X-Setup-Key" in page.text,
      "Alex" in page.text, page.headers.get("cache-control")), (200, True, False, "no-store"))
check("state without the key: 401", c.get("/v1/setup/state").status_code, 401)
check("a wrong key: 401", c.get("/v1/setup/state", headers={"X-Setup-Key": "nope"}).status_code, 401)
urls = box_setup.setup_urls()
key = KEY_FILE.read_text().strip()
check("the sticker URL carries the key in the fragment", urls, [f"http://10.0.0.9:7770/setup#k={key}"])
check("the key file is the service user's only", oct(KEY_FILE.stat().st_mode & 0o777), "0o600")
K = {"X-Setup-Key": key}
st = c.get("/v1/setup/state", headers=K).json()
check("state: nothing done yet", (st["needs_setup"], st["steps"]), (True, {"owner_named": False, "people_added": False,
                                                                         "calendars": False, "recovery": False, "home": False,
                                                                         "owner_paired": False}))
check("the master token doesn't stand in for the key", c.get("/v1/setup/state", headers={"X-Agent-Token": MASTER}).status_code, 401)

r = c.post("/v1/setup/owner", json={"name": "Alex Hale"}, headers=K)
check("name the owner", (r.status_code, r.json()["name"], r.json()["role"]), (200, "Alex Hale", "admin"))
r = c.post("/v1/setup/people", json={"name": "Robin Hale", "role": "adult"}, headers=K)
robin = r.json()["id"]
check("add an adult", (r.status_code, r.json()["role"]), (200, "adult"))
ava = c.post("/v1/setup/people", json={"name": "Ava Hale", "role": "kid"}, headers=K).json()["id"]
check("a second admin: 400", c.post("/v1/setup/people", json={"name": "Leo", "role": "admin"}, headers=K).status_code, 400)
check("the same name twice: 409", c.post("/v1/setup/people", json={"name": "robin hale", "role": "adult"},
                                         headers=K).status_code, 409)
check("people without the key: 401", c.post("/v1/setup/people", json={"name": "Jordan", "role": "adult"}).status_code, 401)
r = c.post("/v1/setup/calendars", json={"person_id": ava, "calendars": ["Ava school", "Swim"]}, headers=K)
check("whose calendar is whose", (r.status_code, r.json()["calendars"]), (200, ["Ava school", "Swim"]))
check("someone else's calendar: 409", c.post("/v1/setup/calendars", json={"person_id": robin, "calendars": ["swim"]},
                                             headers=K).status_code, 409)
check("calendars not a list: 400", c.post("/v1/setup/calendars", json={"person_id": robin, "calendars": "Swim"},
                                          headers=K).status_code, 400)
st = c.get("/v1/setup/state", headers=K).json()
check("state: owner, people, calendars done", st["steps"], {"owner_named": True, "people_added": True,
                                                            "calendars": True, "recovery": False, "home": False, "owner_paired": False})
check("everyone listed", sorted(p["name"] for p in st["people"]), ["Alex Hale", "Ava Hale", "Robin Hale"])

print("── pairing (#157's codes) ──")
p = c.post("/v1/setup/pair", json={"person_id": robin}, headers=K).json()
check("Robin's code and titan://pair link", (len(p["code"]), p["qr"].startswith("titan://pair?c=" + p["code"])), (6, True))
cl = c.post("/v1/pair/claim", json={"code": p["code"], "device_name": "Robin's iPhone", "platform": "ios"}).json()
check("Robin's phone pairs as Robin", cl["person"]["name"], "Robin Hale")
check("Robin's phone doesn't end setup", c.get("/v1/setup/state", headers=K).status_code, 200)
check("unknown person: 404", c.post("/v1/setup/pair", json={"person_id": "p_nobody"}, headers=K).status_code, 404)
o = c.post("/v1/setup/pair", json={"person_id": hh.ADMIN_PERSON_ID}, headers=K).json()
tok = c.post("/v1/pair/claim", json={"code": o["code"], "device_name": "Alex's iPhone", "platform": "ios"}).json()["token"]
me = c.get("/v1/me", headers={"X-Agent-Token": tok}).json()
check("the owner's phone is admin, named", (me["role"], me["person"]["name"]), ("admin", "Alex Hale"))
check("…and setup is over: 404, key deleted", (c.get("/v1/setup/state", headers=K).status_code, KEY_FILE.exists()),
      (404, False))
check("the page is gone too", c.get("/setup").status_code, 404)
check("after setup the owner adds people the usual way", c.post("/v1/people", json={"name": "Jordan Hale", "role": "adult"},
      headers={"X-Agent-Token": tok}).status_code, 201)

print("── finish by hand; the limiter ──")
hh.reset_store(hh.HouseholdStore(_TMP / "api2.db"))
for f in (_TMP / "box-setup").iterdir():
    f.unlink()
box_setup.limiter = hh.ClaimLimiter()
K2 = {"X-Setup-Key": box_setup.ensure_key()}
check("a second fresh box is open", c.get("/v1/setup/state", headers=K2).status_code, 200)
check("finish", c.post("/v1/setup/finish", headers=K2).json(), {"needs_setup": False})
check("…then 404", (c.get("/v1/setup/state", headers=K2).status_code, KEY_FILE.exists()), (404, False))
hh.reset_store(hh.HouseholdStore(_TMP / "api3.db"))
for f in (_TMP / "box-setup").iterdir():
    f.unlink()
box_setup.limiter = hh.ClaimLimiter()
codes = [c.get("/v1/setup/state", headers={"X-Setup-Key": f"guess-{i}"}).status_code for i in range(15)]
check("guessing runs into the limiter", (codes[0], 429 in codes), (401, True))
check("…while the right key still gets in", c.get("/v1/setup/state", headers={"X-Setup-Key": box_setup.ensure_key()}).status_code,
      200)

print("── the app's one-call owner claim ──")
hh.reset_store(hh.HouseholdStore(_TMP / "api4.db"))
for f in (_TMP / "box-setup").iterdir():
    f.unlink()
box_setup.limiter = hh.ClaimLimiter()
K4 = {"X-Setup-Key": box_setup.ensure_key()}
check("a bad platform: 400 before any code is made", (c.post("/v1/setup/claim", json={"device_name": "x", "platform": "fax"},
      headers=K4).status_code, c.get("/v1/setup/state", headers=K4).status_code), (400, 200))
r = c.post("/v1/setup/claim", json={"name": "Jordan Hale", "device_name": "Jordan's iPhone", "platform": "ios"}, headers=K4)
tok4 = r.json().get("token")
check("claim: the owner, named, with a device token", (r.status_code, r.json()["person"]["name"], r.json()["person"]["role"],
      r.json()["device"]["name"]), (200, "Jordan Hale", "admin", "Jordan's iPhone"))
check("…the token works as the owner", c.get("/v1/me", headers={"X-Agent-Token": tok4}).json()["role"], "admin")
check("…and only once", c.post("/v1/setup/claim", json={"device_name": "x", "platform": "ios"}, headers=K4).status_code, 404)

print("── review fixes (JEV, 10-07) ──")
import threading  # noqa: E402


def fresh(name):
    hh.reset_store(hh.HouseholdStore(_TMP / name))
    for f in (_TMP / "box-setup").iterdir():
        f.unlink()
    box_setup.limiter = hh.ClaimLimiter()
    return {"X-Setup-Key": box_setup.ensure_key()}


def open_codes(pid):
    return hh.get_store()._db.execute("SELECT COUNT(*) FROM pair_codes WHERE person_id=? AND used_at IS NULL",
                                      (pid,)).fetchone()[0]


K5 = fresh("api5.db")
results = []
gate = threading.Barrier(8)


def racer(i):
    gate.wait()
    r = c.post("/v1/setup/claim", json={"device_name": f"phone {i}", "platform": "ios"}, headers=K5)
    results.append(r.status_code)


ts = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
admins = [d for p in hh.get_store().people() if p["role"] == "admin" for d in p["devices"] if not d["revoked"]]
check("8 racing owner claims: exactly one wins, the rest 404", (sorted(results).count(200), len(admins),
      sorted(set(results))), (1, 1, [200, 404]))

K6 = fresh("api6.db")
a1 = c.post("/v1/setup/pair", json={"person_id": hh.ADMIN_PERSON_ID}, headers=K6).json()["code"]
a2 = c.post("/v1/setup/pair", json={"person_id": hh.ADMIN_PERSON_ID}, headers=K6).json()["code"]
check("a new owner code replaces the unused older one", (open_codes(hh.ADMIN_PERSON_ID),
      c.post("/v1/pair/claim", json={"code": a1, "device_name": "x", "platform": "ios"}).status_code), (1, 404))
rid = c.post("/v1/setup/people", json={"name": "Leo Hale", "role": "kid"}, headers=K6).json()["id"]
c.post("/v1/setup/pair", json={"person_id": rid}, headers=K6)
c.post("/v1/setup/finish", headers=K6)
check("finish deletes every unused code made during setup, the owner's and a kid's (#1188)",
      (open_codes(hh.ADMIN_PERSON_ID), open_codes(rid),
       c.post("/v1/pair/claim", json={"code": a2, "device_name": "x", "platform": "ios"}).status_code), (0, 0, 404))

K7 = fresh("api7.db")
hh.get_store().limiter._locked_until = 10 ** 12   # 30 junk codes have locked every /v1/pair/claim out
r = c.post("/v1/setup/claim", json={"device_name": "Ava's iPhone", "platform": "ios"}, headers=K7)
check("the keyed owner claim isn't behind the global claim lockout", r.status_code, 200)
hh.get_store().limiter._locked_until = 0

K8 = fresh("api8.db")
import unittest.mock as _um  # noqa: E402
with _um.patch.object(hh.HouseholdStore, "_claim", side_effect=hh.PairingError(410, "boom")):
    r = c.post("/v1/setup/claim", json={"device_name": "x", "platform": "ios"}, headers=K8)
check("a failed keyed claim leaves no owner code behind", (r.status_code, open_codes(hh.ADMIN_PERSON_ID)), (410, 0))

K9 = fresh("api9.db")
check("the key's first use starts its 30 minutes", c.get("/v1/setup/state", headers=K9).status_code, 200)
(_TMP / "box-setup" / "key-first-used").write_text(str(__import__("time").time() - 31 * 60))
check("after 30 minutes the key is 410", c.get("/v1/setup/state", headers=K9).status_code, 410)
api._box_setup_at_start()
check("a power-cycle re-arms the same key", c.get("/v1/setup/state", headers=K9).status_code, 200)
page = c.get("/setup").text
check("the page stops polling once the owner is named", "clearInterval(poll)" in page, True)


print("── the recovery phrase step (#411, #407): made here, shown once ──")
import box_backup  # noqa: E402
K10 = fresh("api10.db")
check("without the key: 401", c.post("/v1/setup/recovery").status_code, 401)
r = c.post("/v1/setup/recovery", headers=K10)
j = r.json()
check("the phrase, its group count and both fingerprints, never cached",
      (r.status_code, sorted(j), j["groups"], len(j["phrase"].split("-")), r.headers.get("cache-control")),
      (200, ["groups", "key", "phrase", "sender"], box_backup.PHRASE_GROUPS + 1, box_backup.PHRASE_GROUPS + 1, "no-store"))
pub = Path(os.environ["BOX_BACKUP_PUB"])
check("the box keeps the public key and the signing key, 0600; the phrase is in neither",
      (oct(pub.stat().st_mode & 0o777), oct(Path(os.environ["BOX_BACKUP_SIGN_KEY"]).stat().st_mode & 0o777),
       j["phrase"] in pub.read_text() or j["phrase"] in Path(os.environ["BOX_BACKUP_SIGN_KEY"]).read_text()),
      ("0o600", "0o600", False))
r2 = c.post("/v1/setup/recovery", headers=K10)
check("asking again: 409, and no phrase", (r2.status_code, "phrase" in r2.json()), (409, False))
check("state marks the step done", c.get("/v1/setup/state", headers=K10).json()["steps"]["recovery"], True)
import io  # noqa: E402
sealed, opened = io.BytesIO(), io.BytesIO()
box_backup.seal(io.BytesIO(b"household data"), sealed)
sealed.seek(0)
box_backup.unseal(sealed, opened, j["phrase"])
check("the phrase shown opens a backup the box seals", opened.getvalue(), b"household data")
check("the page shows it once, then hides the button", ("/v1/setup/recovery" in page, "only time the box shows it" in page),
      (True, True))
os.environ.pop("BOX_SETUP", None)
check("on titan (no setup): 404", c.post("/v1/setup/recovery", headers=K10).status_code, 404)
os.environ["BOX_SETUP"] = "1"


print("── connect the house (#411 step 5): the box onboards its own HA, lists the hands-off devices ──")
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("ha_stub", Path(__file__).parent / "sandbox_ha_stub.py")
stub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stub)
stub.FRESH["on"] = True
hsrv = stub.ThreadingHTTPServer(("127.0.0.1", 0), stub.H)
threading.Thread(target=hsrv.serve_forever, daemon=True).start()
os.environ["BOX_HA_URL"] = f"http://127.0.0.1:{hsrv.server_address[1]}"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "box-ha-token")
K11 = fresh("api11.db")
check("without the key: 401", c.post("/v1/setup/home").status_code, 401)
r = c.post("/v1/setup/home", headers=K11)
j = r.json()
check("connected: HA's four onboarding steps done", (r.status_code, j.get("connected"), all(stub.FRESH["steps"].values())),
      (200, True, True))
check("what it found, by kind", j["devices"], {"climate": 1, "cover": 1, "light": 1, "media_player": 1, "switch": 1})
check("locks, alarms, garage doors and cameras: listed by name, kept out of the device counts",
      ([(h["kind"], h["name"]) for h in j["hands_off"]], "lock" in j["devices"] or "alarm_control_panel" in j["devices"]),
      ([("alarm", "Home Alarm"), ("camera", "Porch Camera"), ("garage", "Garage Door"), ("lock", "Front Door")], False))
tokf = Path(os.environ["HA_TOKEN_FILE"])
check("the agent's token file is written 0600, and the token isn't in the reply",
      (oct(tokf.stat().st_mode & 0o777), tokf.read_text().strip() in r.text), ("0o600", False))
check("state marks the step done", c.get("/v1/setup/state", headers=K11).json()["steps"]["home"], True)
r2 = c.post("/v1/setup/home", headers=K11)
check("asking again only re-reads the list (no second owner)", (r2.status_code, r2.json()["hands_off"] == j["hands_off"]),
      (200, True))
import home_assistant  # noqa: E402
home_assistant.HA_URL, home_assistant.TOKEN_FILE = os.environ["BOX_HA_URL"], tokf
check("the agent's own HA client reads the house with that token", len(home_assistant._http("GET", "/api/states")), 9)
tokf.unlink()
r3 = c.post("/v1/setup/home", headers=K11)
check("an HA that already has an owner the box didn't make: 409, with why", (r3.status_code, "already has an owner" in r3.text),
      (409, True))
hsrv.shutdown()
K12 = fresh("api12.db")
r4 = c.post("/v1/setup/home", headers=K12)
check("HA not answering: 503 with a plain reason", (r4.status_code, "isn't answering" in r4.text), (503, True))
check("the page has the step, and never offers a control for the hands-off kinds",
      ("/v1/setup/home" in page and "never controlled from here" in page, "lock.front_door" in page), (True, False))


print("── the recovery step when the key folder can't be written (10-09 sandbox): 503, not a bare 500 ──")
K13 = fresh("api13.db")
saved = os.environ["BOX_BACKUP_PUB"]
ro = _TMP / "ro-keys"
ro.mkdir(exist_ok=True)
os.chmod(ro, 0o500)
os.environ["BOX_BACKUP_PUB"] = str(ro / "sub" / "pub.json")
r = c.post("/v1/setup/recovery", headers=K13)
check("503 with a plain reason, and no phrase", (r.status_code, "couldn't save its backup key" in r.text, "phrase" in r.text),
      (503, True, False))
os.chmod(ro, 0o700)
os.environ["BOX_BACKUP_PUB"] = saved


print("── /v1/setup/home answers only the setup key: never an adult, kid, screen or the admin's own token ──")
os.environ["BOX_HA_URL"] = "http://127.0.0.1:9"   # never reached: every call below is refused first
K14 = fresh("api14.db")
st14 = hh.get_store()
toks = {}
for role in ("adult", "kid", "household"):
    pid = st14.add_person("Robin" if role == "adult" else "Ava" if role == "kid" else "Den", role)["id"]
    code = st14.start_pairing(pid)["code"]
    toks[role] = st14._claim({"code": code, "device_name": role + " device", "platform": "ios"})["token"]
toks["admin (master)"] = MASTER
for who, t in toks.items():
    r = c.post("/v1/setup/home", headers={"X-Agent-Token": t})
    check(f"{who} token: refused (401), no device list", (r.status_code, "hands_off" in r.text), (401, False))
r = c.post("/v1/setup/home", headers={**K14, "X-Agent-Token": toks["adult"]})
check("…and an adult token doesn't change what the setup key gets (its own reply, not the adult's)", r.status_code in (200, 503), True)
c.post("/v1/setup/finish", headers=K14)
r = c.post("/v1/setup/home", headers=K14)
check("after setup: 404, even with the old key", (r.status_code, "hands_off" in r.text), (404, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
