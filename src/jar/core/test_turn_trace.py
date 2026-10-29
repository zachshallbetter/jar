#!/usr/bin/env python3
# gate: hermetic   (temp databases and a temp turn log; touches nothing live)
"""jellybean#1267 part 1: a conversation's turns for the developer inspector. The turn log names each turn's
conversation (`thread`), each tool call's time and which call failed; turn_trace shapes one person's lines for one
conversation; GET /v1/conversations/{cid}/trace is admin-only and shows a viewer their own turns and nobody else's.
Hermetic: temp stores, a temp turn log, the model and tools faked.

  TZ=UTC python3 scripts/test_turn_trace.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TMP = Path(tempfile.mkdtemp(prefix="titan-turn-trace-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "agent-turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db", "PUSH_DB": "push.db",
             "SPACES_DB": "spaces.db"}.items():
    os.environ[k] = str(_TMP / v)
LOG = os.environ["AGENT_TURN_LOG"]
os.environ["MEMORY_HYBRID"] = "0"
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ["AGENT_TOOL_BRAIN"] = "gemini"
os.environ.pop("GEMINI_API_KEY", None)
os.environ.pop("TITAN_DISCOVERY", None)
ENG_FILE = _TMP / "engineering_token"
ENG_FILE.write_text("engineering-test-token-0123456789")
os.environ["AGENT_ENGINEERING_TOKEN_FILE"] = str(ENG_FILE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runtime_lib as lib  # noqa: E402
import turn_trace  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.300} expected={expected!r:.300}")


def log_lines() -> list[dict]:
    with open(LOG) as fh:
        return [json.loads(x) for x in fh.read().splitlines() if x.strip()]


# A pinned household clock: 2026-10-12 09:30 Pacific.
T0 = datetime(2026, 10, 12, 9, 30, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()

print("── the writer: thread, per-call times and which call failed ──")
calls = iter(["Sunny, 18°C.", "Error: no forecast for \"Hale Street\" on day 3"])
real_exec, real_inner = lib._execute_tool, lib._run_agent_turn_inner
lib._execute_tool = lambda name, args: next(calls)


def inner(messages, *a, **k):
    lib.execute_tool("get_weather", {"place": "Hale Street"})
    lib.execute_tool("get_weather", {"place": "Hale Street", "day": 3})
    return "Sunny today."


lib._run_agent_turn_inner = inner
try:
    lib._TURN_IO.turn_id, lib._TURN_IO.thread = "t-writer-1", "th-alex-weather"
    lib._TURN_IO.person = {"id": "p_alex", "name": "Alex"}
    lib._run_agent_turn([{"role": "user", "content": "Weather on Hale Street?"}])
    rec = log_lines()[-1]
    check("the line names its conversation and turn", (rec.get("thread"), rec.get("turn_id"), rec.get("person")),
          ("th-alex-weather", "t-writer-1", "p_alex"))
    check("tools in call order", rec.get("tool_names"), ["get_weather", "get_weather"])
    check("a time per call, aligned with tool_names", (len(rec.get("tool_times") or []),
                                                       all(isinstance(x, int) for x in rec.get("tool_times") or [])),
          (2, True))
    check("the second call is the one that failed", rec.get("tool_fail_at"), [1])
    check("the failure head carries no quoted value or digits", rec["tool_fails"][0][1].count("Hale"), 0)
    lib._TURN_IO.thread = None
    lib._run_agent_turn_inner = lambda messages, *a, **k: "Still sunny."
    lib._run_agent_turn([{"role": "user", "content": "And now?"}])
    check("a turn with no conversation id logs none", "thread" in log_lines()[-1], False)
finally:
    lib._execute_tool, lib._run_agent_turn_inner = real_exec, real_inner
    lib._TURN_IO.turn_id = lib._TURN_IO.thread = lib._TURN_IO.person = None

print("\n── shaping one line ──")
REC = {"ts": T0, "source": "user", "client": "mac", "mode": "gemini", "jev_router": "1", "route": "jev-frontier",
       "jev": {"choice": "frontier", "sure": 0.91, "needs_tools": 0.8, "ms": 310}, "brain": "gemini", "fallback": None,
       "ms": 241000, "error": None, "turn_id": "t-a1", "thread": "th-alex", "person": "p_alex",
       "context_ms": 900, "build_ms": 700, "model_ms": 3100, "tool_ms": 236000, "device_ms": 400, "confirm_ms": 235000,
       "asked_ms": 230000, "device_calls": 2, "tools": 3,
       "tool_names": ["list_calendar_events", "run_shell", "send_message"], "tool_times": [350, 400, 235200],
       "tool_fails": [["send_message", "Device action timed out"]], "tool_fail_at": [2],
       "device_times": [["run_shell", 400], ["send_message", 235000, 230000]],
       "tool_devices": [["run_shell", "mac"], ["send_message", "iphone"]],
       "effort": "high", "effort_why": "asked", "self_check": "revised", "self_check_ms": 800,
       "postanswer": {"self_check": 1}, "postanswer_skipped": {"claim_fix": 1},
       "source_retry": 1, "source_retry_fixed": 1, "claim_retry": 1, "egress": ["gemini", "jev"]}
t = turn_trace.shape(REC)
check("time: epoch and ISO Z", (t["ts"], t["at"]), (T0, "2026-10-12T16:30:00.000Z"))
check("route, Jev, brain, effort", (t["route"], t["jev"], t["brain"], t["effort"]),
      ("jev-frontier", {"choice": "frontier", "sure": 0.91, "needs_tools": 0.8, "ms": 310}, "gemini", "high"))
check("tools in order with ok, time and device", [(s["name"], s["ok"], s.get("ms"), s.get("device")) for s in t["tools"]],
      [("list_calendar_events", True, 350, None), ("run_shell", True, 400, "mac"),
       ("send_message", False, 235200, "iphone")])
check("the failure head is its reason", t["tools"][2]["reason"], "Device action timed out")
check("the confirmation that timed out is a wait", t["device_waits"],
      [{"step": 2, "tool": "send_message", "ended": "timed_out", "ms": 235000, "asked_ms": 230000}])
check("checks and retries", t["checks"], {"self_check": "revised", "postanswer": {"self_check": 1},
                                          "postanswer_skipped": {"claim_fix": 1},
                                          "retries": {"source_retry": 1, "source_retry_fixed": 1, "claim_retry": 1}})
check("egress", t["egress"], ["gemini", "jev"])
check("the ms split", t["ms"], {"total": 241000, "build": 700, "context": 900, "model": 3100, "tools": 236000,
                                "device": 400, "confirm": 235000, "asked": 230000, "self_check": 800})
check("an error's words never pass; only that it failed",
      turn_trace.shape({**REC, "error": "RuntimeError: Robin's code 4471"})["failed"], True)
check("…and no field holds them", "4471" in json.dumps(turn_trace.shape({**REC, "error": "Robin's code 4471"})), False)
old = {k: v for k, v in REC.items() if k not in ("tool_times", "tool_fail_at")}
check("a line from before tool_times/tool_fail_at: failures matched by name",
      [(s["name"], s["ok"], "ms" in s) for s in turn_trace.shape(old)["tools"]],
      [("list_calendar_events", True, False), ("run_shell", True, False), ("send_message", False, False)])
check("a label that isn't a plain label is dropped", turn_trace.shape({**REC, "route": "x\n{\"a\": 1}"})["route"], None)

print("\n── one person's lines, one conversation ──")
Path(LOG).write_text("")
lines = [
    {**REC, "ts": T0 + 60, "turn_id": "t-a2", "thread": "th-alex"},
    REC,
    {**REC, "ts": T0 + 30, "turn_id": "t-r1", "thread": "th-alex", "person": "p_robin"},   # Robin's, same id
    {**REC, "ts": T0 + 40, "turn_id": "t-r2", "thread": "th-robin", "person": "p_robin"},
    {**REC, "ts": T0 + 50, "turn_id": "t-a3", "thread": "th-other"},
    {"ts": T0 + 70, "source": "user", "person": "p_alex", "ms": 900, "brain": "gemini", "tools": 0},   # temporary: no ids
]
with open(LOG, "w") as fh:
    for x in lines:
        fh.write(json.dumps(x) + "\n")
    fh.write("not json\n")
got, more = turn_trace.for_conversation(LOG, "p_alex", "th-alex")
check("Alex: their own turns in th-alex, oldest first", ([x["turn_id"] for x in got], more), (["t-a1", "t-a2"], False))
got, _ = turn_trace.for_conversation(LOG, "p_robin", "th-alex")
check("Robin asking for the same id sees only Robin's line", [x["turn_id"] for x in got], ["t-r1"])
check("Robin can't read Alex's other conversation", turn_trace.for_conversation(LOG, "p_robin", "th-other"), ([], False))
got, more = turn_trace.for_conversation(LOG, "p_alex", "th-alex", limit=1)
check("limit keeps the newest and says more were left", ([x["turn_id"] for x in got], more), (["t-a2"], True))
check("no person, nothing", turn_trace.for_conversation(LOG, "", "th-alex"), ([], False))
check("a missing log, nothing", turn_trace.for_conversation(str(_TMP / "absent.jsonl"), "p_alex", "th-alex"), ([], False))

print("\n── the route: admin only, and each viewer only their own ──")
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "api-household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def pair(name, role):
    s_ = c.post("/v1/pair/start", json={"new_person": {"name": name, "role": role}}, headers=M).json()
    got_ = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": f"{name}'s Mac",
                                          "platform": "mac"}).json()
    return {"X-Agent-Token": got_["token"]}, got_["person"]["id"]


ALEX, alex_id = pair("Alex", "admin")
ROBIN, robin_id = pair("Robin", "admin")
JORDAN, _ = pair("Jordan", "adult")
with open(LOG, "w") as fh:
    for x in ({**REC, "person": alex_id, "thread": "th-alex", "turn_id": "t-a1"},
              {**REC, "ts": T0 + 60, "person": alex_id, "thread": "th-alex", "turn_id": "t-a2"},
              {**REC, "ts": T0 + 30, "person": robin_id, "thread": "th-alex", "turn_id": "t-r1"},
              {**REC, "ts": T0 + 40, "person": robin_id, "thread": "th-robin", "turn_id": "t-r2"}):
        fh.write(json.dumps(x) + "\n")

r = c.get("/v1/conversations/th-alex/trace", headers=ALEX)
check("Alex reads their conversation", (r.status_code, [x["turn_id"] for x in r.json().get("turns", [])]),
      (200, ["t-a1", "t-a2"]))
check("…shaped, no message text or raw line", (set(r.json()), "tool_fails" in r.text, "person" in r.json()["turns"][0]),
      ({"id", "turns", "truncated"}, False, False))
r = c.get("/v1/conversations/th-robin/trace", headers=ALEX)
check("Alex can't read Robin's conversation", r.status_code, 404)
r = c.get("/v1/conversations/th-alex/trace", headers=ROBIN)
check("Robin asking for Alex's id sees only Robin's own turn", [x["turn_id"] for x in r.json()["turns"]], ["t-r1"])
check("Robin reads their own", [x["turn_id"] for x in c.get("/v1/conversations/th-robin/trace", headers=ROBIN).json()["turns"]],
      ["t-r2"])
check("an adult can't call it", c.get("/v1/conversations/th-alex/trace", headers=JORDAN).status_code, 403)
check("the engineering token can't call it",
      c.get("/v1/conversations/th-alex/trace", headers={"X-Agent-Token": ENG_FILE.read_text()}).status_code, 403)
check("no token: 401", c.get("/v1/conversations/th-alex/trace").status_code, 401)
check("limit is honoured", [x["turn_id"] for x in c.get("/v1/conversations/th-alex/trace?limit=1",
                                                          headers=ALEX).json()["turns"]], ["t-a2"])
# A conversation Alex has (stored messages) but no turn of theirs names yet: 200 with no turns, not a 404.
mem = api._get_memory()
mem.add_message(hh.conv_id_for(alex_id), "user", "Plan a picnic", thread="th-alex-quiet")
r = c.get("/v1/conversations/th-alex-quiet/trace", headers=ALEX)
check("their stored conversation with no logged turns: empty", (r.status_code, r.json().get("turns")), (200, []))
check("…which Robin still can't see", c.get("/v1/conversations/th-alex-quiet/trace", headers=ROBIN).status_code, 404)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
