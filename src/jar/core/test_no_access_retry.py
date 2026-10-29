#!/usr/bin/env python3
# gate: hermetic   (no model, no stores: the retry with a fake run and the turn's declared tools)
"""A false "no access" (#1203, 10-07 box turn_eval: "I don't have access to Robin's text messages" with
search_messages declared that turn). The reply is asked once more, with the reader's entry, to read it and answer;
kept only when the reader ran. A real "no access" (no reader declared), a reply that read it, a turn that changed
something, or too little time: left as it is. Both brains. Fictional people.
Run: python3 scripts/test_no_access_retry.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-noaccess-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import claim_checks as C  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── what's a false 'no access' ──")
D = {"search_messages", "find_my_locate", "threads", "home_status", "search_notes"}
check("texts, with search_messages declared", C.false_no_access("I don't have access to Robin's text messages.", D, set()),
      [("messages", "search_messages")])
check("Find My, with find_my_locate declared",
      C.false_no_access("I don't have access to Ava's personal items or their location.", D, set()),
      [("find my", "find_my_locate")])
check("…'can't locate devices' too", C.false_no_access("I can't locate devices unless they're on Find My.", D, set()),
      [("find my", "find_my_locate")])
check("the house, with home_status declared", C.false_no_access("I can't check the thermostat right now.", D, set()),
      [("the house", "home_status")])
check("a real 'no access': no reader declared (email isn't here)",
      C.false_no_access("I don't have access to your email.", D, set()), [])
check("…or nothing titan has a reader for", C.false_no_access("I don't have access to your bank account.", D, set()), [])
check("the reader already ran: honest (it read and found nothing)",
      C.false_no_access("I can't see any texts from Robin about it.", D, {"search_messages"}), [])
check("no denial at all", C.false_no_access("Robin said pickup is at 3.", D, set()), [])

print("── the retry ──")
import agent_runtime_lib as lib  # noqa: E402
io = lib._TURN_IO
real_specs = lib._turn_tool_specs
lib._turn_tool_specs = lambda: {n: {"description": n, "parameters": {}} for n in D}
MSGS = [{"role": "user", "content": "What did Robin text me about pickup?"}]
calls = []


def turn(outcomes=None):
    io.outcomes = list(outcomes or [])
    io.tools_used = set()
    io.deadline = None
    lib._TURN_INFO.info = {}


def reads_texts(m):
    calls.append(m)
    io.outcomes.append({"tool": "search_messages", "ok": True, "write": False, "text": "Robin: pickup at 3"})
    return "Robin said pickup is at 3."


turn()
got = lib._no_access_retry("I don't have access to Robin's text messages.", MSGS, reads_texts)
check("a false 'no access': asked once more, read it, and the answer replaces it", got, "Robin said pickup is at 3.")
check("…the instruction names the reader and carries its entry",
      ("you have search_messages this turn" in calls[-1][-1]["content"], "search_messages" in calls[-1][-1]["content"]),
      (True, True))
check("…counted, and counted as fixed", (lib._TURN_INFO.info.get("no_access_retry"), lib._TURN_INFO.info.get(
      "no_access_retry_fixed")), (1, 1))
calls.clear()
turn()
got = lib._no_access_retry("I don't have access to Robin's text messages.", MSGS, lambda m: "Still can't, sorry.")
check("the second try still didn't read it: the first reply stands", got, "I don't have access to Robin's text messages.")
turn()
got = lib._no_access_retry("I don't have access to your email.", MSGS, reads_texts)
check("a real 'no access' (no reader declared): left as it is, nothing asked", (got, len(calls)),
      ("I don't have access to your email.", 0))
turn([{"tool": "add_todo", "ok": True, "write": True, "text": "added"}])
got = lib._no_access_retry("I don't have access to Robin's text messages.", MSGS, reads_texts)
check("something was changed this turn: never asked again", (got, len(calls)),
      ("I don't have access to Robin's text messages.", 0))
turn()
io.deadline = __import__("time").monotonic() + 3
got = lib._no_access_retry("I don't have access to Robin's text messages.", MSGS, reads_texts)
check("too little time left: left as it is", len(calls), 0)
io.deadline = None

print("── wired into both brains ──")
src = (Path(__file__).parent / "agent_runtime_lib.py").read_text()
check("the local brain runs it before the claim retry",
      src.index("text = _no_access_retry(text, messages") < src.index("text = _local_claim_retry(text, messages"), True)
check("Gemini runs it after the source retry", src.index("text = _gemini_source_retry(") < src.index(
      "text = _no_access_retry(text, gm"), True)

lib._turn_tool_specs = real_specs
io.outcomes = io.tools_used = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
