#!/usr/bin/env python3
"""Push to the iPhone (push.py + its hooks), against a fake APNs. Hermetic.
Run: python3 scripts/test_push.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-push-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import push  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


TOK = "a" * 64
print("── registration ──")
push.register("dev-z", TOK, "development", "com.zachshallbetter.jellybean.ios")
push.register("dev-m", "b" * 64, "production", "com.zachshallbetter.jellybean.ios")
push.register("dev-k", "c" * 64, "development", "com.zachshallbetter.jellybean.ios")
push.register("dev-h", "d" * 64, "development", "com.zachshallbetter.jellybean.ios")
for bad in ((("dev-x", "nothex!", "development", "com.x.y")), (("dev-x", TOK, "staging", "com.x.y")), (("dev-x", TOK, "development", "bad bundle"))):
    try:
        push.register(*bad); check(f"refused {bad[1:3]}", False, True)
    except ValueError:
        check(f"refused {bad[1:3]}", True, True)
push.register("dev-z", "e" * 64, "development", "com.zachshallbetter.jellybean.ios")
check("re-registering replaces the token", [t["token"] for t in push._tokens() if t["device_id"] == "dev-z"], ["e" * 64])

DEVICES = [{"id": "dev-z", "person_id": "zach", "role": "admin"}, {"id": "dev-m", "person_id": "p_mir", "role": "adult"},
           {"id": "dev-k", "person_id": "p_ava", "role": "kid"}, {"id": "dev-h", "person_id": "p_kitchen", "role": "household"},
           {"id": "dev-old", "person_id": "zach", "role": "admin", "revoked": True}]
print("\n── which cards push, to whom ──")
check("calm and unaddressed: no", push.should_push({"urgency": 1, "to": "household"}), False)
check("urgency 2: yes", push.should_push({"urgency": 2, "to": "household"}), True)
check("addressed by name at urgency 1: yes", push.should_push({"urgency": 1, "to": '["p_mir"]'}), True)
check("addressed by name at urgency 0 (after-the-fact news): no", push.should_push({"urgency": 0, "to": '["p_mir"]'}), False)
check("urgency 0 to a group: no", push.should_push({"urgency": 0, "to": "household"}), False)
# jellybean#289: engineering cards wait in their folded card; only an urgent one interrupts.
import proactive as _pc  # noqa: E402
check("engineering cards are grouped by source", [_pc.card_group(s) for s in ("trigger:git", "trigger:checks",
      "trigger:mac", "titan", "scheduled", None)], ["engineering"] * 3 + [None] * 3)
check("an engineering card at urgency 2 doesn't push", push.should_push({"urgency": 2, "to": "admins",
      "group": "engineering"}), False)
check("…an urgent one (3) does", push.should_push({"urgency": 3, "to": "admins", "group": "engineering"}), True)
who = lambda card: sorted(t["device_id"] for t in push.targets(card, DEVICES))  # noqa: E731
check("an admin card: Zach's phone only", who({"id": "c1", "urgency": 2, "to": "admins"}), ["dev-z"])
check("a household card: everyone's", who({"id": "c2", "urgency": 2, "to": "household"}), ["dev-h", "dev-k", "dev-m", "dev-z"])
check("adults: not the kid or the kitchen", who({"id": "c3", "urgency": 2, "to": "adults"}), ["dev-m", "dev-z"])
check("addressed to Robin: her phone only", who({"id": "c4", "urgency": 0, "to": '["p_mir"]'}), ["dev-m"])
push.mark_live("dev-m")
check("her app is open: no push (the card is on screen)", who({"id": "c5", "urgency": 2, "to": '["p_mir"]'}), [])
con = push._db(); con.execute("UPDATE device_live SET at=? WHERE device_id='dev-m'", (time.time() - 120,)); con.commit(); con.close()
check("…a minute later she's back to push", who({"id": "c6", "urgency": 2, "to": '["p_mir"]'}), ["dev-m"])

print("\n── sending (fake APNs) ──")
sent = []
def fake(t, card):
    sent.append((t["device_id"], card["id"]))
    return {"dev-k": (410, "Unregistered"), "dev-h": (400, "BadDeviceToken")}.get(t["device_id"], (200, ""))
out = push.push_card({"id": "c7", "urgency": 3, "to": "household", "key": "washer"}, devices=DEVICES, send=fake)
check("every target is tried", sorted(d for d, _ in sent), ["dev-h", "dev-k", "dev-m", "dev-z"])
check("dead tokens are dropped", sorted(t["device_id"] for t in push._tokens()), ["dev-m", "dev-z"])
check("a calm unaddressed card sends nothing", push.push_card({"id": "c8", "urgency": 0, "to": "household"}, devices=DEVICES, send=fake), [])
check("not configured and no sender: nothing", push.push_card({"id": "c9", "urgency": 3, "to": "admins"}, devices=DEVICES), [])
p = push.payload({"id": "c10", "key": "dinner", "title": "Secret title", "detail": "Secret detail"})
check("the payload carries no content, and no key (the phone finds the card by its id)",
      ("Secret" in str(p), p["card"], "key" in p, p["aps"]["mutable-content"], p["aps"]["category"]),
      (False, "c10", False, 1, "jb.card"))
check("Time to leave is time-sensitive", push.payload({"id": "c11", "key": "leave-dentist"})["aps"].get("interruption-level"), "time-sensitive")

print("\n── the payload's fixed fields match the phone's rules (jellybean#1056 CardNotice) ──")
def aps(card, **kw):
    return push.payload({"id": "x", "urgency": 1, **card}, **kw)["aps"]
check("a safety source: jb.card.safety, time-sensitive, relevance 1.0, sound",
      [(a["category"], a["interruption-level"], a["relevance-score"], "sound" in a) for a in
       (aps({"source": s_, "urgency": 2}) for s_ in ("lock-up", "home-security", "alarm", "security"))],
      [("jb.card.safety", "time-sensitive", 1.0, True)] * 4)
check("…an exact match only: a prefixed source ('trigger:alarm', 'kid:alarm', 'device:alarm') is an ordinary card "
      "(titan-node#1289 review)", [aps({"source": s_, "urgency": 2})["category"] for s_ in
                                    ("trigger:alarm", "kid:alarm", "device:alarm")], ["jb.card"] * 3)
check("…and busy elsewhere doesn't make it quiet", aps({"source": "lock-up"}, passive=True)["interruption-level"], "time-sensitive")
check("a shared household screen can't mute a card: jb.card.safety there, jb.card on a person's phone",
      (aps({"source": "routine"}, role="household")["category"], aps({"source": "routine"}, role="adult")["category"]),
      ("jb.card.safety", "jb.card"))
check("urgency 1-3: active, relevance 0.6, with sound",
      [(aps({"source": "routine", "urgency": u})["interruption-level"], aps({"source": "routine", "urgency": u})["relevance-score"],
        "sound" in aps({"source": "routine", "urgency": u})) for u in (1, 2, 3)], [("active", 0.6, True)] * 3)
q0 = aps({"source": "mail-bills", "urgency": 0})
check("urgency 0: passive, relevance 0.2, no sound", (q0["interruption-level"], q0["relevance-score"], "sound" in q0),
      ("passive", 0.2, False))
check("Time to leave: relevance 1.0", aps({"key": "leave-ab12"})["relevance-score"], 1.0)
THREAD_CASES = [({"source": "bill-due", "key": "bill-due:1a2b"}, "bills"), ({"source": "mail-bills", "key": "payfail-9f"}, "bills"),
                ({"source": "titan", "key": "payfail-9f"}, "bills"), ({"source": "approve-cap"}, "bills"),
                ({"source": "calendar"}, "calendar"), ({"source": "trigger:calendar"}, "calendar"),
                ({"source": "next-stop"}, "calendar"), ({"source": "titan", "key": "leave-ab12"}, "calendar"),
                ({"source": "lock-up"}, "house"), ({"source": "sitter"}, "house"), ({"source": "home"}, "house"),
                ({"source": "mail"}, "mail"), ({"source": "kb-digest"}, "mail"), ({"source": "mail-move"}, "mail"),
                ({"source": "agent"}, "chat"), ({"source": "titan"}, "chat"), ({"source": "routine"}, "routines"),
                ({"source": "tasks"}, "tasks"), ({"source": "scheduled"}, "tasks"), ({"source": "todos"}, "tasks"),
                ({"source": "note", "from": "Robin"}, "cards"), ({"source": "person", "from": "Alex"}, "cards"),
                ({"source": "voice-note", "from": "Jordan"}, "cards"), ({"source": "agent", "from": "Robin"}, "cards"),
                ({"source": "routine", "from": "Robin"}, "cards"), ({"source": "bill-due", "from": "Robin"}, "bills"),
                ({"source": "agent", "from": "titan"}, "chat"), ({"source": "trigger:git"}, "cards"),
                ({"source": "device-health"}, "cards"), ({}, "chat")]
check("thread-id: the phone's NoticeThread, with a person's note as 'cards' (never from:<name>)",
      [(c_, aps(c_)["thread-id"]) for c_, _ in THREAD_CASES if aps(c_)["thread-id"] != _], [])
check("APNs priority: 10 for urgency 2-3, urgent or time-sensitive; 5 otherwise",
      [push.apns_priority(c_) for c_ in ({"urgency": 2}, {"urgency": 3}, {"urgency": 1, "key": "leave-ab12"},
                                          {"urgency": 1, "source": "lock-up"}, {"urgency": 1, "to": '["p_mir"]'},
                                          {"urgency": 0}, {"urgency": 1, "source": "routine"})],
      ["10", "10", "10", "10", "5", "5", "5"])
sent_prio = []
saved_post = push._curl_post
push._curl_post = lambda t, body, kind, prio, coll: sent_prio.append((body["aps"]["category"], prio)) or (200, "")
push._curl_send({"device_id": "dev-h", "role": "household"}, {"id": "c-h", "urgency": 1, "to": '["p_kitchen"]', "source": "routine"})
push._curl_send({"device_id": "dev-m", "role": "adult"}, {"id": "c-m", "urgency": 2, "to": "household", "source": "routine"})
push._curl_post = saved_post
check("the send uses the device's role for the category and the card for the priority", sent_prio,
      [("jb.card.safety", "5"), ("jb.card", "10")])
check("targets carry the device's role", sorted((t["device_id"], t["role"]) for t in push.targets({"id": "c-r", "urgency": 2, "to": "household"}, DEVICES)),
      [("dev-m", "adult"), ("dev-z", "admin")])

print("\n── across a person's devices (jellybean#204) ──")
q = push.payload({"id": "c12", "key": "dinner"}, passive=True)
check("busy elsewhere: the push lands quietly (passive, no sound)", (q["aps"].get("interruption-level"), "sound" in q["aps"]), ("passive", False))
check("…but Time to leave is never quiet", push.payload({"id": "c13", "key": "leave-dentist"}, passive=True)["aps"].get("interruption-level"),
      "time-sensitive")
push.note_active("zach", "dev-mac")
check("used the Mac just now: active elsewhere for the phone, not for the Mac itself",
      (push.active_elsewhere("zach", "dev-z"), push.active_elsewhere("zach", "dev-mac"), push.active_elsewhere("p_mir", "dev-m")), (True, False, False))
check("…two minutes later, not any more", push.active_elsewhere("zach", "dev-z", now=time.time() + push.ACTIVE_S + 1), False)
quiet = []
push.push_card({"id": "c14", "urgency": 3, "to": "household"}, devices=DEVICES,
               send=lambda t, card: quiet.append((t["device_id"], t["passive"])) or (200, ""))
check("a card push is quiet only for the person busy elsewhere", sorted(quiet), [("dev-m", False), ("dev-z", True)])
push.register("dev-z2", "9" * 64, "development", "com.zachshallbetter.jellybean.ios")
TWO = DEVICES + [{"id": "dev-z2", "person_id": "zach", "role": "admin"}]
con = push._db(); con.execute("DELETE FROM device_live"); con.commit(); con.close()
cleared = []
out = push.clear_card("zach", "c14", "dev-z", devices=TWO, send=lambda t, body: cleared.append((t["device_id"], body)) or (200, ""))
check("Done on one phone clears it on his other phone only, never Robin's",
      [d for d, _ in cleared], ["dev-z2"])
check("the clear push carries the card's id and nothing else", cleared[0][1], {"aps": {"content-available": 1}, "clear": "c14"})
push.mark_live("dev-z2")
check("an open app isn't pushed (its live cards drop it)", push.clear_card("zach", "c15", "dev-z", devices=TWO, send=lambda t, b: (200, "")), [])
check("not configured and no sender: nothing", push.clear_card("zach", "c16", None, devices=TWO), [])

print("\n── an expired card leaves the phones it was for, once ──")
import proactive as _px  # noqa: E402
EDB, PDB = _TMP / "expiry-push.db", _TMP / "expiry-proactive.db"
for d_, t_ in (("e-alex", "1"), ("e-alex2", "2"), ("e-robin", "3"), ("e-ava", "4"), ("e-tv", "5")):
    push.register(d_, t_ * 64, "development", "com.example.jellybean", EDB)
EDEV = [{"id": "e-alex", "person_id": "p_alex", "role": "adult"}, {"id": "e-alex2", "person_id": "p_alex", "role": "adult"},
        {"id": "e-robin", "person_id": "p_robin", "role": "adult"}, {"id": "e-ava", "person_id": "p_ava", "role": "kid"},
        {"id": "e-tv", "person_id": "p_tv", "role": "household"}]
t0 = time.time()
house_c = _px.add_card("Bins tonight", "made up", urgency=2, to="household", expires_in=60, db=PDB)
alex_c = _px.add_card("Plumber at 3", "made up", urgency=1, to=["p_alex"], expires_in=60, db=PDB)
calm_c = _px.add_card("Battery fine again", "made up", urgency=0, to=["p_alex"], expires_in=60, db=PDB)
later_c = _px.add_card("Still open", "made up", urgency=2, to="adults", expires_in=3600, db=PDB)
cleared_e = []
grab_e = lambda t, body: cleared_e.append((t["device_id"], body["clear"])) or (200, "")  # noqa: E731
check("nothing has expired yet: nothing cleared", push.clear_expired(t0 + 30, devices=EDEV, send=grab_e, db=EDB, proactive_db=PDB), [])
out_e = push.clear_expired(t0 + 90, devices=EDEV, send=grab_e, db=EDB, proactive_db=PDB)
check("expired: the household card leaves every phone and the shared screen; Alex's leaves both his phones",
      sorted(cleared_e), sorted([(d_, house_c["id"]) for d_ in ("e-alex", "e-alex2", "e-ava", "e-robin", "e-tv")]
                                + [(d_, alex_c["id"]) for d_ in ("e-alex", "e-alex2")]))
check("…the calm card (it never pushed) and the one still open: no clear", any(cid in (calm_c["id"], later_c["id"]) for _, cid in cleared_e),
      False)
check("…a line per device", len(out_e), 7)
n_e = len(cleared_e)
check("the next sweep clears nothing twice", (push.clear_expired(t0 + 110, devices=EDEV, send=grab_e, db=EDB, proactive_db=PDB),
                                              len(cleared_e)), ([], n_e))
check("…the memory forgets a card once it's out of the window", (house_c["id"] in push._expiry_cleared,
      push.clear_expired(t0 + 60 + push.EXPIRY_WINDOW_S + 5, devices=EDEV, send=grab_e, db=EDB, proactive_db=PDB),
      house_c["id"] in push._expiry_cleared, len(cleared_e)), (True, [], False, n_e))
check("not configured and no sender: nothing", push.clear_expired(t0 + 90, devices=EDEV, db=EDB, proactive_db=PDB), [])
check("an expired card's fields carry no words", sorted(_px.expired_between(t0, t0 + 90, db=PDB)[0]),
      ["expires_at", "from", "group", "id", "key", "kind", "source", "to", "urgency"])
api_src = (Path(__file__).parent / "agent_api_server.py").read_text()
check("the API's 20 s loop runs the sweep", "push.clear_expired" in api_src.split("async def _scheduled_runner", 1)[1].split("\n\n\n", 1)[0],
      True)

print("\n── Important cards off: muted, not unregistered (jellybean#786 row 1) ──")
ADB = _TMP / "alerts.db"
import sqlite3  # noqa: E402
old = sqlite3.connect(str(ADB))   # a push.db from before the column
old.execute("CREATE TABLE push_tokens (device_id TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL, "
            "bundle TEXT NOT NULL, updated_at REAL NOT NULL)")
old.execute("INSERT INTO push_tokens VALUES ('dev-old-phone', ?, 'development', 'com.example.jellybean', 0)", ("1" * 64,))
old.commit(); old.close()
check("an older push.db opens with alerts on for its rows", [(t["device_id"], t["card_alerts"]) for t in push._tokens(ADB)],
      [("dev-old-phone", True)])
B = "com.example.jellybean"
push.register("dev-on", "2" * 64, "development", B, ADB)                       # no flag: an older app
push.register("dev-muted", "3" * 64, "development", B, ADB, card_alerts=False)
push.register("dev-kid", "4" * 64, "development", B, ADB)
push.register("dev-kid-muted", "5" * 64, "development", B, ADB, card_alerts=False)
try:
    push.register("dev-x", "6" * 64, "development", B, ADB, card_alerts="no"); check("card_alerts must be a bool", False, True)
except ValueError:
    check("card_alerts must be a bool", True, True)
ALERTS = [{"id": "dev-on", "person_id": "p_alex", "role": "adult"}, {"id": "dev-muted", "person_id": "p_robin", "role": "adult"},
          {"id": "dev-kid", "person_id": "p_ava", "role": "kid"}, {"id": "dev-kid-muted", "person_id": "p_leo", "role": "kid"}]
aw = lambda card: sorted(t["device_id"] for t in push.targets(card, ALERTS, ADB))  # noqa: E731
check("a household card: every unmuted phone, the kid's included; never a muted one",
      aw({"id": "a1", "urgency": 2, "to": "household"}), ["dev-kid", "dev-on"])
check("addressed to the muted adult: nobody", aw({"id": "a2", "urgency": 2, "to": '["p_robin"]'}), [])
check("addressed to the kid: the kid's phone (the control: kids get what's theirs)",
      aw({"id": "a3", "urgency": 2, "to": '["p_ava"]'}), ["dev-kid"])
check("addressed to the muted kid: nobody", aw({"id": "a4", "urgency": 2, "to": '["p_leo"]'}), [])
for aud in ("admins", "adults"):
    check(f"an {aud} card never reaches a kid's phone, muted or not",
          [d for d in aw({"id": "a5", "urgency": 3, "to": aud}) if d.startswith("dev-kid")], [])
silent = []
grab = lambda t, body=None: silent.append(t["device_id"]) or (200, "")  # noqa: E731
push.nudge_inbox("dev-muted", send=grab, db=ADB)
push.nudge_sync("p_robin", devices=ALERTS, send=lambda t: grab(t), db=ADB)
push.clear_card("p_robin", "a2", devices=ALERTS, send=grab, db=ADB)
check("the muted phone still gets the #776 inbox wake, the sync nudge and the clear", silent, ["dev-muted"] * 3)
silent.clear()
push.nudge_inbox("dev-on", send=grab, db=ADB)
check("…as an unmuted one does (the control)", silent, ["dev-on"])
# Held while driving, then muted before the drive ends: the held alerts are dropped, never sent later.
import proactive as _pa  # noqa: E402
held_card = _pa.add_card("Washer's done", "made up", urgency=2, to="household")
push.register("dev-drive", "7" * 64, "development", B, ADB)
DRIVE = [{"id": "dev-drive", "person_id": "p_jordan", "role": "adult"}]
push.set_driving("dev-drive", True, db=ADB)
check("driving: an important card is held", push.push_card(held_card, devices=DRIVE, send=lambda t, c: (200, ""), db=ADB),
      ["dev-drive: held"])
push.register("dev-drive", "7" * 64, "development", B, ADB, card_alerts=False)
late = []
check("muted before the drive ended: nothing is sent when it does",
      (push.set_driving("dev-drive", False, send=lambda t, c: late.append(c["id"]) or (200, ""), db=ADB), late), ([], []))
check("…and nothing is left held to come later", push._db(ADB).execute("SELECT COUNT(*) FROM held_pushes").fetchone()[0], 0)
push.register("dev-drive", "7" * 64, "development", B, ADB, card_alerts=True)
push.set_driving("dev-drive", True, db=ADB)
push.push_card(held_card, devices=DRIVE, send=lambda t, c: (200, ""), db=ADB)
check("the control: unmuted, the held card goes when the drive ends",
      push.set_driving("dev-drive", False, send=lambda t, c: late.append(c["id"]) or (200, ""), db=ADB), [f"{held_card['id']}: 200"])

print("\n── the provider token (ES256) ──")
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
import jwt  # noqa: E402
key = ec.generate_private_key(ec.SECP256R1())
(_TMP / "AuthKey.p8").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
os.environ.update(APNS_KEY_FILE=str(_TMP / "AuthKey.p8"), APNS_KEY_ID="ABC123DEFG", APNS_TEAM_ID="PKH7G93GGD")
check("configured with a key file, id and team", push.configured(), True)
tok = push._jwt()
hdr = jwt.get_unverified_header(tok)
claims = jwt.decode(tok, key.public_key(), algorithms=["ES256"])
check("ES256, kid and team", (hdr["alg"], hdr["kid"], claims["iss"]), ("ES256", "ABC123DEFG", "PKH7G93GGD"))
check("reused, not re-signed each time", push._jwt(), tok)
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID"):
    os.environ.pop(k)

print("\n── the API and the hooks ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}
s = c.post("/v1/pair/start", json={"new_person": {"name": "Robin", "role": "adult"}}, headers=M).json()
claim = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": "Robin's iPhone", "platform": "ios"}).json()
MIR, dev = {"X-Agent-Token": claim["token"]}, claim["device"]["id"]
check("a paired phone registers: 204", c.post("/v1/devices/push", json={"token": "f" * 64, "environment": "development",
      "bundle": "com.zachshallbetter.jellybean.ios"}, headers=MIR).status_code, 204)
check("the master token can't (no device)", c.post("/v1/devices/push", json={"token": "f" * 64, "environment": "development",
      "bundle": "com.x.y"}, headers=M).status_code, 400)
check("a bad token → 400", c.post("/v1/devices/push", json={"token": "zz", "environment": "development", "bundle": "com.x.y"}, headers=MIR).status_code, 400)
check("card_alerts false is kept (jellybean#786 row 1): 204, token kept, muted",
      (c.post("/v1/devices/push", json={"token": "f" * 64, "environment": "development", "bundle": "com.zachshallbetter.jellybean.ios",
                                        "card_alerts": False}, headers=MIR).status_code,
       [t["card_alerts"] for t in push._tokens() if t["device_id"] == dev]), (204, [False]))
check("card_alerts must be a bool → 400", c.post("/v1/devices/push", json={"token": "f" * 64, "environment": "development",
      "bundle": "com.zachshallbetter.jellybean.ios", "card_alerts": "off"}, headers=MIR).status_code, 400)
check("missing (an older app): alerts on", (c.post("/v1/devices/push", json={"token": "f" * 64, "environment": "development",
      "bundle": "com.zachshallbetter.jellybean.ios"}, headers=MIR).status_code,
      [t["card_alerts"] for t in push._tokens() if t["device_id"] == dev]), (204, [True]))
check("DELETE /v1/devices/push turns push off, NOT the device", (c.delete("/v1/devices/push", headers=MIR).status_code,
      dev in [t["device_id"] for t in push._tokens()], c.get("/v1/me", headers=MIR).status_code), (204, False, 200))
c.post("/v1/devices/push", json={"token": "f" * 64, "environment": "development", "bundle": "com.zachshallbetter.jellybean.ios"}, headers=MIR)
c.get("/v1/cards/wait?since=0&timeout=1", headers=MIR)
check("the long-poll marks her phone live", [t["live"] for t in push._tokens() if t["device_id"] == dev], [True])
called = []
saved = push.push_card_async
push.push_card_async = lambda card: called.append(card["id"])
cd = proactive.add_card("Who's getting Leo?", "", to=[s["person"]["id"]], urgency=2)
check("creating a card hands it to push", called, [cd["id"]])
push.push_card_async = saved
s2 = c.post("/v1/pair/start", json={"person_id": s["person"]["id"]}, headers=M).json()
claim2 = c.post("/v1/pair/claim", json={"code": s2["code"], "device_name": "Robin's iPad", "platform": "ios"}).json()
MIR2, dev2 = {"X-Agent-Token": claim2["token"]}, claim2["device"]["id"]
clears = []
saved_clear = push.clear_card_async
push.clear_card_async = lambda person, cid, dev=None: clears.append((person, cid, dev))
check("Done on her phone: acked", c.post(f"/v1/proactive/{cd['id']}/ack", headers=MIR).json(), {"acked": True})
check("…and her other devices are told to clear it (not the phone that did it)", clears, [(s["person"]["id"], cd["id"], dev)])
check("…and she counts as busy on the phone for her iPad's pushes", push.active_elsewhere(s["person"]["id"], dev2), True)
check("a card that isn't hers clears nothing", (c.post("/v1/proactive/nope/ack", headers=MIR).json(), len(clears)), ({"acked": False}, 1))
push.clear_card_async = saved_clear
check("revoking the phone drops its token", (c.delete(f"/v1/devices/{dev}", headers=M).status_code,
      dev in [t["device_id"] for t in push._tokens()]), (200, False))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
