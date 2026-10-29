#!/usr/bin/env python3
# gate: hermetic
"""Continue on another room's screen (jellybean#433 slice 1): rooms come from the room each household device set for
itself; a person puts one of their replies on the screens in the room they name; only those screens get it, for 10
minutes; the screen's Done or the sender's take-back ends it and the text is gone. Nobody is tracked. Throwaway
stores; made-up people.
Run: python3 scripts/test_handoff.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-handoff-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "TASK_LOG_DB": "task_log.db", "HANDOFF_DB": "handoff.db", "FENCES_DB": "fences.db"}.items():
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
import handoff  # noqa: E402
import household_identity as hh  # noqa: E402
import room_state  # noqa: E402
room_state.hold = lambda room, **kw: None   # slice 2's hold has its own test (test_room_state.py); not the clock here

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios", room=None):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    h = {"X-Agent-Token": cl["token"]}
    if room:
        c.patch("/v1/devices/self", json={"room": room}, headers=h)
    return h, s["person"]["id"]


ALEX, alex = pair("Alex Hale", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Hale", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Hale", "kid", "Ava's iPhone")
LIVING, _ = pair("Living room", "household", "Living room TV", "tvos", room="Living room")
KITCHEN, _ = pair("Kitchen", "household", "Kitchen iPad", room="Kitchen")
DEN, _ = pair("Den", "household", "Den TV", "tvos")          # never named its room

print("── rooms ──")
r = c.get("/v1/handoff/rooms", headers=ALEX).json()["rooms"]
check("the rooms with a screen, by their own names", r, [{"room": "Kitchen", "screens": 1}, {"room": "Living room", "screens": 1}])
check("a kid sees them too", c.get("/v1/handoff/rooms", headers=AVA).status_code, 200)

print("── sending ──")
reply = "Dinner plan: tacos on Tuesday, pasta Wednesday. Leo has practice at 5."
r = c.post("/v1/handoff", json={"to_room": "kitchen", "kind": "reply", "text": reply, "title": "This week's dinners"}, headers=ALEX)
check("Alex continues on the kitchen screen", (r.status_code, r.json().get("room"), r.json().get("screens")),
      (201, "Kitchen", ["Kitchen iPad"]))
hid = r.json()["id"]
check("…for 10 minutes", 595 < r.json()["expires_at"] - time.time() <= 600, True)
k = c.get("/v1/handoff/self", headers=KITCHEN).json()["handoffs"]
check("the kitchen screen has it, from Alex", [(h["from"], h["title"], h["text"]) for h in k], [("Alex", "This week's dinners", reply)])
check("the living room doesn't", c.get("/v1/handoff/self", headers=LIVING).json()["handoffs"], [])
check("nor a screen with no room", c.get("/v1/handoff/self", headers=DEN).json()["handoffs"], [])
check("a person can't read a screen's", c.get("/v1/handoff/self", headers=ROBIN).status_code, 403)
r = c.post("/v1/handoff", json={"to_room": "Garage", "text": "x"}, headers=ALEX)
check("a room with no screen: 404 and the ones there are", (r.status_code, r.json().get("rooms")), (404, ["Kitchen", "Living room"]))
check("no text: 400", c.post("/v1/handoff", json={"to_room": "Kitchen"}, headers=ALEX).status_code, 400)
check("another kind: 400", c.post("/v1/handoff", json={"to_room": "Kitchen", "kind": "media", "text": "x"}, headers=ALEX).status_code, 400)
check("a shared screen doesn't send", c.post("/v1/handoff", json={"to_room": "Kitchen", "text": "x"}, headers=LIVING).status_code, 403)
check("a kid sends their own", c.post("/v1/handoff", json={"to_room": "Living room", "text": "My chores are done!"},
                                      headers=AVA).status_code, 201)
check("Alex sees his own live one", [h["id"] for h in c.get("/v1/handoff", headers=ALEX).json()["handoffs"]], [hid])

print("── ending ──")
check("Robin can't take back Alex's", c.delete(f"/v1/handoff/{hid}", headers=ROBIN).status_code, 404)
check("the living room can't end the kitchen's", c.delete(f"/v1/handoff/{hid}", headers=LIVING).status_code, 404)
check("the kitchen's Done ends it", c.delete(f"/v1/handoff/{hid}", headers=KITCHEN).json(), {"ended": True})
check("…gone from the screen", c.get("/v1/handoff/self", headers=KITCHEN).json()["handoffs"], [])
rows = sqlite3.connect(os.environ["HANDOFF_DB"]).execute("SELECT text FROM handoffs WHERE id=?", (hid,)).fetchall()
check("…and its text is gone from titan", rows, [])
h2 = c.post("/v1/handoff", json={"to_room": "Kitchen", "text": "The vet is at 4."}, headers=ALEX).json()["id"]
check("Alex takes one back", c.delete(f"/v1/handoff/{h2}", headers=ALEX).json(), {"ended": True})
check("…gone", c.get("/v1/handoff/self", headers=KITCHEN).json()["handoffs"], [])

print("── time runs out ──")
t0 = time.time()
code, out = handoff.send(alex, "adult", {"to_room": "Kitchen", "text": "Back by 6."}, st.people(), now=t0)
dev = next(d["id"] for p in st.people() for d in p["devices"] if d["name"] == "Kitchen iPad")
check("there at 9 minutes", len(handoff.for_device(dev, now=t0 + 540)), 1)
check("gone at 10", handoff.for_device(dev, now=t0 + 601), [])
check("…text and all", sqlite3.connect(os.environ["HANDOFF_DB"]).execute("SELECT COUNT(*) FROM handoffs WHERE id=?",
                                                                       (out["id"],)).fetchone()[0], 0)

print("── fences: a room's screens are a household audience (titan-node#917) ──")
import fences  # noqa: E402
os.environ["AGENT_FENCES"] = "1"
bill = "Your electric bill is $142.18, due Friday."
r = c.post("/v1/handoff", json={"to_room": "Living room", "text": bill, "anyway": True}, headers=ALEX)
check("money to a household room: 403, held by a fence, with the reason and no way around it (anyway or not)",
      (r.status_code, r.json().get("held"), r.json().get("why")),
      (403, "fence", "This mentions money, which is for the adults only."))
check("…nothing stored for that room's screen", any(h["text"] == bill for h in handoff.for_device(
      next(d["id"] for p in st.people() for d in p.get("devices") or [] if d.get("room") == "Living room"))), False)
held = fences.get().con.execute("SELECT COALESCE(SUM(n), 0) FROM held").fetchone()[0]
check("…counted as held back (a count, never the text)", held >= 1, True)
check("a neutral reply still goes", c.post("/v1/handoff", json={"to_room": "Living room", "text": "Tacos tonight."},
                                           headers=ALEX).status_code, 201)
fences.get().set_entity("household:dr-quinn", ["Dr. Quinn"], robin, "private", reason="test")
r = c.post("/v1/handoff", json={"to_room": "Kitchen", "text": "Dr. Quinn moved the appointment to 3."}, headers=ALEX)
check("an entity fence Robin set, handed by Alex to a room: held",
      (r.status_code, r.json().get("why")), (403, "This mentions something kept between some people."))
r = c.post("/v1/handoff", json={"to_room": "Kitchen", "text": "Dr. Quinn moved the appointment to 3."}, headers=ROBIN)
check("…and by Robin too: a shared screen is outside her private fence (she shares it explicitly, as anywhere)",
      r.status_code, 403)
os.environ["AGENT_FENCES"] = "0"
check("fences off: delivered as before", c.post("/v1/handoff", json={"to_room": "Living room", "text": bill},
                                                headers=ALEX).status_code, 201)
check("fence_why says the kind, never the text", handoff.fence_why([{"kind": "class", "subject": "health"}]),
      "This mentions health, which stays with the person it's about.")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
