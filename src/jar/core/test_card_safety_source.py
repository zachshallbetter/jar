#!/usr/bin/env python3
# gate: hermetic   (temp stores; APNs, Gemini and ntfy off; the real POST /v1/cards route via TestClient)
"""titan-node#1289 review: a safety source (Lock Up, alarms) or a "leave-" key makes a push time-sensitive and
unmutable, through Focus. Only a device-less caller (the master token, Home Assistant; titan's own jobs in-process)
may set one; a paired device's (an adult's, a kid's, a shared screen's) is stored as an ordinary card. Safety is an
exact match, the same for the push and the server's can-mute. Fictional people.
Run: python3 scripts/test_card_safety_source.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-safetysrc-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "SPACES_DB": "spaces.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db",
             "SITTER_DB": "sitter.db"}.items():
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


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import proactive as P  # noqa: E402
import push  # noqa: E402

push.push_card_async = lambda card: None
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


ALEX, alex = pair("Alex Slate", "adult", "Alex's iPhone")
ALEX_MAC, _ = pair(None, None, "Alex's Mac", platform="mac", person_id=alex)
ROBIN, robin = pair("Robin Slate", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Slate", "kid", "Ava's iPhone")
TV, _tv = pair("Living room", "household", "Living room TV", platform="tvos")


def devs(h):
    return {d["name"]: d for d in c.get("/v1/proactive/delivery", headers=h).json()["devices"]}


def card(h, cid):
    return next((x for x in c.get("/v1/proactive", headers=h).json()["cards"] if x["id"] == cid), None)



print("── POST /v1/cards: only a device-less caller can make a safety card or a time-to-leave card ──")


def post(h, **kw):
    r = c.post("/v1/cards", json={"title": "Check the stove", "to": "adults", "urgency": 1, **kw}, headers=h)
    return r.status_code, r.json().get("card") or r.json()


def stored(cid):
    import sqlite3
    con = sqlite3.connect(os.environ["PROACTIVE_DB"])
    try:
        return con.execute("SELECT source, card_key FROM cards WHERE id=?", (cid,)).fetchone()
    finally:
        con.close()


def aps_of(cid, role="adult"):
    s, k = stored(cid)
    return push.payload({"id": cid, "source": s, "key": k, "urgency": 1}, role=role)["aps"]


for who, h in (("an adult's phone", ALEX), ("a kid's phone (to the adults)", AVA), ("a shared screen", TV)):
    code, out = post(h, source="alarm")
    a = aps_of(out["id"])
    check(f"{who} sets source 'alarm': stored as an ordinary card, 'device:alarm' (shown as 'alarm')",
          (code, stored(out["id"])[0], push.plain_source(stored(out["id"])[0])), (200, "device:alarm", "alarm"))
    check(f"…no safety category, not time-sensitive, and it can be muted",
          (a["category"], a["interruption-level"], P.can_mute({"source": stored(out["id"])[0]}, "adult")),
          ("jb.card", "active", True))
for who, h in (("an adult's phone", ALEX), ("a kid's phone", AVA), ("a shared screen", TV)):
    code, out = post(h, key="leave-dentist")
    check(f"{who} sets key 'leave-…': stored as 'device-leave-…', not time-sensitive",
          (stored(out["id"])[1], aps_of(out["id"])["interruption-level"]), ("device-leave-dentist", "active"))
check("every safety source from a device is renamed", [stored(post(ALEX, source=s_)[1]["id"])[0]
      for s_ in sorted(P.SAFETY_SOURCES)], ["device:" + s_ for s_ in sorted(P.SAFETY_SOURCES)])
check("any case or spacing a device writes is caught, as the phone matches it (jellybean#1056: trimmed, lowercased)",
      [stored(post(ALEX, source=s_)[1]["id"])[0] for s_ in ("Alarm", " LOCK-UP ", "Security")],
      ["device:Alarm", "device:LOCK-UP", "device:Security"])
check("…and 'Leave-…' keys", stored(post(ALEX, key="Leave-dentist")[1]["id"])[1], "device-Leave-dentist")
check("push.is_safety reads a source as the phone does (trimmed, lowercased)",
      [push.is_safety({"source": s_}) for s_ in ("Alarm", " lock-up ", "device:Alarm")], [True, True, False])
check("a prefixed safety word from a device ('kid:alarm') is ordinary as it stands",
      (stored(post(AVA, source="kid:alarm")[1]["id"])[0], aps_of(post(AVA, source="kid:alarm")[1]["id"])["category"]),
      ("kid:alarm", "jb.card"))

print("\n── the device-less callers keep them ──")
code, out = post(M, source="alarm")
a = aps_of(out["id"])
check("the master token's 'alarm': a safety card, time-sensitive, can't be muted",
      (stored(out["id"])[0], a["category"], a["interruption-level"], P.can_mute({"source": "alarm"}, "adult")),
      ("alarm", "jb.card.safety", "time-sensitive", False))
code, out = post(M, key="leave-school")
check("the master token's 'leave-…': kept, time-sensitive", (stored(out["id"])[1], aps_of(out["id"])["interruption-level"]),
      ("leave-school", "time-sensitive"))
lk = P.add_card("Lock up?", source="lock-up", to="adults", urgency=2)
check("titan's own job (in-process add_card) keeps 'lock-up' as safety", push.is_safety(lk), True)
check("an ordinary source from a device is untouched", stored(post(ALEX, source="places")[1]["id"])[0], "places")

print("\n── one rule everywhere: proactive.is_safety_source (trimmed, lowercased, exact) ──")
code, out = post(M, source="Alarm")
check("'Alarm' from the master token: a safety card on the push AND on the server (can't mute, can't stop)",
      (stored(out["id"])[0], push.is_safety({"source": "Alarm"}), P.can_mute({"source": "Alarm"}, "adult"),
       P.is_safety_source(" Lock-Up ")), ("Alarm", True, False, True))
qp = P.add_card("Smoke alarm", source="Alarm", to="adults", urgency=1)
srcs = {s["source"]: s for s in c.get("/v1/proactive/sources", headers=ALEX).json()["sources"]}
check("…its source can't be stopped in the settings list", srcs.get("Alarm", {}).get("can_stop"), False)
r = c.put("/v1/proactive/sources", json={"source": "Alarm", "mode": "stop"}, headers=ALEX)
check("…and a stop of it is refused", r.status_code >= 400, True)

print("\n── one rule on both sides: exact match ──")
for s_ in ("trigger:alarm", "x:security", "device:lock-up"):
    check(f"'{s_}': not safety for the push, and mutable on the server (they agree)",
          (push.is_safety({"source": s_}), P.can_mute({"source": s_}, "adult")), (False, True))

print("\n── notify_user's source for 'admins' ──")
import agent_runtime_lib as lib  # noqa: E402
io = lib._TURN_IO
io.person, io.role = {"id": ava, "name": "Ava"}, "kid"
check("a kid's note to the grown-ups in charge ('admins'): 'note'", lib._notify_source("admins", "Ava"), "note")
io.person, io.role = {"id": "zach", "name": "Admin"}, "admin"
check("the admin's own note to 'admins' is to themself: 'agent'", lib._notify_source("admins", "Admin"), "agent")
io.person = io.role = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
