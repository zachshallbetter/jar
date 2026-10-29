#!/usr/bin/env python3
# gate: hermetic
"""A kid's Watch paired directly (titan-node#755): the devices.kind migration, pairing (a code on the Watch, a parent's
confirmation, the token collected once with the Watch's secret), the hardening (10 minutes, single use, a per-source
limit, 3 refusals per code, the secret never stored or printed), the watch token's lifetime and its few routes, a
parent unpairing their kid's Watch, /v1/watch/today (nothing the kid couldn't already read: their own and household
events, title and time only; their own chores), and a watch turn's shaped reply. Fictional people only.
Run: python3 scripts/test_kid_watch.py"""
from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-kid-watch-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db",
             "PUSH_DB": "push.db", "CONNECTORS_LEDGER": "ledger.db", "ROUTINES_DB": "routines.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "TODOS_DB": "todos.db", "PLACES_DB": "places.db",
             "SPACES_DB": "spaces.db", "PRESENCE_DB": "presence.db", "CONTINUITY_DB": "continuity.db",
             "SERVER_IDENTITY_KEY": "server-identity.key"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.160} expected={expected!r:.160}")


print("── the devices.kind migration ──")
old = _TMP / "old.db"
con = sqlite3.connect(old)
con.executescript("""CREATE TABLE people (id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, created_at REAL);
CREATE TABLE devices (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, name TEXT NOT NULL, platform TEXT NOT NULL,
  token_sha256 TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, last_seen REAL, revoked_at REAL);
INSERT INTO people VALUES ('alex', 'Alex', 'adult', 0);
INSERT INTO devices VALUES ('d_old', 'alex', 'Alex''s iPhone', 'ios', 'x', 0, 0, NULL);""")
con.commit(); con.close()
try:
    s1 = hh.HouseholdStore(old)
    s2 = hh.HouseholdStore(old)   # opened again: nothing to add, nothing fails
    kinds = [r[0] for r in sqlite3.connect(old).execute("SELECT kind FROM devices")]
    check("an older table gains kind; its rows are ordinary devices; opening twice is fine", kinds, ["device"])
except Exception as exc:  # noqa: BLE001
    check("the migration", f"{type(exc).__name__}: {exc}", "no error")

print("── pairing ──")
now = [1_800_000_000.0]
store = hh.HouseholdStore(_TMP / "api.db", now=lambda: now[0])
hh.reset_store(store)
alex, robin, jordan = store.add_person("Alex", "adult"), store.add_person("Robin", "adult"), store.add_person("Jordan", "adult")
ava, leo = store.add_person("Ava", "kid"), store.add_person("Leo", "kid")
store.update_person(ava["id"], parents=[alex["id"], robin["id"]])
store.update_person(leo["id"], parents=[alex["id"]])
ALEX = hh.Identity(alex, {"id": "d_alex", "name": "Alex's iPhone", "platform": "ios"}, "adult", "device")
JORDAN = hh.Identity(jordan, {"id": "d_jordan", "name": "Jordan's iPhone", "platform": "ios"}, "adult", "device")

out = io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
    req = store.watch_pair_request("Ava's Watch")
    check("the Watch gets a 6-digit code and a secret", (len(req["code"]), req["code"].isdigit(), len(req["secret"]) > 30), (6, True, True))
    check("…waiting for a parent", store.watch_pair_poll(req["request_id"], req["secret"]), None)
    try:
        store.watch_pair_confirm(req["code"], ava["id"], JORDAN)
        check("an adult who isn't Ava's parent: refused", "ok", 403)
    except hh.PairingError as e:
        check("an adult who isn't Ava's parent: refused", e.status, 403)
    got = store.watch_pair_confirm(req["code"], ava["id"], ALEX)
    check("Ava's parent confirms", got, {"request_id": req["request_id"], "kid_first_name": "Ava"})
    try:
        store.watch_pair_poll(req["request_id"], "not-the-secret")
        check("the wrong secret", "token", 404)
    except hh.PairingError as e:
        check("the wrong secret", e.status, 404)
    tok = store.watch_pair_poll(req["request_id"], req["secret"])
    check("the token, once", (tok["kid_first_name"], tok["device"]["name"], bool(tok["token"])), ("Ava", "Ava's Watch", True))
    try:
        store.watch_pair_poll(req["request_id"], req["secret"])
        check("collected twice", "token", 410)
    except hh.PairingError as e:
        check("collected twice", e.status, 410)
check("the poll secret never printed", req["secret"] in out.getvalue(), False)
check("…nor stored", req["secret"].encode() in (_TMP / "api.db").read_bytes(), False)

r2 = store.watch_pair_request("Leo's Watch")
for _ in range(hh.WATCH_CONFIRM_ATTEMPTS):
    try:
        store.watch_pair_confirm(r2["code"], leo["id"], JORDAN)
    except hh.PairingError:
        pass
try:
    store.watch_pair_confirm(r2["code"], leo["id"], ALEX)
    check("3 refusals spend the code", "confirmed", 410)
except hh.PairingError as e:
    check("3 refusals spend the code", e.status, 410)
r3 = store.watch_pair_request("x")
try:
    store.watch_pair_confirm(r3["code"], alex["id"], ALEX)
    check("only for a kid", "confirmed", 400)
except hh.PairingError as e:
    check("only for a kid", e.status, 400)
r4 = store.watch_pair_request("Leo's Watch")
now[0] += hh.CODE_TTL_S + 1
try:
    store.watch_pair_confirm(r4["code"], leo["id"], ALEX)
    check("a code lives 10 minutes", "confirmed", 410)
except hh.PairingError as e:
    check("a code lives 10 minutes", e.status, 410)

print("── the watch token ──")
ident = store.resolve_device(tok["token"])
check("Ava's, role kid, via watch", (ident.person_id, ident.role, ident.via, ident.device["platform"]),
      (ava["id"], "kid", "watch", "watchos"))
check("an ordinary claim is still a device", store.resolve_device("not-a-token"), None)
now[0] += hh.WATCH_IDLE_S + 10
check("90 days unused: it lapses", store.resolve_device(tok["token"]), None)

print("── the routes ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import kids_screen  # noqa: E402
import schedule_view  # noqa: E402
now[0] = 1_800_000_000.0
MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
store2 = hh.HouseholdStore(_TMP / "routes.db")
hh.reset_store(store2)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def paired(name, role):
    s = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    r = c.post("/v1/pair/claim", json={"code": s["code"], "device_name": f"{name}'s iPhone", "platform": "ios"}).json()
    return s["person"], {"X-Agent-Token": r["token"]}, r["device"]["id"]


p_alex, A, _ = paired("Alex", "adult")
p_jordan, J, _ = paired("Jordan", "adult")
p_ava, _, ava_ipad = paired("Ava", "kid")
p_leo, _, _ = paired("Leo", "kid")
store2.update_person(p_ava["id"], parents=[p_alex["id"]])
store2.update_person(p_leo["id"], parents=[p_alex["id"]])
r = c.post("/v1/watch/pair/request", json={"device_name": "Ava's Watch"})
w = r.json()
check("the Watch asks, no token", (r.status_code, sorted(w)), (200, ["code", "expires_at", "request_id", "secret"]))
check("polling before a parent: 202", c.get(f"/v1/watch/pair/request/{w['request_id']}", headers={"X-Pair-Secret": w["secret"]}).status_code, 202)
check("Jordan (not Ava's parent): 403", c.post("/v1/watch/pair/confirm", json={"code": w["code"], "person_id": p_ava["id"]}, headers=J).status_code, 403)
check("Alex confirms", c.post("/v1/watch/pair/confirm", json={"code": w["code"], "person_id": p_ava["id"]}, headers=A).json()["kid_first_name"], "Ava")
t = c.get(f"/v1/watch/pair/request/{w['request_id']}", headers={"X-Pair-Secret": w["secret"]}).json()
WH = {"X-Agent-Token": t["token"]}
check("the Watch's own few routes", [c.get("/v1/me", headers=WH).status_code, c.get("/v1/timeline", headers=WH).status_code,
      c.get("/v1/schedule", headers=WH).status_code, c.get("/v1/proactive", headers=WH).status_code,
      c.get("/v1/memory/facts", headers=WH).status_code, c.post("/v1/prompt", json={"prompt": "hi"}, headers=WH).status_code],
      [200, 403, 403, 403, 403, 403])

calls: list = []


def fake_view(start, end, viewer, role, only):
    calls.append((viewer, role))
    return {"people": [{"id": p_ava["id"], "you": True, "events": [{"title": "Ballet", "start": "2026-10-05T16:00:00",
                        "end": "2026-10-05T17:00:00", "all_day": False, "location": "Maple Studio", "calendar": "Ava"}]},
                       {"id": p_alex["id"], "you": False, "events": [{"title": "Alex private: lawyer", "start": "2026-10-05T10:00:00"}]},
                       {"id": p_leo["id"], "you": False, "events": [{"title": "Leo private: dentist", "start": "2026-10-05T11:00:00"}]}],
            "household": {"events": [{"title": "Family dinner", "start": "2026-10-05T18:00:00", "location": "Home", "managed_by": "household"}]}}


real_view, schedule_view.for_viewer = schedule_view.for_viewer, fake_view
real_gather, kids_screen.gather = kids_screen.gather, lambda role: {"kids": [
    {"id": p_ava["id"], "name": "Ava", "chores": [{"id": "c1", "text": "Feed Biscuit", "done": False, "at": "7:00"}], "next": "Ballet at 4:00"},
    {"id": p_leo["id"], "name": "Leo", "chores": [{"id": "c2", "text": "Leo's homework", "done": True}]}]}
today = c.get("/v1/watch/today", headers=WH).json()
schedule_view.for_viewer, kids_screen.gather = real_view, real_gather
check("built from the kid's own schedule rules (for_viewer as Ava, role kid)", calls, [(p_ava["id"], "kid")])
check("her own events and the household's; never a parent's or a sibling's, even when shown",
      [e["title"] for e in today["events"]], ["Ballet", "Family dinner"])
check("title and time only: no place, calendar or organizer", sorted(today["events"][0]), ["all_day", "end", "start", "title"])
check("her own chores only", today["chores"], [{"id": "c1", "text": "Feed Biscuit", "done": False}])
check("an adult's token: not their view", c.get("/v1/watch/today", headers=A).status_code, 403)

check("Alex lists Ava's Watch", [x["kid_first_name"] for x in c.get("/v1/watch/devices", headers=A).json()["watches"]], ["Ava"])
check("Jordan sees none", c.get("/v1/watch/devices", headers=J).json(), {"watches": []})
check("Jordan can't unpair it", c.delete(f"/v1/devices/{t['device']['id']}", headers=J).status_code, 403)
check("a parent can't revoke the kid's ordinary devices (only a Watch)", c.delete(f"/v1/devices/{ava_ipad}", headers=A).status_code, 403)
check("the Watch can't revoke another device", c.delete(f"/v1/devices/{ava_ipad}", headers=WH).status_code, 403)
check("Alex unpairs it", c.delete(f"/v1/devices/{t['device']['id']}", headers=A).status_code, 200)
check("…its token is gone", c.get("/v1/me", headers=WH).status_code, 401)
codes = [c.post("/v1/watch/pair/request", json={}).status_code for _ in range(12)]
check("pair/request is limited per source", codes[-1], 429)

print("── a watch turn's reply ──")
long = "Ballet is at four. " * 30
shaped = api._watch_shaped({"text": long, "blocks": [{"type": "document"}], "sources": [{"source_id": "s"}],
                            "cites": [1], "peek": {"x": 1}, "trace": "t", "egress": ["gemini"]})
check("short text only: no blocks, sources, cites or peek", (sorted(shaped), len(shaped["text"]) <= api.WATCH_REPLY_CHARS),
      (["blocks", "egress", "sources", "text", "truncated"], True))
check("cut at a sentence", shaped["text"].endswith("."), True)
check("a watch turn is known by its identity", (api._is_watch_turn({"identity": hh.Identity(ava, None, "kid", "watch")}),
      api._is_watch_turn({"identity": ALEX})), (True, False))
admin = hh.owner_identity()
check("an eval may ask as a kid's Watch", api._eval_as({"eval_as": {"role": "kid", "via": "watch"}}, "eval", admin).via, "watch")
check("…only an eval", api._eval_as({"eval_as": {"role": "kid", "via": "watch"}}, None, admin).via, "master")
check("…only a kid", api._eval_as({"eval_as": {"role": "adult", "via": "watch"}}, "eval", admin).via, "eval")
import privacy_eval  # noqa: E402
names = [n for n, who, *_ in privacy_eval.CASES if who.get("via") == "watch"]
check("privacy_eval has the kid's Watch cases", len(names) >= 5, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
