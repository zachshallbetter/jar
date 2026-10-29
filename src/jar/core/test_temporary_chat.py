#!/usr/bin/env python3
# gate: hermetic
"""A temporary chat (jellybean#708, Zach 10-04): `source: "temporary"` on a turn. Adults only (a kid or a shared
device is refused with 403). Its actions are real (unlike an eval's, which are faked), but it leaves nothing behind:
no memory read in or written out, no facts, not in Past conversations, no search trace, nothing it teaches titan
(mentions, gaps, aliases), no recent outcomes carried into the person's next turn, no timeline note, and a turn-log
line with counts only. Every store is a temp file and the model is never called.
Run: python3 scripts/test_temporary_chat.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-temporary-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": str(_TMP / "turns.jsonl"),
             "SCHEDULED_DB": "scheduled.db", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v) if not v.startswith("/") else v
os.environ["MEMORY_HYBRID"] = "0"
os.environ.pop("AGENT_TURN_SOURCE", None)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import agent_api_server as S  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.300} expected={expected!r:.300}")


def who(role: str, pid: str = "robin") -> "hh.Identity":
    return hh.Identity({"id": pid, "name": pid.title(), "role": role}, None, role, "test")


print("── the request ──")
check("source temporary is read", S._parse_source({"source": "Temporary"}), "temporary")
check("eval still is (from the admin, W5); anything else is an ordinary turn", (S._parse_source({"source": "eval"}, S._hh.owner_identity("master")), S._parse_source({"source": "x"}),
                                                           S._parse_source({})), ("eval", None, None))
for role, refused in (("admin", False), ("adult", False), ("kid", True), ("household", True)):
    r = S._temporary_refused("temporary", who(role))
    check(f"{role}: {'refused' if refused else 'allowed'}", (r is not None, r.status_code if r else None), (refused, 403 if refused else None))
check("an ordinary turn is never refused here", S._temporary_refused(None, who("kid")), None)
check("eval and temporary leave no memory; an ordinary turn does",
      (S._leaves_no_memory({"source": "temporary"}), S._leaves_no_memory({"source": "eval"}), S._leaves_no_memory({"source": None}),
       S._leaves_no_memory(None)), (True, True, False, False))

print("── through the API: a kid's temporary turn never runs ──")
from fastapi.testclient import TestClient  # noqa: E402
ran: list = []
S._run_turn = lambda turn_id, *a, **k: ran.append(S._turns[turn_id].get("source")) or "ok"
real_resolve = S._resolve_identity
c = TestClient(S.app)
S._resolve_identity = lambda request: who("kid", "ava")
r = c.post("/v1/prompt", json={"prompt": "hi", "source": "temporary", "tools": False})
check("a kid: 403, and the turn never ran", (r.status_code, r.json().get("error"), ran),
      (403, "temporary chats are for adults on their own devices", []))
r = c.post("/v1/prompt/stream", json={"prompt": "hi", "source": "temporary", "tools": False})
check("…the streaming route too", r.status_code, 403)
S._resolve_identity = lambda request: who("household", "kitchen")
check("a shared device: 403", c.post("/v1/prompt", json={"prompt": "hi", "source": "temporary", "tools": False}).status_code, 403)
S._resolve_identity = lambda request: who("adult", "robin")
r = c.post("/v1/prompt", json={"prompt": "hi", "source": "temporary", "tools": False})
check("an adult: it runs, as a temporary turn", (r.status_code, ran), (200, ["temporary"]))
check("no refused turn is left behind in the turn table", all(t.get("source") != "temporary" or t.get("identity") is not None
                                                              for t in S._turns.values()), True)
S._resolve_identity = real_resolve

print("── memory: none read, none written ──")


class FakeMemory:
    def __init__(self):
        self.recalls, self.writes = 0, 0

    def recall(self, *a, **k):
        self.recalls += 1
        return type("R", (), {"prependContext": "FACT: Robin likes tea", "appendSystemContext": ""})()

    def __getattr__(self, name):   # any write path counts
        def w(*a, **k):
            self.writes += 1
        return w


mem = FakeMemory()
S._get_memory = lambda: mem
S._MEMORY_AVAILABLE = True
for tid, source in (("t-user", None), ("t-temp", "temporary")):
    S._init_turn(tid)
    S._turns[tid].update({"source": source, "identity": who("adult"), "remember": True})
msgs_user, _ = S._build_messages("what tea do I like", [], False, None, "t-user")
check("an ordinary turn reads memory (the control)", (mem.recalls, any("Robin likes tea" in m["content"] for m in msgs_user)), (1, True))
msgs_temp, _ = S._build_messages("what tea do I like", [], False, None, "t-temp")
check("a temporary turn doesn't", (mem.recalls, any("Robin likes tea" in m["content"] for m in msgs_temp)), (1, False))
S._remember_turn("t-temp", who("adult"), "q", "a", extract=True)
check("nothing is written to memory (so nothing reaches Past conversations)", mem.writes, 0)

print("── the runtime: real actions, no traces ──")
lib._TURN_IO.source = "temporary"
check("a temporary turn is not a real turn (it teaches titan nothing)", (lib.real_turn(), lib.temporary_turn()), (False, True))
check("no recent outcomes: not added, not read", lib._outcome_owner(), None)
logged: list = []
real_exec = lib.execute_tool
lib.execute_tool = lambda name, args: logged.append((name, args)) or "ok"
lib._mac_action_note = lambda name, args: "Opened Notes on the Mac"
lib._TURN_IO.device_completed = True
lib._log_mac_action("open_app", {"name": "Notes"}, "Opened Notes.")
check("no timeline note for what it did on the Mac", logged, [])
lib._TURN_IO.source = None
lib._log_mac_action("open_app", {"name": "Notes"}, "Opened Notes.")
check("…an ordinary turn's is noted (the control)", [n for n, _ in logged], ["log_event"])
lib.execute_tool = real_exec

print("── the turn log: counts only ──")
rec = {"ts": 1.5, "source": "temporary", "client": "mac", "mode": "gemini", "brain": "gemini", "route": "default",
       "ms": 812, "tools": 2, "tool_ms": 40, "tool_names": ["web_search", "fetch_url"], "egress": ["web_search"],
       "skills_read": ["travel"], "tool_fails": ["fetch_url: HTTP 403 at example.test"], "error": "TimeoutError: robin's query",
       "person": "robin", "role": "adult", "fallback": "gemini timed out", "context_sizes": {"history": 10}, "empty_reply": False}
out = lib.turn_log_counts(rec)
check("names, services, skills, failures and error words dropped", [k for k in ("tool_names", "egress", "skills_read", "tool_fails",
                                                                                "context_sizes") if k in out], [])
check("counts, timings, brain and whose kept; error and fallback as yes/no",
      {k: out.get(k) for k in ("ms", "tools", "tool_ms", "brain", "person", "role", "source", "error", "fallback", "empty_reply")},
      {"ms": 812, "tools": 2, "tool_ms": 40, "brain": "gemini", "person": "robin", "role": "adult", "source": "temporary",
       "error": True, "fallback": True, "empty_reply": False})
log = Path(os.environ["AGENT_TURN_LOG"])
lib._TURN_IO.source = "temporary"
lib._TURN_INFO.info = {"brain": "gemini", "tools": 1, "tool_names": ["web_search"], "egress": ["web_search"], "person": "robin"}
lib._TURN_INFO.route = {}
lib._log_turn(0.0, 4, None)
lib._TURN_IO.source = None
line = json.loads(log.read_text().splitlines()[-1])
check("the written line is the counts-only one", ("tool_names" in line, "egress" in line, line.get("tools"), line.get("source")),
      (False, False, 1, "temporary"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
