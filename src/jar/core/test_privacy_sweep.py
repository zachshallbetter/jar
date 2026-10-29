#!/usr/bin/env python3
# gate: hermetic
"""The privacy sweep's missing cells (titan-node#1055): one person's item must not reach another adult, a kid or a
shared household screen through each read path that had no two-person test.
  - cards: pressing a button on a card that isn't yours (POST /v1/proactive/{id}/action) does nothing;
  - uploads over HTTP (GET /v1/files/uploads/{id}): only the sender and the people they named;
  - the Activity Letter (reflection.gather): another person's card and another person's memory facts stay out.
Fictional people (Alex, Robin, Jordan, Ava) and a TV; throwaway stores; nothing live.
Run: python3 scripts/test_privacy_sweep.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-privsweep-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "SPACES_DB": "spaces.db", "UPLOADS_DIR": "uploads", "UPLOADS_DB": "uploads.db", "LOCK_UP_DB": "lock_up.db",
             "TV_UNLOCK_DB": "tv_unlock.db", "SITTER_DB": "sitter.db", "FENCES_DB": "fences.db",
             "CLAUDE_CHATS_DB": "claude_chats.db"}.items():
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


def pair(name, role, device, platform="ios"):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    cl = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": device, "platform": platform}).json()
    return {"X-Agent-Token": cl["token"]}, s["person"]["id"]


ALEX, alex = pair("Alex Pike", "adult", "Alex's iPhone")
ROBIN, robin = pair("Robin Pike", "adult", "Robin's iPhone")
JORDAN, jordan = pair("Jordan Pike", "adult", "Jordan's iPhone")
AVA, ava = pair("Ava Pike", "kid", "Ava's iPhone")
TV, _tv = pair("Living room", "household", "Living room TV", platform="tvos")
OTHERS = (("another adult", JORDAN), ("a kid", AVA), ("the household TV", TV))

print("── cards: a button on someone else's card does nothing ──")
fired = []
mine = P.add_card("Pick a time for the plumber", source="tasks", to=[robin],
                  actions=[{"id": "go", "label": "Tomorrow 9", "kind": "reply", "value": "plumber:tomorrow"}])
for who, h in OTHERS:
    r = c.post(f"/v1/proactive/{mine['id']}/action", json={"action_id": "go"}, headers=h)
    check(f"{who}: refused (ok false, no such card)", (r.status_code, r.json().get("ok"), r.json().get("error")),
          (200, False, "no such card"))
check("…and Robin's card is untouched (not acked for her)", next(x for x in c.get("/v1/proactive", headers=ROBIN).json()["cards"]
                                                                 if x["id"] == mine["id"])["acked_at"], None)
check("…no trigger was queued from those presses", P.recent_triggers(20), [])

print("\n── uploads over HTTP: the sender and the people they named ──")
import uploads  # noqa: E402
st, own = uploads.save(alex, "note.txt", b"Alex's private note about the surprise party.")
check("Alex's file is saved", st, 201)
check("Alex gets it back", c.get(f"/v1/files/uploads/{own['id']}", headers=ALEX).status_code, 200)
for who, h in (("Robin (another adult)", ROBIN),) + OTHERS:
    check(f"{who}: 404", c.get(f"/v1/files/uploads/{own['id']}", headers=h).status_code, 404)
st, named = uploads.save(alex, "list.txt", b"Shopping list for Robin.", audience=[robin])
check("named for Robin: Robin gets it", c.get(f"/v1/files/uploads/{named['id']}", headers=ROBIN).status_code, 200)
for who, h in OTHERS:
    check(f"…{who} still 404", c.get(f"/v1/files/uploads/{named['id']}", headers=h).status_code, 404)

print("\n── the Activity Letter (the admin's): another person's card and facts stay out ──")
import reflection as rf  # noqa: E402
day = rf._today()                       # the household's local day, as the letter counts it (not the host's clock)
start, end = rf._bounds(day)
P.add_card("Robin's dentist moved to Friday", source="calendar", to=[robin])
P.add_card("Bins go out tonight", source="routine", to="household")
items = rf.gather(day, timeline=lambda *a, **k: [], proactive_db=Path(os.environ["PROACTIVE_DB"]),
                  memory_db=_TMP / "none.db")
titles = " ".join(str(i.get("title") or "") for i in items["items"])
check("a household card is in the admin's letter", "Bins go out tonight" in titles, True)
check("a card for Robin only is not", "dentist" in titles, False)
mdb = _TMP / "memory-facts.db"
con = sqlite3.connect(mdb)
con.execute("CREATE TABLE facts (id TEXT, text TEXT, ts REAL, person_id TEXT)")
now = (start + end) / 2 if time.time() > end else max(start + 1, min(time.time(), end - 1))
con.executemany("INSERT INTO facts VALUES (?,?,?,?)", [("f1", "Zach likes the window seat", now, "zach"),
                                                       ("f2", "Robin is planning a surprise for Zach", now, "p_robin")])
con.commit()
con.close()
items = rf.gather(day, timeline=lambda *a, **k: [], proactive_db=_TMP / "empty-proactive.db", memory_db=mdb)
titles = " ".join(str(i.get("title") or "") for i in items["items"])
check("the admin's own fact is remembered in his letter", "window seat" in titles, True)
check("Robin's fact never is", "surprise" in titles, False)

print("\n── partial cells, the missing roles (#1055) ──")
print("   conversations: a kid and the TV")
mem = api._get_memory()
mem.add_message(hh.conv_id_for(robin), "user", "Robin asks about the pool schedule", thread="th-robin", device="Robin's iPhone")
mem.add_message(hh.conv_id_for(robin), "assistant", "It opens at 7.", thread="th-robin", device="Robin's iPhone")
r = c.get("/v1/conversations", headers=AVA)
check("a kid's list: none of Robin's", [x["title"] for x in (r.json().get("conversations") or [])] if r.status_code == 200 else [], [])
for who, h in OTHERS:
    r = c.get("/v1/conversations/th-robin/messages", headers=h)
    check(f"{who}: Robin's thread isn't readable ({r.status_code})", (r.status_code in (403, 404), "pool" in r.text), (True, False))
r = c.get("/v1/conversations", headers=TV)
check("the TV: no conversation list", (r.status_code in (403, 404) or not r.json().get("conversations"), "pool" in r.text), (True, False))

print("   cards: snooze, mute, ack and the long-poll, for every other role")
rc = P.add_card("Robin's prescription is ready", source="tasks", to=[robin])
for who, h in OTHERS:
    check(f"{who}: can't snooze Robin's card (404)",
          c.post(f"/v1/proactive/{rc['id']}/snooze", json={"until": time.time() + 600}, headers=h).status_code, 404)
    check(f"{who}: can't mute it (404 or 403)", c.post(f"/v1/proactive/{rc['id']}/mute", headers=h).status_code in (403, 404), True)
    c.post(f"/v1/proactive/{rc['id']}/ack", headers=h)
    w = c.get("/v1/cards/wait?since=0&timeout=1", headers=h)
    check(f"{who}: the long-poll never carries it", "prescription" in w.text, False)
check("…and for Robin it's still there, unacked and unsnoozed", next((x["acked_at"] for x in c.get("/v1/proactive", headers=ROBIN)
                                                                      .json()["cards"] if x["id"] == rc["id"]), "gone"), None)

print("   Claude chats: another adult and the TV")
import claude_chats as cc  # noqa: E402
cc.store(jordan, "claude-ai", [{"conv_id": "c1", "title": "Jordan's trip plan", "messages": [{"role": "user", "text": "Plan Lisbon"}]}])
check("Jordan sees his own", c.get("/v1/claude-chats", headers=JORDAN).json()["sources"].get("claude-ai", {}).get("conversations"), 1)
check("Robin (another adult) sees none of his", c.get("/v1/claude-chats", headers=ROBIN).json().get("sources"), {})
r = c.get("/v1/claude-chats", headers=TV)
check("the TV: refused, nothing of his", (r.status_code in (403, 404) or not r.json().get("sources"), "Lisbon" in r.text), (True, False))
r = c.delete("/v1/claude-chats?apply=true", headers=ROBIN)
check("Robin's 'forget' can't touch Jordan's", c.get("/v1/claude-chats", headers=JORDAN).json()["sources"].get("claude-ai", {})
      .get("conversations"), 1)

print("\n── batch 2 (#1055): places over HTTP, and records (bills, YouTube) for every role ──")
print("   places: Robin's arrival reaches only her")
r = c.post("/v1/presence/place", json={"id": "evt-r1", "event": "arrived", "place": "Quarry Gym",
                                       "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, headers=ROBIN)
check("Robin's arrival is accepted", r.status_code, 204)
mine_p = c.get("/v1/presence/places", headers=ROBIN).json().get("people") or []
check("Robin sees her own place", [p_.get("place") for p_ in mine_p if p_.get("id") == robin], ["Quarry Gym"])
for who, h in OTHERS:
    r = c.get("/v1/presence/places", headers=h)
    body = r.text
    check(f"{who}: never Robin's place ({r.status_code})", ("Quarry Gym" in body, robin in body), (False, False))

print("   records: Robin's private bill and YouTube video, through find_records")
import agent_runtime_lib as lib  # noqa: E402
NODES = [{"node_id": "bill-r1", "node_type": "bill", "data": {"vendor": "Fern Water", "amount": 42.1, "due_date": "2026-10-20",
          "provenance": {"owner": robin, "audience": "private"}}},
         {"node_id": "yt-r1", "node_type": "youtube_video", "data": {"name": "Sourdough in an hour", "url": "https://youtu.be/x",
          "provenance": {"owner": robin, "audience": "private", "source": "youtube"}}}]
real_rr = lib._runtime_request
lib._runtime_request = lambda svc, path, method="GET", payload=None: {"nodes": [n for n in NODES if ("node_type=" not in path or
                                                                                    f"node_type={n['node_type']}" in path)]}
try:
    def as_(pid, role, rtype):
        lib._TURN_IO.person, lib._TURN_IO.role, lib._TURN_IO.source = {"id": pid, "name": pid}, role, None
        try:
            return lib._execute_tool("find_records", {"type": rtype})
        finally:
            lib._TURN_IO.person = lib._TURN_IO.role = None
    for rtype, word in (("bill", "Fern Water"), ("youtube_video", "Sourdough")):
        check(f"{rtype}: Robin finds her own", word in as_(robin, "adult", rtype), True)
        for who, pid, role in (("another adult", jordan, "adult"), ("a kid", ava, "kid"), ("the household TV", "household", "household")):
            check(f"{rtype}: {who} never sees it", word in as_(pid, role, rtype), False)
    check("the record filter is the per-item one (owner + audience + fences), whatever the tool's role table says",
          [lib._node_visible(NODES[0], p_, r_) for p_, r_ in ((robin, "adult"), (jordan, "adult"), (ava, "kid"), ("household", "household"))],
          [True, False, False, False])
finally:
    lib._runtime_request = real_rr

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
