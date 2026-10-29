#!/usr/bin/env python3
"""A turn the router sends to the local model never dies on its context (10-01, 11:15 PM: a long
iPhone conversation reached 16,612 tokens against the local model's 16,384; twelve turns failed with
"HTTP Error 400" because nothing fell back to Gemini).

Run: python3 scripts/test_local_overflow.py
"""
import os
import sys
import tempfile
import urllib.error
sys.path.insert(0, os.path.dirname(__file__))
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


print("\n── the local model's context ──")
sysmsg = {"role": "system", "content": "s" * 30000}
# Enough turns to overflow whatever context titan runs (16k until 10-02, 32k pooled since #625).
_turns = int(lib.LOCAL_CTX_TOKENS * lib._CHARS_PER_TOKEN / 4000) + 4
history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "x" * 4000} for i in range(_turns)]
last = {"role": "user", "content": "What would you like to have that could improve your abilities?"}
fit = lib._fit_local_context([sysmsg, *history, last], 512)
size = sum(len(m["content"]) for m in fit) / lib._CHARS_PER_TOKEN
check("a long conversation is trimmed to fit", size <= lib.LOCAL_CTX_TOKENS - 512, True)
check("the system prompt and the question stay", (fit[0] is sysmsg, fit[-1] is last), (True, True))
check("the oldest turns go first", fit[1]["content"].startswith("turn 0"), False)
short = [sysmsg, history[0], last]
check("a short one is untouched", lib._fit_local_context(short, 512), short)

print("\n── local fails → Gemini (only when the router chose local) ──")
orig = (lib._run_local_agent, lib._run_gemini_agent, lib._classify_turn)
calls = []


def local_400(*a, **k):
    calls.append("local")
    raise urllib.error.HTTPError("http://127.0.0.1:8010", 400, "Bad Request", {}, None)


lib._run_local_agent = local_400
lib._run_gemini_agent = lambda *a, **k: calls.append("gemini") or "Answer from Gemini."
lib._classify_turn = lambda messages: "local"
os.environ["GEMINI_API_KEY"] = "test-key"
msgs = [{"role": "user", "content": "hi"}]
try:
    os.environ["AGENT_TOOL_BRAIN"] = "auto"
    out = lib._run_agent_turn_inner(msgs)
    check("auto-routed local 400 → Gemini answers", (out, calls), ("Answer from Gemini.", ["local", "gemini"]))
    check("… and the turn says so", (lib._TURN_INFO.info.get("brain"), lib._TURN_INFO.info.get("fallback", "")[:12]),
          ("gemini", "local failed"))
    calls.clear()
    lib._TURN_IO.brain = "local"   # a background turn: household data stays on titan
    try:
        lib._run_agent_turn_inner(msgs)
    except urllib.error.HTTPError:
        pass
    finally:
        lib._TURN_IO.brain = None
    check("a pinned local turn never goes to Gemini", calls, ["local"])
    # The owner's review: Gemini failed first, then local failed: raise, never a second Gemini try.
    calls.clear()

    def gemini_down(*a, **k):
        calls.append("gemini")
        raise RuntimeError("gemini 503")
    lib._run_gemini_agent = gemini_down
    lib._classify_turn = lambda messages: "gemini"
    try:
        lib._run_agent_turn_inner(msgs)
        check("Gemini failed, then local failed: the turn fails", "answered", "raised")
    except urllib.error.HTTPError:
        check("Gemini failed, then local failed: Gemini tried once", calls, ["gemini", "local"])
    # A local turn that already made a change never reruns on Gemini (it would happen twice).
    calls.clear()
    lib._run_gemini_agent = lambda *a, **k: calls.append("gemini") or "again"
    lib._classify_turn = lambda messages: "local"

    def local_wrote_then_400(*a, **k):
        calls.append("local")
        lib._TURN_IO.outcomes = [{"tool": "create_reminder", "write": True, "ok": True, "text": "Created"}]
        raise urllib.error.HTTPError("http://127.0.0.1:8010", 400, "Bad Request", {}, None)
    lib._run_local_agent = local_wrote_then_400
    try:
        lib._run_agent_turn_inner(msgs)
    except urllib.error.HTTPError:
        pass
    finally:
        lib._TURN_IO.outcomes = []
    check("a local turn that already wrote isn't rerun on Gemini", calls, ["local"])
finally:
    lib._run_local_agent, lib._run_gemini_agent, lib._classify_turn = orig
    os.environ.pop("AGENT_TOOL_BRAIN", None)
    os.environ.pop("GEMINI_API_KEY", None)

print("\n── titan's own abilities aren't small talk ──")
for q in ("What would you like to have that could improve your abilities? Knowledge, responses, considerations?",
          "What could you need to do better?"):
    check(f"hard pattern: {q[:40]!r}", lib._HARD_RE.search(q) is not None, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
