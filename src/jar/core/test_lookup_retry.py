#!/usr/bin/env python3
# gate: hermetic   (no model, no network: the local run is a fake)
"""A local reply that only promises a lookup is asked once more to make the call (#739).
Run: python3 scripts/test_lookup_retry.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


print("── what counts as a promised lookup ──")
for text in ("I'll check the travel time to the dentist.", "Let me look that up for you.", "I'll find out when it starts.",
             "I'm going to pull up your calendar.", "Let me see what's on tomorrow."):
    check(f"promise: {text}", bool(lib._LOOKUP_PROMISE_RX.search(text)), True)
for text in ("Leave by 8:30 to make the dentist at 9.", "I'll check back later.", "I'll check in with you tomorrow.",
             "You're free at 3.", "Which dentist do you mean?", "I checked: nothing is overdue."):
    check(f"not a promise: {text}", bool(lib._LOOKUP_PROMISE_RX.search(text)), False)

MSGS = [{"role": "user", "content": "When should I leave for the dentist?"}]


def turn(tools_used, outcomes=None):
    lib._TURN_IO.tools_used = set(tools_used)
    lib._TURN_IO.outcomes = list(outcomes or [])
    lib._TURN_IO.deadline = None
    lib._TURN_INFO.info = {}


print("\n── the retry ──")
calls = []


def run_calls_tool(m):
    calls.append(m)
    lib._TURN_IO.tools_used.add("travel_time")
    return "Leave by 8:30: it's 25 minutes away."


turn([])
got = lib._local_lookup_retry("I'll check the travel time to the dentist.", MSGS, run_calls_tool)
check("no tool called and a promise: asked once more, and the answer replaces it", got, "Leave by 8:30: it's 25 minutes away.")
check("…with the instruction to call the tool", calls[-1][-1]["content"], lib._DO_THE_LOOKUP)
check("…and the first reply as the assistant's turn", calls[-1][-2], {"role": "assistant", "content": "I'll check the travel time to the dentist."})
check("…counted in the turn log", (lib._TURN_INFO.info.get("lookup_retry"), lib._TURN_INFO.info.get("lookup_retry_called")), (1, 1))

calls.clear()
turn(["list_calendar_events"])
got = lib._local_lookup_retry("I'll check the travel time too.", MSGS, run_calls_tool)
check("a tool already ran this turn: left as it is", (got, len(calls)), ("I'll check the travel time too.", 0))

turn([])
got = lib._local_lookup_retry("Leave by 8:30.", MSGS, run_calls_tool)
check("an answer, not a promise: left as it is", (got, len(calls)), ("Leave by 8:30.", 0))

turn([], [{"tool": "create_reminder", "write": True, "ok": True}])
lib._TURN_IO.tools_used = set()
got = lib._local_lookup_retry("I'll check the travel time.", MSGS, run_calls_tool)
check("something was changed this turn: never asked again", (got, len(calls)), ("I'll check the travel time.", 0))

turn([])
got = lib._local_lookup_retry("I'll check the travel time.", MSGS, lambda m: "")
check("an empty second answer: the first stands", got, "I'll check the travel time.")


def boom(m):
    raise RuntimeError("out of time")


turn([])
got = lib._local_lookup_retry("I'll check the travel time.", MSGS, boom)
check("the second try fails: the first stands", got, "I'll check the travel time.")

turn([])
lib._TURN_IO.deadline = __import__("time").monotonic() + 5   # 5 s left
got = lib._local_lookup_retry("I'll check the travel time.", MSGS, run_calls_tool)
check("too little time left: left as it is", got, "I'll check the travel time.")
lib._TURN_IO.deadline = None

print("\n── an action promised, or a tool's name as prose (#1203, the box's turn_eval) ──")
for text in ("I'll draw a bean for you. One moment...", "I'll rename the captures by what's in them. Let me start by finding "
             "the files.", "Let me create that for you.", "I'm going to write the script now.", "One moment."):
    check(f"action promise: {text}", bool(lib._ACTION_PROMISE_RX.search(text)), True)
for text in ("I drew it: a green bean on a white plate.", "I made the list: three items.", "Dinner is at 6.",
             "Would you like me to create it?", "It's a moment of calm on the lake."):
    check(f"not an action promise: {text}", bool(lib._ACTION_PROMISE_RX.search(text)), False)
calls.clear()
turn([])
got = lib._local_lookup_retry("I'll draw a bean for you. One moment...", [{"role": "user", "content": "Draw a bean"}],
                              run_calls_tool)
check("an action promise with no tool: asked once more with the do-it instruction", (len(calls), calls[-1][-1]["content"]),
      (1, lib._DO_THE_ACTION))
check("…counted as an action retry", (lib._TURN_INFO.info.get("lookup_retry"), lib._TURN_INFO.info.get("action_retry")), (1, 1))
check("…the do-it instruction never says to claim it when they didn't ask", "answer again without promising" in lib._DO_THE_ACTION,
      True)
lib._turn_tool_specs = (lambda real: (lambda: {**real(), "mark_bills_paid": {"description": "x", "parameters": {}}}))(
    lib._turn_tool_specs)
check("a tool's name as prose is found (only a real tool, only a short reply)",
      (lib._prose_tool("Mark_bills_paid for Lumen Power."), lib._prose_tool("Paid_for it all, thanks."),
       lib._prose_tool("Mark_bills_paid " + "x" * 200)), ("mark_bills_paid", None, None))
calls.clear()
turn([])
got = lib._local_lookup_retry("Mark_bills_paid for Lumen Power.", [{"role": "user", "content": "All paid except Lumen"}],
                              run_calls_tool)
check("…asked once more to call it, with its entry", (len(calls), calls[-1][-1]["content"].startswith(
      "Your reply was the tool's name written as text, so nothing ran. Call mark_bills_paid now"),
      lib._TURN_INFO.info.get("prose_tool_retry")), (1, True, 1))

print("\n── wired into the local path ──")
src = Path(lib.__file__).read_text()
check("the local brain runs it before the claim retry (and the unbacked-claim retry after that, #893)",
      src.index("text = _local_lookup_retry(") < src.index("text = _local_claim_retry(text, messages")
      < src.index("return _local_unbacked_retry(text, messages"), True)

lib._TURN_IO.tools_used = lib._TURN_IO.outcomes = None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
