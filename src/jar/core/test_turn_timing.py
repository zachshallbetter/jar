#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#683: where a turn's time goes before the reply. The prompt build's stages, Gemini's call count and first call,
and the first call's prompt size by part reach the turn log; nothing carries over to a later turn. Hermetic: the
model is faked, the turn log is a temporary file.

  python3 scripts/test_turn_timing.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(tempfile.mkdtemp(), "agent-turns.jsonl")
os.environ["AGENT_TURN_LOG"] = LOG
os.environ["GEMINI_API_KEY"] = "test-key-not-real-0000000000"
os.environ["AGENT_TOOL_BRAIN"] = "gemini"

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


def last_log() -> dict:
    with open(LOG) as fh:
        return json.loads(fh.read().splitlines()[-1])


# --- The prompt build's timings reach the turn's log line, and only once ---------------------------------------
real_inner = lib._run_agent_turn_inner
lib._run_agent_turn_inner = lambda messages, *a, **k: "Your afternoon is free from one until four."
try:
    lib._TURN_IO.pre_timing = {"build_ms": 412, "recall_ms": 180, "skill_ms": 150, "toolprompt_ms": 3, "note": "x"}
    lib._run_agent_turn([{"role": "user", "content": "Am I free this afternoon?"}])
    rec = last_log()
    check("build stages logged", {k: rec.get(k) for k in ("build_ms", "recall_ms", "skill_ms", "toolprompt_ms")},
          {"build_ms": 412, "recall_ms": 180, "skill_ms": 150, "toolprompt_ms": 3})
    check("only timings are taken over (no other key)", "note" in rec, False)
    check("taken once: cleared for the next turn", getattr(lib._TURN_IO, "pre_timing", None), None)
    lib._run_agent_turn([{"role": "user", "content": "And tomorrow?"}])
    check("a later turn on the thread (a background turn) has none", "build_ms" in last_log(), False)
finally:
    lib._run_agent_turn_inner = real_inner

# --- Gemini: calls counted, the first call's time kept, the first prompt's size by part (sizes only) -----------
replies = [
    {"candidates": [{"content": {"parts": [{"functionCall": {"name": "list_calendar_events", "args": {}}}]}}]},
    {"candidates": [{"content": {"parts": [{"text": "You're free from one until four."}]}}]},
]
calls: list[int] = []
real_attempts, real_exec = lib._gemini_call_attempts, lib.execute_tool


def fake_attempts(body, model, timeout):
    calls.append(1)
    return replies[len(calls) - 1]


lib._gemini_call_attempts, lib.execute_tool = fake_attempts, lambda name, args: "1 PM–4 PM free"
try:
    lib._TURN_INFO.info = {}
    out = lib._run_gemini_agent([{"role": "system", "content": "You are titan."},
                                 {"role": "user", "content": "Am I free this afternoon?"}], max_rounds=2)
    info = lib._TURN_INFO.info
finally:
    lib._gemini_call_attempts, lib.execute_tool = real_attempts, real_exec
check("the answer is unchanged", out, "You're free from one until four.")
check("both Gemini calls counted", info.get("gemini_calls"), 2)
check("the first call's time kept (an int)", isinstance(info.get("gemini_first_ms"), int), True)
gp = info.get("gemini_prompt") or {}
check("the first prompt by part: system, tool declarations, messages", sorted(gp), ["messages", "system", "tools"])
check("… as sizes only (no names or text)", all(isinstance(v, int) and v > 0 for v in gp.values()), True)
lib._TURN_INFO.info = {"gemini_first_ms": 5, "gemini_prompt": {"system": 1, "tools": 1, "messages": 1}}
lib._gemini_call_attempts = lambda body, model, timeout: replies[1]
try:
    lib._run_gemini_agent([{"role": "user", "content": "again"}], max_rounds=1)   # a claim or degenerate retry
finally:
    lib._gemini_call_attempts = real_attempts
check("a second run in the turn keeps the first call's time and prompt", (lib._TURN_INFO.info["gemini_first_ms"],
      lib._TURN_INFO.info["gemini_prompt"]), (5, {"system": 1, "tools": 1, "messages": 1}))
check("… and counts its call", lib._TURN_INFO.info.get("gemini_calls"), 1)

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
