#!/usr/bin/env python3
# gate: hermetic
"""Unlock the TV with your phone (jellybean#427): a household TV asks, the adults at home confirm on their phones, the
first Unlock wins; the TV then gets only that adult's own things, read-only, while it lasts; it ends on expiry, the
TV's Lock (a kid's profile too), the phone's Lock now, the adult leaving home, or a new unlock; every unlock is
audited. Throwaway stores; made-up people; the calendar and bills reads are stubbed (no record runtime).
Run: python3 scripts/test_tv_unlock.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-tvunlock-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "UPLOADS_DB": "uploads.db", "UPLOADS_DIR": "uploads",
             "TV_UNLOCK_DB": "tv_unlock.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ["PLACES_JITTER_S"] = "0"   # the departure locks at once here; the 5-minute hold is test_places_order.py's
for k in ("APNS_KEY_FILE", "APNS_KEY_ID", "APNS_TEAM_ID", "GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
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
import household_identity as hh  # noqa: E402
import proactive  # noqa: E402
import tv_unlock  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")
IPAD, _ = pair("Kitchen", "household", "Kitchen iPad")

# Each adult's own things, as the layer would read them (the real reads need the record runtime).
CAL = {robin: [{"id": "e1", "title": "Dentist", "start": "2026-10-03T15:00", "end": "2026-10-03T16:00", "all_day": False}],
       alex: [{"id": "e2", "title": "Alex's surprise planning", "start": "2026-10-03T18:00", "all_day": False}]}
BILLS = {robin: [{"title": "Maple Animal Clinic", "amount": 84.2, "currency": "USD", "due": "2026-10-14"}],
         alex: [{"title": "Alex's card", "amount": 12, "due": "2026-10-09"}]}
tv_unlock._calendar = lambda pid, role: CAL.get(pid, [])
tv_unlock._bills = lambda pid: BILLS.get(pid, [])

proactive.add_card("Robin's renewal", "Passport renewal is due next month.", source="agent", to=[robin])
proactive.add_card("Alex's private note", "Gift idea for Robin.", source="agent", to=[alex])
proactive.add_card("Garbage night", "Bins out by 8.", source="calendar", to="household")


def place(h, event, pid, n=[0]):
    n[0] += 1
    return c.post("/v1/presence/place", json={"id": f"pe-{pid}-{n[0]}", "event": event, "place": "home",
                                              "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, headers=h).status_code


print("── asking ──")
check("Robin's phone says she's home", place(ROBIN, "arrived", robin), 204)
r = c.post("/v1/tv-unlocks/request", json={"minutes": 30}, headers=TV)
check("the TV asks", (r.status_code, r.json().get("asked")), (202, 1))
rid = r.json()["request_id"]
card = next(x for x in proactive.list_cards(person=robin, role="adult") if (x.get("data") or {}).get("request_id") == rid)
check("…Robin (the adult at home) gets the card", (card["source"], card["data"]["kind"], card["data"]["device_name"]),
      ("tv-unlock", "tv_unlock", "Living room TV"))
check("…capped at 10 minutes", card["data"]["minutes"], 10)
check("…with Unlock and Not now", [a["id"] for a in card["actions"]], ["unlock", "decline"])
check("Alex (not home) doesn't", any((x.get("data") or {}).get("request_id") == rid
                                     for x in proactive.list_cards(person=alex, role="adult")), False)
check("the TV sees it pending", c.get("/v1/tv-unlocks/self", headers=TV).json()["state"], "pending")
check("a household iPad can't ask", c.post("/v1/tv-unlocks/request", headers=IPAD).status_code, 403)
check("a person can't ask", c.post("/v1/tv-unlocks/request", headers=ALEX).status_code, 403)
check("the layer is locked", c.get("/v1/tv-unlocks/self/layer", headers=TV).status_code, 403)

print("── confirming ──")
check("a kid can't unlock", tv_unlock.answer(f"unlock:{rid}", ava, "kid")[0], False)
check("an adult who wasn't asked can't", tv_unlock.answer(f"unlock:{rid}", alex, "adult")[0], False)
r = c.post(f"/v1/proactive/{card['id']}/action", json={"action_id": "unlock"}, headers=ROBIN)
check("Robin taps Unlock", (r.json().get("ok"), r.json().get("said")), (True, "Unlocked the Living room TV for 10 minutes."))
s = c.get("/v1/tv-unlocks/self", headers=TV).json()
check("the TV: active, Robin", (s["state"], s["person"]["first_name"]), ("active", "Robin"))
check("…for 10 minutes", 590 < s["expires_at"] - time.time() <= 600, True)
on = [x for x in proactive.list_cards(person=robin, role="adult") if (x.get("data") or {}).get("kind") == "tv_unlock_active"]
check("Robin gets Lock now", [a["id"] for a in on[0]["actions"]] if on else None, ["lock"])
check("the ask's card retired", any(x["id"] == card["id"] for x in proactive.list_cards(person=robin, role="adult")), False)

print("── the layer ──")
L = c.get("/v1/tv-unlocks/self/layer", headers=TV).json()
check("Robin's calendar", [e["title"] for e in L["calendar"]], ["Dentist"])
check("Robin's bills", [b["title"] for b in L["bills"]], ["Maple Animal Clinic"])
check("Robin's own card, read-only", [(x["title"], "actions" in x) for x in L["cards"]], [("Robin's renewal", False)])
blob = json.dumps(L)
check("nothing of Alex's", any(w in blob for w in ("Alex's surprise", "Alex's card", "Alex's private")), False)
check("no household cards (the TV has those)", "Garbage night" in blob, False)
check("Alex can't lock Robin's", c.delete(f"/v1/tv-unlocks/{on[0]['data']['unlock_id']}", headers=ALEX).status_code, 404)

print("── a kid's profile ends it ──")
r = c.request("DELETE", "/v1/tv-unlocks/self", json={"reason": "kid_profile"}, headers=TV)
check("the TV locks for a kid", r.json(), {"ended": 1})
s = c.get("/v1/tv-unlocks/self", headers=TV).json()
check("…none, and why", (s["state"], s.get("ended", {}).get("reason")), ("none", "kid_profile"))
r = c.get("/v1/tv-unlocks/self/layer", headers=TV)
check("…and the layer is gone", (r.status_code, "Robin" in r.text, "Dentist" in r.text), (403, False, False))
check("Lock now retired with it", any((x.get("data") or {}).get("kind") == "tv_unlock_active"
                                      for x in proactive.list_cards(person=robin, role="adult")), False)


def unlock(minutes=5, now=None, who=robin):
    rid = c.post("/v1/tv-unlocks/request", json={"minutes": minutes}, headers=TV).json()["request_id"]
    ok, _ = tv_unlock.answer(f"unlock:{rid}", who, "adult", now=now)
    return ok


print("── walking away ──")
check("unlocked again", unlock(), True)
check("Robin leaves home", place(ROBIN, "left", robin), 204)
s = c.get("/v1/tv-unlocks/self", headers=TV).json()
check("…it locks: left_home", (s["state"], s.get("ended", {}).get("reason")), ("none", "left_home"))
check("…no layer", c.get("/v1/tv-unlocks/self/layer", headers=TV).status_code, 403)

print("── the phone's Lock now ──")
place(ROBIN, "arrived", robin)
check("unlocked again", unlock(), True)
uid = tv_unlock.audit(1)[0]["id"]
check("Robin locks it from her phone", c.delete(f"/v1/tv-unlocks/{uid}", headers=ROBIN).json(), {"ended": True})
check("…lock_phone", c.get("/v1/tv-unlocks/self", headers=TV).json().get("ended", {}).get("reason"), "lock_phone")

print("── time runs out ──")
t0 = time.time()
check("unlocked for 1 minute", unlock(minutes=1, now=t0), True)
check("still on at 59 s", tv_unlock.state(tv_unlock.audit(1)[0]["device_id"], now=t0 + 59)["state"], "active")
dev = tv_unlock.audit(1)[0]["device_id"]
s = tv_unlock.state(dev, now=t0 + 61)
check("over at 61 s: expired", (s["state"], s.get("ended", {}).get("reason")), ("none", "expired"))
check("…no layer", tv_unlock.layer(dev, now=t0 + 62), None)

print("── one adult at a time ──")
place(ALEX, "arrived", alex)
check("Robin unlocks", unlock(), True)
check("then Alex does", unlock(who=alex), True)
rows = tv_unlock.audit(1)
check("Robin's ended: replaced", next(r["end_reason"] for r in rows if r["person_id"] == robin and r["ended_at"]
                                      and r["started_at"] >= rows[1]["started_at"]), "replaced")
L = c.get("/v1/tv-unlocks/self/layer", headers=TV).json()
check("the layer is Alex's alone now", (L["person"]["id"], "Dentist" in json.dumps(L), "Maple" in json.dumps(L)), (alex, False, False))
c.request("DELETE", "/v1/tv-unlocks/self", headers=TV)

print("── an old ask ──")
rid = c.post("/v1/tv-unlocks/request", headers=TV).json()["request_id"]
check("Unlock after 90 s: refused", tv_unlock.answer(f"unlock:{rid}", robin, "adult", now=time.time() + 91)[0], False)

print("── the audit ──")
a = c.get("/v1/tv-unlocks", headers=M).json()["unlocks"]
check("every unlock is there, with why it ended", sorted({r["end_reason"] for r in a if r["end_reason"]}),
      sorted({"kid_profile", "left_home", "lock_phone", "expired", "replaced", "lock_tv"}))
check("…admin only", c.get("/v1/tv-unlocks", headers=ROBIN).status_code, 403)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
