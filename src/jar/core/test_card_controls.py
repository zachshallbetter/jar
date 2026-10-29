#!/usr/bin/env python3
# gate: hermetic
"""Universal card controls (titan-node#1039): Later (a real snooze), Stop these (a mute), every card expiring, and
the payload's can_mute / approval / snoozed_from, all per person: one person's snooze or mute never touches anyone
else's cards. Safety cards can be snoozed, never muted; a shared household screen can't mute. A muted or snoozed
person gets no push; a snooze comes back once, pushed once to that person only. Fictional people; throwaway stores.
Run: python3 scripts/test_card_controls.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-cardctl-"))
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
import local_time  # noqa: E402
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

push.push_card_async = lambda card: None   # never a real push from a test
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Quarry", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Quarry", "adult", "Robin's iPhone")
AVA, ava = pair("Ava Quarry", "kid", "Ava's iPhone")
TV, _ = pair("Living room", "household", "Living room TV", platform="tvos")


def ids(h):
    return [x["id"] for x in c.get("/v1/proactive", headers=h).json()["cards"]]


def card(h, cid):
    return next((x for x in c.get("/v1/proactive", headers=h).json()["cards"] if x["id"] == cid), None)


print("── every card expires ──")
now = time.time()
for kind, want in (("info", 86400), ("update", 86400), ("calendar", 2 * 86400), ("suggestion", 3 * 86400),
                   ("report", 3 * 86400), ("reminder", 7 * 86400), ("alert", 7 * 86400)):
    x = P.add_card(f"k {kind}", kind=kind, to="adults")
    check(f"{kind} without an expiry: {want // 3600} h", round(x["expires_at"] - x["updated_at"]), want)
x = P.add_card("approve?", source="approve-cap", to="adults")
check("an approval source waits a week", round(x["expires_at"] - x["updated_at"]), 7 * 86400)
x = P.add_card("own", expires_in=600, to="adults")
check("a source's own expiry still wins", round(x["expires_at"] - x["updated_at"]), 600)

print("\n── the payload ──")
plain = P.add_card("Pick up the dry cleaning", kind="reminder", source="todos", to="adults")
lock = P.add_card("Lock up?", source="lock-up", to="adults")
cap = P.add_card("Set a limit", source="approve-cap", to="adults")
pc = card(ALEX, plain["id"])
check("a plain card: can_mute, not an approval, not snoozed", (pc["can_mute"], pc["approval"], pc["snoozed_from"]),
      (True, False, None))
check("a safety card can't be muted", card(ALEX, lock["id"])["can_mute"], False)
check("Lock Up and a limit card are approvals (Done = Not now)", (card(ALEX, lock["id"])["approval"],
                                                                  card(ALEX, cap["id"])["approval"]), (True, True))
hcard = P.add_card("Movie night?", to="household")
check("on a shared household screen nothing can be muted", card(TV, hcard["id"])["can_mute"], False)

print("\n── Later: per person ──")
until = time.time() + 3600
r = c.post(f"/v1/proactive/{plain['id']}/snooze", json={"until": until}, headers=ALEX)
check("Alex snoozes it for an hour", (r.status_code, round(r.json()["snoozed_until"])), (200, round(until)))
check("…hidden from Alex", plain["id"] in ids(ALEX), False)
check("…Robin still sees it", plain["id"] in ids(ROBIN), True)
check("…no push to Alex while it's snoozed; Robin still could", (P.hidden_for(alex, plain), P.hidden_for(robin, plain)),
      (True, False))
iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 7200))
check("ISO 8601 works too", c.post(f"/v1/proactive/{cap['id']}/snooze", json={"until": iso}, headers=ROBIN).status_code, 200)
for bad in (time.time() - 5, time.time() + 31 * 86400, "tomorrow", None, True):
    check(f"until {bad!r}: 400", c.post(f"/v1/proactive/{lock['id']}/snooze", json={"until": bad}, headers=ALEX).status_code,
          400)
check("a card the caller can't see: 404 (a kid on the adults' card)",
      c.post(f"/v1/proactive/{plain['id']}/snooze", json={"until": until}, headers=AVA).status_code, 404)
check("a safety card can be snoozed", c.post(f"/v1/proactive/{lock['id']}/snooze", json={"until": until},
                                             headers=ALEX).status_code, 200)
check("the household TV can snooze a household card", c.post(f"/v1/proactive/{hcard['id']}/snooze", json={"until": until},
                                                              headers=TV).status_code, 200)

print("\n── …and it comes back once ──")
before = time.time()
time.sleep(0.01)
con = P._db()
con.execute("UPDATE card_snoozes SET until=? WHERE card_id=? AND person_id=?", (time.time() - 1, plain["id"], alex))
con.commit()
con.close()
back = card(ALEX, plain["id"])
check("due: back for Alex, marked snoozed_from today", (back is not None, back and back["snoozed_from"]),
      (True, local_time.day(time.time())))   # the household's day, not UTC's (titan is UTC)
check("…the long-poll's `since` sees it as a change", plain["id"] in [x["id"] for x in P.list_cards(before - 5, person=alex,
                                                                                                    role="adult")], True)
check("…and last_change moves", P.last_change() >= before - 1, True)
pushed = []
check("pushed once, to Alex's own devices only", (P.wake_snoozed(push_fn=pushed.append), [json.loads(p["to"]) for p in pushed]),
      (1, [[alex]]))
check("…never twice", P.wake_snoozed(push_fn=pushed.append), 0)
check("Robin never saw a snoozed_from", card(ROBIN, plain["id"])["snoozed_from"], None)

print("\n── Stop these: per person, by key family or source + kind ──")
b1 = P.add_card("Lumen Power is due Friday", source="bill-due", key="bill-due:lumen-2026-10", to="adults")
r = c.post(f"/v1/proactive/{b1['id']}/mute", headers=ALEX)
check("Alex mutes a keyed card: its key family", (r.status_code, r.json()["mute"]["key_family"], r.json()["mute"]["source"]),
      (200, "bill-due", ""))
check("…this card is dismissed for Alex, not for Robin", (b1["id"] in ids(ALEX), b1["id"] in ids(ROBIN)), (False, True))
b2 = P.add_card("Fern Water is due", source="bill-due", key="bill-due:fern-2026-11", to="adults")
check("a new card in the family: hidden from Alex, shown to Robin", (b2["id"] in ids(ALEX), b2["id"] in ids(ROBIN)),
      (False, True))
check("…and never pushed to Alex", (P.hidden_for(alex, b2), P.hidden_for(robin, b2)), (True, False))
s1 = P.add_card("Try the new bakery", kind="suggestion", source="places", to="adults")
r = c.post(f"/v1/proactive/{s1['id']}/mute", headers=ROBIN)
check("Robin mutes an unkeyed card: its source + kind", (r.json()["mute"]["source"], r.json()["mute"]["kind"],
                                                        r.json()["mute"]["key_family"]), ("places", "suggestion", ""))
s2 = P.add_card("A park nearby", kind="suggestion", source="places", to="adults")
s3 = P.add_card("You're near home", kind="info", source="places", to="adults")
check("…another places suggestion: hidden from Robin only; a places info card still shows",
      (s2["id"] in ids(ROBIN), s2["id"] in ids(ALEX), s3["id"] in ids(ROBIN)), (False, True, True))
mine = c.get("/v1/proactive/mutes", headers=ALEX).json()["mutes"]
check("GET mutes: the caller's own only", ([m["key_family"] for m in mine],
                                           [m["source"] for m in c.get("/v1/proactive/mutes", headers=ROBIN).json()["mutes"]]),
      (["bill-due"], ["places"]))
check("Robin can't remove Alex's mute", c.delete(f"/v1/proactive/mutes?id={mine[0]['id']}", headers=ROBIN).json(),
      {"removed": False})
check("Alex removes it: the family shows again", (c.delete(f"/v1/proactive/mutes?id={mine[0]['id']}", headers=ALEX).json(),
                                                  b2["id"] in ids(ALEX)), ({"removed": True}, True))
r = c.post(f"/v1/proactive/{lock['id']}/mute", headers=ROBIN)
check("a safety card: 403 cannot_mute", (r.status_code, r.json()), (403, {"error": "cannot_mute"}))
check("the household TV: 403 cannot_mute", c.post(f"/v1/proactive/{hcard['id']}/mute", headers=TV).status_code, 403)
check("a card the caller can't see: 404", c.post(f"/v1/proactive/{s2['id']}/mute", headers=AVA).status_code, 404)
con = P._db()
fb = con.execute("SELECT person_id, source, key_family, signal FROM card_feedback WHERE signal='mute' ORDER BY at").fetchall()
con.close()
check("each mute is recorded as a negative example for triage", [(r_[0] == alex or r_[0] == robin, r_[3]) for r_ in fb],
      [(True, "mute"), (True, "mute")])

print("\n── key families ──")
for k, fam in (("bill-due:lumen-2026-10", "bill-due"), ("coordination-2026-10-05", "coordination"),
               ("device-health:mac-5f3a2c", "device-health"), ("next-stop", "next-stop"), (None, ""),
               ("trip-12", "trip")):
    check(f"{k!r} → {fam!r}", P.key_family(k), fam)

print("\n── push leaves a muted or snoozing person out ──")
devices = [{"id": "d-alex", "person_id": alex, "role": "adult"}, {"id": "d-robin", "person_id": robin, "role": "adult"}]
push._tokens = lambda db=None: [{"device_id": "d-alex", "live": False}, {"device_id": "d-robin", "live": False}]
check("a card in Alex's muted family (re-muted): only Robin's phone", (
    c.post(f"/v1/proactive/{b2['id']}/mute", headers=ALEX).status_code,
    [t["device_id"] for t in push.targets({**b2, "to": "adults"}, devices)]), (200, ["d-robin"]))

print("\n── safety sources match what the producers really send ──")
import re  # noqa: E402
HERE = Path(__file__).parent
# A producer of physical-safety cards: every card source it sends must be unmutable (#413/#927/#1013: Lock Up).
SAFETY_PRODUCERS = ("home_security.py",)
for f in SAFETY_PRODUCERS:
    sent = set(re.findall(r'add_card\([^)]*?source="([a-z:_-]+)"', (HERE / f).read_text(), re.S))
    check(f"{f}: it sends a card source, and every one is in SAFETY_SOURCES", (bool(sent), sorted(sent - P.SAFETY_SOURCES)),
          (True, []))
# Any producer whose source names a lock, alarm, door, garage, camera or security is a safety source, unless listed here.
NOT_SAFETY = {"tv-unlock": "unlocks a household TV's own content for an adult; nothing physical"}
named = set()
for f in HERE.glob("*.py"):
    if f.name.startswith("test_"):
        continue
    named |= set(re.findall(r'source="([a-z:_-]*(?:lock|alarm|door|garage|camera|security)[a-z:_-]*)"', f.read_text()))
check("every lock/alarm/door/garage/camera/security source is a safety source or explained",
      sorted(n for n in named if n not in P.SAFETY_SOURCES and n not in NOT_SAFETY), [])
check("Lock Up's real source can't be muted", P.can_mute({"source": "lock-up"}), False)

print("\n── eval turns ──")
import agent_runtime_lib as lib  # noqa: E402
src = Path(lib.__file__).read_text()
check("no agent tool snoozes or mutes a card (app endpoints only; a future tool must be eval-simulated)",
      ("proactive.snooze(" in src, "proactive.mute(" in src, [n for n in lib._TOOL_REGISTRY if "snooze" in n or "mute_card" in n]),
      (False, False, []))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
