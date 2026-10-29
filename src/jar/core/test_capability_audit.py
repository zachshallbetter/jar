#!/usr/bin/env python3
# gate: hermetic
"""What titan did for each person (#1628 slice 4, capability_audit.py): one row per tool call with the tool, its
domain and risk, ran or refused and why, and how it came out, never arguments or results; eval, canary and test turns
aren't recorded; rows older than 30 days are pruned; GET /v1/me/capability-log returns the caller's own rows only and
GET/PUT /v1/me/capabilities their own tiers; a shared screen gets 403; delete-my-data covers the record.
Fictional people (Robin an adult, Ava a kid, a living-room screen); temporary stores; no model.
Run: python3 scripts/test_capability_audit.py"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-capability-audit-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "SPACES_DB": "spaces.db", "CONNECTORS_LEDGER": "ledger.db",
             "REFLECTION_DB": "reflection.db", "DAILY_LOG_DIR": "daily", "WORLD_DIR": "world",
             "ME_DELETE_DIR": "me-delete", "CLAUDE_CHATS_DB": "claude_chats.db",
             "CAPABILITY_SWITCHES": "switches.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "user"
for k in ("GEMINI_API_KEY", "TITAN_ALERT_NTFY_URL"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import capability_audit as A  # noqa: E402
import governance as G  # noqa: E402
import me_delete  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def as_person(pid, role="adult", source="user", client="mac"):
    lib._TURN_IO.person = {"id": pid, "name": pid}
    lib._TURN_IO.role = role
    lib._TURN_IO.source = source
    lib._TURN_IO.client = client
    lib._TURN_IO.capability_tiers = None


print("── the store ──")
A.record(person="p_alex", role="adult", surface="mac", source="user", tool="kb_retrieve", domain="knowledgeBase",
         risk="read", outcome="ok", ms=12)
A.record(person="p_alex", role="adult", surface="mac", source="eval", tool="kb_retrieve", domain="knowledgeBase",
         risk="read", outcome="ok")
A.record(person="p_alex", role="adult", surface="mac", source="canary", tool="kb_retrieve", domain="knowledgeBase",
         risk="read", outcome="ok")
A.record(person="p_alex", role="adult", surface="mac", source="user", tool="kb_retrieve", domain="knowledgeBase",
         risk="read", outcome="maybe")
rows = A.log("p_alex")
check("one row; eval, canary and an unknown outcome aren't recorded", len(rows), 1)
check("the row's fields", {k: rows[0][k] for k in ("tool", "domain", "risk", "outcome", "surface", "ms")},
      {"tool": "kb_retrieve", "domain": "knowledgeBase", "risk": "read", "outcome": "ok", "surface": "mac", "ms": 12})
check("no column could hold arguments or results", sorted(rows[0]),
      sorted(["ts", "role", "surface", "source", "tool", "domain", "risk", "outcome", "why", "ms"]))
A.record(person=None, role="admin", surface=None, source="user", tool="host_status", domain="system", risk="read",
         outcome="ok")
check("a turn without a person is recorded under system", len(A.log("system")), 1)
check("another person's are their own", A.log("p_robin"), [])
old = time.time() - 40 * 86400
con = A._db()
with con:
    con.execute("INSERT INTO capability_audit (ts, person, tool, outcome) VALUES (?,?,?,?)", (old, "p_alex", "web_search", "ok"))
con.close()
A._last_prune = 0.0
A.record(person="p_alex", role="adult", surface="mac", source="user", tool="get_datetime", domain="assistant",
         risk="read", outcome="ok")
check("rows older than 30 days are pruned", [r["tool"] for r in A.log("p_alex")], ["get_datetime", "kb_retrieve"])
check("since filters", [r["tool"] for r in A.log("p_alex", since=time.time() - 1)][:1], ["get_datetime"])
check("limit caps", len(A.log("p_alex", limit=1)), 1)
A.record(person="p_alex", role="adult", surface="mac", source="user", tool="x", domain=None, risk=None, outcome="ok",
         path=Path("/dev/null/nope.db"))
check("an unwritable store doesn't raise", True, True)

print("── in a turn ──")
G.set_tier("p_jordan", "web", "never")
as_person("p_jordan")
lib.execute_tool("web_search", {"query": "a secret question"})
lib.execute_tool("mystery_tool", {})
lib.execute_tool("get_datetime", {})
got = [(r["tool"], r["outcome"], r["why"]) for r in A.log("p_jordan")]
check("each call is a row: refused (and why) or ok", sorted(got),
      sorted([("web_search", "refused", "person_never"), ("mystery_tool", "refused", "no_descriptor"),
              ("get_datetime", "ok", None)]))
check("the arguments are nowhere in the store", b"secret question" in (_TMP / "spaces.db").read_bytes(), False)
check("the surface and role are kept", {(r["surface"], r["role"]) for r in A.log("p_jordan")}, {("mac", "adult")})
as_person("p_jordan", source="eval")
lib.execute_tool("get_datetime", {})
check("an eval turn isn't recorded", len(A.log("p_jordan")), 3)

print("── the routes ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role, platform="ios"):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    tok = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s device",
                                         "platform": platform}).json()["token"]
    return {"X-Agent-Token": tok}, next(p["id"] for p in st.people() if p["name"] == name)


ROBIN, robin = pair("Robin Hale", "adult")
AVA, ava = pair("Ava Hale", "kid")
TV, _ = pair("Living room", "household", "tvos")
A.record(person=robin, role="adult", surface="iphone", source="user", tool="search_mail", domain="mail", risk="read",
         outcome="ok")
A.record(person=ava, role="kid", surface="iphone", source="user", tool="kb_retrieve", domain="knowledgeBase",
         risk="read", outcome="ok")
r = c.get("/v1/me/capability-log", headers=ROBIN)
check("Robin reads their own log", (r.status_code, [e["tool"] for e in r.json()["entries"]], r.json()["keep_days"]),
      (200, ["search_mail"], 30))
check("Ava (a kid) reads their own, not Robin's", [e["tool"] for e in c.get("/v1/me/capability-log", headers=AVA).json()["entries"]],
      ["kb_retrieve"])
check("a shared screen gets 403", c.get("/v1/me/capability-log", headers=TV).status_code, 403)
check("a bad limit is 400", c.get("/v1/me/capability-log?limit=lots", headers=ROBIN).status_code, 400)
caps = c.get("/v1/me/capabilities", headers=ROBIN).json()
check("every domain is listed, on by default", (len(caps["domains"]), {d["tier"] for d in caps["domains"]}, caps["all_off"]),
      (len(G.T.DOMAINS), {"always"}, False))
r = c.put("/v1/me/capabilities", json={"mail": "never"}, headers=ROBIN)
check("Robin turns mail off for themselves", (r.status_code, next(d["tier"] for d in r.json()["domains"] if d["domain"] == "mail")),
      (200, "never"))
check("…which governance now sees", G.tiers(robin), {"mail": "never"})
check("Ava's are untouched", G.tiers(ava), {})
check("an unknown domain is 400 and writes nothing",
      (c.put("/v1/me/capabilities", json={"web": "never", "nope": "never"}, headers=ROBIN).status_code, G.tiers(robin)),
      (400, {"mail": "never"}))
check("an unknown tier is 400", c.put("/v1/me/capabilities", json={"web": "ask"}, headers=ROBIN).status_code, 400)
check("an empty body is 400", c.put("/v1/me/capabilities", json={}, headers=ROBIN).status_code, 400)
check("a shared screen can't set tiers", c.put("/v1/me/capabilities", json={"mail": "never"}, headers=TV).status_code, 403)

print("── how an executor's answer came out (#1631 slice 1) ──")
import cross_device as XD  # noqa: E402
E = A.executor_outcome
cases = [
    ("Device action was not completed: Declined by the user.", True, False, ("declined", "person")),
    (f"Device action was not completed: {XD.DECLINED}", True, False, ("declined", "person")),
    (XD.DECLINED, True, False, ("declined", "person")),                     # cross-device's completed: False sentence
    (f"Device action was not completed: {XD.NO_ANSWER}", True, False, ("timeout", "device")),
    ("Device action timed out after 120 s.", True, False, ("timeout", "device")),
    ("Device action was not completed: Confirmation timed out.", True, False, ("timeout", "device")),   # the bridge's own
    ("Device action was not completed: No result from the device.", True, False, ("timeout", "device")),
    (XD.NO_WAKE, True, False, ("unavailable", "no_wake")),
    ("No device is connected to this turn, so device actions can't run here.", True, False, ("unavailable", "no_device")),
    ("Device action was not completed: The confirmation expired before the user answered; nothing ran.", True, False,
     ("timeout", "person")),
    ("Device action was not completed: Not allowed: reading the screen is set to Never in Jellybean's Trust settings. "
     "Tell the user it's off in Trust; don't retry.", True, False, ("refused", "device_never")),     # the Mac's gate
    ("Not allowed: the keychain is off-limits to Jellybean. Tell the user; don't retry.", True, False,
     ("refused", "device_never")),                                     # an executor's own check: a plain result, was "ok"
    ("Device action was not completed: Couldn't read the file.", True, False, None),   # a plain failure: as before
    ("Found 3 reminders.", True, False, None),
    ("Not done: titan's executor isn't running (titan-exec.service, #1647).", False, True, ("unavailable", "executor")),
    ("Not done: no restarts inside an eval window or its lead (nightly).", False, True, ("refused", "executor_rule")),
    ("Error: The restart of titan-embeddings.service failed (exit 1).", False, True, None),
    ("Not done: that isn't a file.", False, False, None),                  # an ordinary tool: untouched
]
check("every executor's fixed answer maps to its outcome; anything else is left to the old rules",
      [E(t, device=d, executor=x) for t, d, x, _ in cases], [want for *_, want in cases])
check("every outcome named is one the record keeps", {w[0] for *_, w in cases if w} <= set(A.OUTCOMES), True)

as_person("p_robin")
before = len(A.log("p_robin", limit=1000))
lib._audit_call("set_alarm", "Device action was not completed: Declined by the user.", 5)
lib._audit_call("list_calendar_events", XD.NO_WAKE, 7)
lib._audit_call("set_alarm", "Device action was not completed: Couldn't set it.", 3)
rows = A.log("p_robin", limit=1000)[: len(A.log("p_robin", limit=1000)) - before]
check("recorded through the audit: declined, unavailable, and a plain failure still failed",
      [(r["tool"], r["outcome"], r["why"]) for r in reversed(rows)],
      [("set_alarm", "declined", "person"), ("list_calendar_events", "unavailable", "no_wake"), ("set_alarm", "failed", None)])

before = len(A.log("p_robin", limit=1000))
lib._TURN_IO.device_raw = ("create_calendar_event", f"Device action was not completed: {XD.NO_ANSWER}")
lib._audit_call("create_calendar_event", "Not confirmed: the event may not have been added.", 9)   # _calendar_after's words
lib._TURN_IO.device_raw = ("create_calendar_event", XD.NO_WAKE)
lib._audit_call("set_alarm", "Device action was not completed: Couldn't set it.", 2)   # another tool's answer: ignored
lib._TURN_IO.device = lambda req: {"error": "Confirmation timed out."}
lib._device_request("list_reminders", {})
lib._audit_call("list_reminders", "anything", 4)
lib._TURN_IO.device = None
rows = A.log("p_robin", limit=1000)[: len(A.log("p_robin", limit=1000)) - before]
check("the executor's own answer is what's classified, even when the calendar reworded it; never another call's",
      [(r["tool"], r["outcome"]) for r in reversed(rows)],
      [("create_calendar_event", "timeout"), ("set_alarm", "failed"), ("list_reminders", "timeout")])
check("…and the answer is used once", getattr(lib._TURN_IO, "device_raw", None), None)

print("── delete-my-data ──")
stores = me_delete.Parts(mem=None, spaces_db=_TMP / "spaces.db")._stores()
check("delete-my-data covers the record", [(p, t, col) for p, t, _, col in stores if t == "capability_audit"],
      [("capabilities", "capability_audit", "person")])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
