#!/usr/bin/env python3
# gate: hermetic   (throwaway stores; the real routes through a TestClient)
"""A paired executor on another machine (titan-node#1631 slice 4): its owner starts the pairing for themselves (kind
"executor"), it claims as platform "other", and its token reaches only its presence and its own inbox. Every other
route of the API, whatever the method, answers it 403; the routes are read from the app itself, so one added later is
covered. Its inbox key is always its own device id, never one it names (a Mac's), so it can't take its owner's Mac
requests. Presence rows from before have no kind and read as Macs, and Mac tools never route to an executor, the
owner's or anyone else's. Fictional people (Alex the admin, Robin an adult). Run: python3 scripts/test_executor_pairing.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-executor-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "SPACES_DB": "spaces.db",
             "SCHEDULED_DB": "scheduled.db", "REFERENCES_DB": "references.db", "WORLD_DIR": "world", "PUSH_DB": "push.db",
             "SITTER_DB": "sitter.db", "LOCK_UP_DB": "lock_up.db", "TV_UNLOCK_DB": "tv_unlock.db", "LINKS_DB": "links.db",
             "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily",
             "FENCES_DB": "fences.db", "LINK_SALT_FILE": "link_salt", "MEMORY_DB": "memory.db", "TITLES_DB": "titles.db",
             "RETRIEVAL_TRACE_DB": "trace.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL", "APNS_KEY_FILE"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).parent))
import titan_alert  # noqa: E402
titan_alert.alert = lambda *a, **k: None

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.600} expected={expected!r:.300}"))


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import cross_device as xd  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-executor-test-0123456789"
api._AGENT_TOKEN = MASTER
store = hh.HouseholdStore(_TMP / "api.db")
hh.reset_store(store)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}
admin = hh.ADMIN_PERSON_ID

print("── pairing ──")
r = c.post("/v1/pair/start", json={"person_id": admin, "kind": "executor", "label": "Garage box"}, headers=M)
check("the owner (the admin) starts an executor pairing for themselves", r.status_code, 200)
code = r.json()["code"]
bad = c.post("/v1/pair/claim", json={"code": code, "device_name": "box", "platform": "mac"})
check("…claimed as a Mac: refused (an executor pairs as platform other)", bad.status_code, 400)
check("…while that code is open, a second executor code for the same person is refused (one at a time)",
      c.post("/v1/pair/start", json={"person_id": admin, "kind": "executor"}, headers=M).status_code, 429)
cl = c.post("/v1/pair/claim", json={"code": code, "device_name": "Garage box", "platform": "other"}).json()
EX = {"X-Agent-Token": cl["token"]}
ex_id = cl["device"]["id"]
check("…claimed as platform other: a token for that device", bool(cl.get("token")), True)
ident = hh.resolve(cl["token"], MASTER)
check("its token resolves as an executor", (ident.via, ident.person_id), ("executor", admin))
robin_start = c.post("/v1/pair/start", json={"new_person": {"name": "Robin Hale", "role": "adult"}}, headers=M).json()
robin = robin_start["person"]["id"]
check("an executor for someone else (Robin) can't be started for them: their own, later",
      c.post("/v1/pair/start", json={"person_id": robin, "kind": "executor"}, headers=M).status_code, 403)
check("an unknown kind is refused", c.post("/v1/pair/start", json={"person_id": admin, "kind": "robot"}, headers=M).status_code, 400)
rob_cl = c.post("/v1/pair/claim", json={"code": robin_start["code"], "device_name": "Robin's iPhone", "platform": "ios"}).json()
check("an ordinary pairing still makes an ordinary device", hh.resolve(rob_cl["token"], MASTER).via, "device")

print("── an adult pairs their own machine, from their own device ──")
ROB = {"X-Agent-Token": rob_cl["token"]}
r = c.post("/v1/me/executor-pairing", json={"label": "Robin's NAS"}, headers=ROB)
check("Robin starts a pairing for their own machine", (r.status_code, r.json().get("person", {}).get("id")), (200, robin))
rcl = c.post("/v1/pair/claim", json={"code": r.json()["code"], "device_name": "Robin's NAS", "platform": "other"}).json()
rid = hh.resolve(rcl["token"], MASTER)
check("…it claims as an executor of Robin's, with the x. token", (rid.via, rid.person_id, rcl["token"].startswith("x.")),
      ("executor", robin, True))
r2 = c.post("/v1/me/executor-pairing", headers=ROB)
check("…with no body at all, another opens once that one is used", r2.status_code, 200)
check("…but not a second open one at once: one open machine code a person", c.post("/v1/me/executor-pairing",
                                                                                 headers=ROB).status_code, 429)
kid_start = c.post("/v1/pair/start", json={"new_person": {"name": "Ava Hale", "role": "kid"}}, headers=M).json()
kid_cl = c.post("/v1/pair/claim", json={"code": kid_start["code"], "device_name": "Ava's iPad", "platform": "ipados"}).json()
tv_start = c.post("/v1/pair/start", json={"new_person": {"name": "Living room", "role": "household"}}, headers=M).json()
tv_cl = c.post("/v1/pair/claim", json={"code": tv_start["code"], "device_name": "TV", "platform": "tvos"}).json()
check("a kid, a shared screen and an executor's own token can't start one",
      [c.post("/v1/me/executor-pairing", headers={"X-Agent-Token": t}).status_code
       for t in (kid_cl["token"], tv_cl["token"], rcl["token"])], [403, 403, 403])
check("Robin's machine is Robin's: never in Alex's executors",
      rcl["device"]["id"] in [m["device_key"] for m in store.macs_for(admin, kind="executor")], False)

print("── its token can't outlive this code (review: a rollback) ──")
import re as _re  # noqa: E402
check("an executor's token has the x. prefix; the old shape check rejects it (a rollback answers 401, never admin)",
      (cl["token"].startswith(hh.EXECUTOR_TOKEN_PREFIX), _re.fullmatch(r"[A-Za-z0-9_-]{43}", cl["token"]) is None), (True, True))
check("an ordinary device's token has no prefix", rob_cl["token"].startswith(hh.EXECUTOR_TOKEN_PREFIX), False)
con = store._db
con.execute("UPDATE devices SET kind='future-kind' WHERE id=?", (rob_cl["device"]["id"],))
check("a device of a kind this code doesn't know resolves to nothing", hh.resolve(rob_cl["token"], MASTER), None)
con.execute("UPDATE devices SET kind='device' WHERE id=?", (rob_cl["device"]["id"],))
con.execute("UPDATE devices SET kind='executor' WHERE id=?", (rob_cl["device"]["id"],))
check("a row marked executor with an unprefixed token resolves to nothing", hh.resolve(rob_cl["token"], MASTER), None)
con.execute("UPDATE devices SET kind='device' WHERE id=?", (rob_cl["device"]["id"],))
con.execute("UPDATE devices SET kind='device' WHERE id=?", (ex_id,))
check("…and an x. token on a row that isn't an executor's, too", hh.resolve(cl["token"], MASTER), None)
con.execute("UPDATE devices SET kind='executor' WHERE id=?", (ex_id,))
check("restored: the executor resolves again", hh.resolve(cl["token"], MASTER).via, "executor")
n_people = len(store.people())
check("new_person with kind executor: refused before anyone is created",
      (c.post("/v1/pair/start", json={"new_person": {"name": "Leo", "role": "kid"}, "kind": "executor"}, headers=M).status_code,
       len(store.people())), (403, n_people))

print("── its token reaches its presence and its own inbox, nothing else ──")
PUBLIC, PUBLIC_PREFIXES = api._PUBLIC_PATHS, api._PUBLIC_PREFIXES
calls = []
for rt in api.app.routes:
    path = getattr(rt, "path", "")
    if not path.startswith("/v1/") or path in PUBLIC or path.startswith(PUBLIC_PREFIXES):
        continue
    for m in sorted(getattr(rt, "methods", set()) - {"HEAD", "OPTIONS"}):
        calls.append((m, re.sub(r"\{[^}]+\}", "x1", path)))
calls = sorted(set(calls))
check("there are authenticated routes to walk", len(calls) > 100, True)
ALLOWED = {("POST", "/v1/device/presence"), ("GET", "/v1/device/inbox"), ("POST", "/v1/device/inbox/x1/result")}
leaky = []
for m, p in calls:
    if (m, p) in ALLOWED:
        continue
    s = c.request(m, p, headers=EX, json={}).status_code
    if s != 403:
        leaky.append((m, p, s))
check("every other route, every method, answers the executor 403", leaky, [])
check("…and the three it may use are exactly those", sorted(ALLOWED <= set(calls) and ALLOWED), sorted(ALLOWED))

print("── its inbox is its own: never its owner's Mac's ──")
mac = c.post("/v1/pair/start", json={"person_id": admin}, headers=M).json()
mac_cl = c.post("/v1/pair/claim", json={"code": mac["code"], "device_name": "Alex's Mac", "platform": "mac"}).json()
MAC = {"X-Agent-Token": mac_cl["token"]}
r = c.post("/v1/device/presence", json={"device_key": "alex-mac-key", "name": "Alex's Mac", "awake": True}, headers=MAC)
check("the Mac reports presence", r.status_code, 200)
r = c.post("/v1/device/presence", json={"device_key": "alex-mac-key", "name": "Garage box", "awake": True}, headers=EX)
check("the executor naming the Mac's key is recorded under its own device id instead", r.status_code, 200)
macs = store.macs_for(admin)
exs = store.macs_for(admin, kind="executor")
check("Alex's Macs: only the Mac, whose row it didn't overwrite", [(m["device_key"], m["name"]) for m in macs],
      [("alex-mac-key", "Alex's Mac")])
check("Alex's executors: the box, keyed by its own device id", [(m["device_key"], m["name"]) for m in exs], [(ex_id, "Garage box")])
xd.CHANNEL.enqueue((admin, "alex-mac-key"), {"id": "rq-mac-1", "tool": "read_file", "args": {}})
r = c.get("/v1/device/inbox?wait=0&device_key=alex-mac-key", headers=EX)
check("the executor asking for the Mac's inbox gets its own (empty), never the Mac's request",
      (r.status_code, r.json().get("requests")), (200, []))
check("…the Mac's request is still waiting for the Mac", [i["id"] for i in xd.CHANNEL.take((admin, "alex-mac-key"))], ["rq-mac-1"])

r1 = c.post("/v1/device/presence", json={"device_key": ex_id, "name": "Not a box", "awake": True}, headers=MAC)
r2 = c.get(f"/v1/device/inbox?wait=0&device_key={ex_id}", headers=MAC)
r3 = c.post("/v1/device/inbox/rq-x/result", json={"status": "done", "result": "x", "device_key": ex_id}, headers=MAC)
check("the owner's own Mac can't name the executor's key: presence, inbox and result all 403",
      (r1.status_code, r2.status_code, r3.status_code), (403, 403, 403))
check("…and the executor's row is still an executor's, named as it named itself",
      [(m["device_key"], m["name"]) for m in store.macs_for(admin, kind="executor")], [(ex_id, "Garage box")])

print("── Mac tools never route to an executor ──")
xd.CHANNEL.poll_started((admin, ex_id))          # the box is live
check("a live executor beside an asleep, unwakeable Mac: no Mac for Mac tools",
      xd.mac_for(admin, store) is None or xd.mac_for(admin, store)["device_key"] == "alex-mac-key", True)
check("…and never the executor", (xd.mac_for(admin, store) or {}).get("device_key") != ex_id, True)
xd.CHANNEL.poll_started((admin, "alex-mac-key"))
check("the Mac live: the Mac", xd.mac_for(admin, store)["device_key"], "alex-mac-key")
check("Robin has no Mac: nothing, never Alex's executor", xd.mac_for(robin, store), None)
con = store._db
con.execute("INSERT INTO mac_presence (person_id, device_key, device_id, name, updated_at) VALUES (?,?,?,?,?)",
            (admin, "old-mac", None, "Old Mac", 1.0))
check("a presence row from before #1631 (no kind) reads as a Mac",
      "old-mac" in [m["device_key"] for m in store.macs_for(admin)], True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
