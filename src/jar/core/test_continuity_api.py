#!/usr/bin/env python3
"""Continuity across a person's devices (jellybean#132 + #138): GET /v1/sync, read marks, and what each
device was on. Hermetic. The privacy rule (item 9) is the point: each person's feed and context are
theirs alone; a kid's never reaches anyone; leaving the household clears it.

Run: python3 scripts/test_continuity_api.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-continuity-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "TODOS_DB": "todos.db",
             "TASK_LOG_DB": "tasklog.db", "CONTINUITY_DB": "continuity.db", "PUSH_DB": "push.db",
             "PROACTIVE_DB": "proactive.db", "ROUTINES_DB": "routines.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json",
             "AGENT_TURN_LOG": "turns.jsonl", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import continuity as C  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── the store ──")
check("addresses keep no query, fragment or login", C.clean_url("https://user:pw@news.example.com/a/b?session=abc123&utm=x#top"),
      "https://news.example.com/a/b")
check("a token-like path segment is hidden", C.clean_url("https://x.example/reset/token_8f3a9c2b7d1e4f6a5b0c9d8e7f6a5b4c"),
      "https://x.example/reset/…")
check("only http(s)", (C.clean_url("file:///Users/z/secret.pdf"), C.clean_url("javascript:alert(1)")), (None, None))
check("capability ids, JWTs and encoded tokens are hidden", [C.clean_url(u) for u in (
      "https://docs.example.com/d/1AbC2dEf3GhI4jKl5MnO/edit", "https://x.example/magic/eyJhbGciOiJIUzI1NiJ9",
      "https://x.example/r/%74oken_abcdefghijklmnopqrstu", "http://[::1]:8080/a")],
      ["https://docs.example.com/d/…/edit", "https://x.example/magic/…", "https://x.example/r/…", "http://[::1]:8080/a"])
check("a test never writes the live feed", str(C._path()).startswith("/ai/"), False)
s0 = C.latest(["p_a"])
C.emit("p_a", "todo", {"id": "t1"})
C.emit("p_b", "todo", {"id": "t2"})
check("a feed shows only its owners' changes", [c["id"] for c in C.since(["p_a"], s0)], ["t1"])
check("an unknown kind is never recorded", C.emit("p_a", "secret", {"x": 1}), None)
check("read marks never move back", (C.set_read("p_a", "th-1", 200.0), C.set_read("p_a", "th-1", 100.0)), (200.0, 200.0))

print("\n── through the API ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import todos  # noqa: E402
import task_log  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
hh.reset_store(hh.HouseholdStore(_TMP / "api.db"))
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(body: dict, name: str) -> tuple[dict, str]:
    s = c.post("/v1/pair/start", json=body, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": name, "platform": "ios"}).json()["token"]
    return {"X-Agent-Token": tok}, s["person"]["id"]


MIR, mid = pair({"new_person": {"name": "Robin", "role": "adult"}}, "Robin's iPhone")
MIR_MAC, _ = pair({"person_id": mid}, "Robin's MacBook")
KID, kid = pair({"new_person": {"name": "Ava", "role": "kid"}}, "Ava's iPhone")
zach = hh.owner_identity().person_id
start = {"m": c.get("/v1/sync", headers=MIR).json()["cursor"], "z": c.get("/v1/sync", headers=M).json()["cursor"],
         "k": c.get("/v1/sync", headers=KID).json()["cursor"]}


def feed(h, key):
    return c.get(f"/v1/sync?since={start[key]}", headers=h).json()["changes"]


# Todos: a person's own list reaches only them; the household's reaches everyone.
todos.add("Call the vet", mid, created_by="Robin")
todos.add("Garbage out", "household", created_by="Zach")
zt = todos.add("Renew registration", zach, created_by="Zach")
todos.complete(zt["id"], "Zach")
mt = [(x["title"], x["done"]) for x in feed(MIR, "m") if x["kind"] == "todo"]
check("Robin's feed: her todo and the household's, never Zach's", mt, [("Call the vet", False), ("Garbage out", False)])
check("Zach's feed: his own (added, then done) and the household's",
      [(x["title"], x["done"]) for x in feed(M, "z") if x["kind"] == "todo"],
      [("Garbage out", False), ("Renew registration", False), ("Renew registration", True)])
check("a kid's feed: the household's only", [x["title"] for x in feed(KID, "k") if x["kind"] == "todo"], ["Garbage out"])

# Chat turns: the person's other devices see them; the asking app's own message carries its id.
mir_phone = next(d["id"] for p in hh.get_store().people() if p["id"] == mid for d in p.get("devices", []) if d["name"] == "Robin's iPhone")
ident = hh.Identity(person={"id": mid, "name": "Robin", "role": "adult"}, device={"id": mir_phone, "name": "Robin's iPhone"},
                    role="adult", via="device")
api._turns["t-1"] = {"thread": "iphone-abc", "client_msg_id": "cm-1", "remember": True}
api._remember_turn("t-1", ident, "When is the dentist?", "Tomorrow at 9 AM.")
msgs = [x for x in feed(MIR_MAC, "m") if x["kind"] == "message"]
check("her Mac sees her phone's turn, in order", [(x["role"], x["text"], x["thread_id"]) for x in msgs],
      [("user", "When is the dentist?", "iphone-abc"), ("assistant", "Tomorrow at 9 AM.", "iphone-abc")])
check("the question carries the phone's client_msg_id (its own echo is skipped)", [x["client_msg_id"] for x in msgs], ["cm-1", None])
check("the reply names its question (reply_to)", [x.get("reply_to") for x in msgs], [None, "cm-1"])
check("#419: on her Mac it isn't hers; on the phone that asked, both halves are `mine`",
      ([x["mine"] for x in msgs], [x["mine"] for x in feed(MIR, "m") if x["kind"] == "message"]), ([False, False], [True, True]))
check("the store's own fields never reach a client", [k for x in msgs for k in ("seq", "from_device") if k in x], [])
check("never in Zach's feed", [x for x in feed(M, "z") if x["kind"] == "message"], [])
api._turns["t-2"] = {"thread": "iphone-abc", "source": "eval", "remember": True}
api._remember_turn("t-2", ident, "eval question", "eval answer")
check("an eval turn never reaches the feed", len([x for x in feed(MIR, "m") if x["kind"] == "message"]), 2)

# Read marks.
r = c.post("/v1/conversations/iphone-abc/read", json={"upto": msgs[-1]["at"]}, headers=MIR)
check("marking her conversation read", (r.status_code, r.json().get("read_upto") == msgs[-1]["at"]), (200, True))
check("her other device hears it", [x["read_upto"] == msgs[-1]["at"] for x in feed(MIR_MAC, "m") if x["kind"] == "thread"], [True])
convs = c.get("/v1/conversations", headers=MIR).json()["conversations"]
check("her conversation list says how far she's read", [x.get("read_upto") == msgs[-1]["at"] for x in convs if x["id"] == "iphone-abc"], [True])
check("someone else's conversation can't be marked", c.post("/v1/conversations/iphone-abc/read", json={"upto": 1}, headers=KID).status_code, 404)
check("upto is required", c.post("/v1/conversations/iphone-abc/read", json={}, headers=MIR).status_code, 400)

# Task steps: metadata only.
task_log.record("task-1", "Plan the party", "Book the venue", "done", "called them; deposit $200 on card 4111", mid)
tk = [x for x in feed(MIR, "m") if x["kind"] == "task"]
check("a task step reaches its owner, without the note", tk and {k: tk[0][k] for k in ("task_id", "title", "step", "state")}
      == {"task_id": "task-1", "title": "Plan the party", "step": "Book the venue", "state": "done"} and "note" not in tk[0], True)
check("…and nobody else", [x for x in feed(M, "z") if x["kind"] == "task"], [])

# What each device was on.
r = c.post("/v1/presence/context", json={"kind": "web", "title": "School lunch menu",
                                         "url": "https://school.example.org/menu/oct?auth=xyz#week2"}, headers=MIR)
check("a page is kept without its query or fragment", (r.status_code, r.json().get("url")), (200, "https://school.example.org/menu/oct"))
check("her Mac sees what her phone was on", [(x["title"], x["device_name"]) for x in c.get("/v1/presence/context", headers=MIR_MAC).json()["contexts"]],
      [("School lunch menu", "Robin's iPhone")])
check("the phone itself isn't told its own", c.get("/v1/presence/context", headers=MIR).json()["contexts"], [])
c.post("/v1/presence/context", json={"kind": "media", "title": "Ava's playlist", "position": 61.5}, headers=KID)
check("a kid's context never reaches anyone else",
      [x["title"] for h in (M, MIR, MIR_MAC) for x in c.get("/v1/presence/context", headers=h).json()["contexts"]], ["School lunch menu"])
check("#132: kept half an hour, not a day", C.CONTEXT_S, 30 * 60)
phone_dev = next(d["id"] for p in hh.get_store().people() if p["id"] == mid for d in p.get("devices", []) if d["name"] == "Robin's iPhone")
check("someone else can't pick up her context", c.delete(f"/v1/presence/context/{phone_dev}", headers=KID).status_code, 404)
check("her Mac picks up what her phone was on: it goes", (c.delete(f"/v1/presence/context/{phone_dev}", headers=MIR_MAC).status_code,
      c.get("/v1/presence/context", headers=MIR_MAC).json()["contexts"]), (200, []))
check("…and again is a 404", c.delete(f"/v1/presence/context/{phone_dev}", headers=MIR_MAC).status_code, 404)
c.post("/v1/presence/context", json={"kind": "web", "title": "School lunch menu", "url": "https://school.example.org/menu/oct"}, headers=MIR)
check("a web context needs a real address", c.post("/v1/presence/context", json={"kind": "web", "url": "file:///etc/passwd"}, headers=MIR).status_code, 400)
check("an unknown kind is refused", c.post("/v1/presence/context", json={"kind": "screen", "title": "x"}, headers=MIR).status_code, 400)

# Long-poll and cursors (#420: opaque, and only the person's own).
cur = c.get("/v1/sync", headers=MIR).json()["cursor"]
again = c.get(f"/v1/sync?since={cur}&wait=0", headers=MIR).json()
check("nothing new: an empty answer at once with wait=0", (again["changes"], C.decode_cursor(mid, again["cursor"]) == C.decode_cursor(mid, cur)),
      ([], True))
check("a cursor is opaque: not a number, and the same position never reads the same twice",
      (cur.isdigit(), again["cursor"] != cur), (False, True))
todos.add("Zach's own errand", zach, created_by="Zach")
todos.add("Ava's homework", kid, created_by="Ava")
after = c.get(f"/v1/sync?since={cur}&wait=0", headers=MIR).json()
check("other people's changes don't move her cursor (it says nothing about their activity)",
      (after["changes"], C.decode_cursor(mid, after["cursor"]) == C.decode_cursor(mid, cur)), ([], True))
check("her cursor is useless to someone else: Ava's answer is a reset, not Robin's feed",
      (lambda r: (r.get("reset"), r["changes"]))(c.get(f"/v1/sync?since={cur}", headers=KID).json()), (True, []))
check("an altered or made-up cursor is a reset", [c.get(f"/v1/sync?since={x}", headers=MIR).json().get("reset")
                                                  for x in ("999999", cur[:-2] + ("AA" if cur[-2:] != "AA" else "BB"), "%00")], [True, True, True])
check("since=now: from here on, no backlog", c.get("/v1/sync?since=now", headers=MIR).json()["changes"], [])
check("from the start: her week's feed", len(c.get("/v1/sync", headers=MIR).json()["changes"]) >= 5, True)

# #419: a change nudges her other phones, content-free.
import push  # noqa: E402
devs = push._household_devices()
mac_id = next(d["id"] for p in hh.get_store().people() if p["id"] == mid for d in p.get("devices", []) if d["name"] == "Robin's MacBook")
kid_dev = next(d["id"] for p in hh.get_store().people() if p["id"] == kid for d in p.get("devices", []))
for d in (mir_phone, mac_id, kid_dev):
    push.register(d, "ab" * 32, "development", "com.zachshallbetter.jellybean.ios")
sent: list[str] = []
with push._db() as con:
    con.execute("DELETE FROM sync_live")   # this test's own polls above


def fake(t):
    sent.append(t["device_id"])
    return 200, ""


push.nudge_sync(mid, mir_phone, devices=devs, send=fake)
check("a change on her phone nudges her Mac, not the phone, never Ava", sent, [mac_id])
push.nudge_sync(mid, mir_phone, devices=devs, send=fake)
check("at most one nudge per device every 30 s", sent, [mac_id])
with push._db() as con:
    con.execute("DELETE FROM sync_nudged")
push.mark_sync_live(mac_id)
push.nudge_sync(mid, None, devices=devs, send=fake)
check("a device waiting on /v1/sync isn't nudged", sorted(sent), sorted([mac_id, mir_phone]))
check("the nudge carries nothing but 'sync'", push.SYNC_PAYLOAD, {"aps": {"content-available": 1}, "sync": 1})
with push._db() as con:
    con.execute("DELETE FROM sync_nudged")
    con.execute("DELETE FROM sync_live")
gone = push.nudge_sync(mid, mac_id, devices=devs, send=lambda t: (410, "Unregistered"))
check("a dead token is dropped", (gone, [t["device_id"] for t in push._tokens()].count(mir_phone)), ([f"{mir_phone}: 410 Unregistered"], 0))
check("polling marks the device as waiting", (c.get("/v1/sync?since=now", headers=MIR_MAC).status_code,
                                             [t["device_id"] for t in push.sync_targets(mid, None, devs)]), (200, []))

# Removing one device keeps the rest (an old iPad mustn't mark everything unread).
c.post("/v1/presence/context", json={"kind": "media", "title": "Podcast", "position": 12}, headers=MIR_MAC)
import places as _pl  # noqa: E402
for h, eid, place in ((MIR, "pe-phone-1", "Work"), (MIR_MAC, "pe-mac-1", "Studio")):
    c.post("/v1/presence/place", json={"id": eid, "event": "arrived", "place": place}, headers=h)
_pl.record("pe-legacy-1", mid, "left", "home", None)   # from before devices were recorded
mac_dev = next(d["id"] for p in hh.get_store().people() if p["id"] == mid for d in p.get("devices", []) if d["name"] == "Robin's MacBook")
c.delete(f"/v1/devices/{mac_dev}", headers=M)
check("one of two devices removed: only its context goes", ([x["title"] for x in C.get_context(mid)], bool(C.reads(mid))),
      (["School lunch menu"], True))
check("#421: one device removed: only its place events go; the phone's and older ones stay",
      sorted(e["place"] for e in _pl.recent(mid)), ["Work", "home"])
check("a list body is a 400, not a 500", c.post("/v1/conversations/iphone-abc/read", json=[1], headers=MIR).status_code, 400)

# Leaving the household clears it.
dev = next(d["id"] for p in hh.get_store().people() if p["id"] == mid for d in p.get("devices", []) if d["name"] == "Robin's iPhone")
c.delete(f"/v1/devices/{dev}", headers=M)
check("leaving clears her feed, read marks and context", (C.since([mid], 0), C.reads(mid), C.get_context(mid)), ([], {}, []))
check("…and every place event, older ones included", _pl.recent(mid), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
