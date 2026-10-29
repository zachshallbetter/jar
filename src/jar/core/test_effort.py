#!/usr/bin/env python3
# gate: hermetic
"""Think harder (#876): a turn's effort decided in code (asked, or auto for money, schedules, several steps or
questions); high = Gemini thinking high for the turn plus one self-check pass that keeps the draft on "OK" and replaces
it otherwise, with read-only tools; logged. Gemini is faked.
Run: python3 scripts/test_effort.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-effort-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db", "CONNECTORS_LEDGER": "ledger.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_GEMINI_THINKING"] = "low"
os.environ["AGENT_EFFORT_AUTO"] = "1"
sys.path.insert(0, str(Path(__file__).parent))
import effort  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


print("── deciding ──")
cases = {
    "When is Ava's ballet?": "normal",
    "What's the wifi password?": "normal",
    "How much did we spend on groceries in September?": "high",
    "Split the $84 dinner between Robin, Jordan and me.": "high",
    "Can we fit a dentist visit in this week and next week?": "high",
    "Is there a conflict with Leo's game on Saturday?": "high",
    "What's on Friday? And who picks up Leo?": "high",
    "Which is cheaper, the annual plan or monthly?": "high",
}
check("auto: money, schedules, several questions or steps go high; lookups stay normal",
      {q: effort.decide(q)[0] for q in cases}, cases)
check("asked high wins over an everyday question", effort.decide("What's the wifi?", "high"), ("high", "asked: high"))
check("asked normal wins over a hard one", effort.decide("How much do we owe?", "normal"), ("normal", "asked: normal"))
check("the model's hint can lower an auto raise", effort.decide("How much do we owe?", None, hint="normal")[0], "normal")
check("…but never raise an everyday question", effort.decide("What's the wifi?", None, hint="high"), ("normal", "auto: everyday"))
check("the why is logged words", effort.decide("How much do we owe?")[1], "auto: money")
os.environ["AGENT_EFFORT_AUTO"] = ""
check("auto off: a hard question stays normal, and says why", effort.decide("How much do we owe?"),
      ("normal", "auto: money (auto off)"))
check("auto off: an explicit high still works", effort.decide("What's the wifi?", "high")[0], "high")
os.environ["AGENT_EFFORT_AUTO"] = "1"

print("── what high does ──")
import agent_runtime_lib as lib  # noqa: E402
lib._TURN_IO.effort_level = None
check("normal: the configured level (low)", lib._gemini_thinking_level(), "low")
lib._TURN_IO.effort_level = "high"
check("high: thinking high for this turn", lib._gemini_thinking_level(), "high")
lib._TURN_IO.effort_level = None

calls = []
real = lib._run_gemini_agent


def fake(answer):
    def run(messages, max_rounds=6, model=None, allowed_tools=None, force_tools=None):
        calls.append({"last": messages[-1]["content"], "draft": messages[-2]["content"], "tools": allowed_tools,
                      "rounds": max_rounds, "roles": [m["role"] for m in messages if m["role"] != "system"]})
        return answer
    return run


gm = [{"role": "user", "content": "How much did we spend on groceries in September?"}]
try:
    lib._TURN_INFO.info = {"tool_names": ["find_records", "create_reminder", "kb_retrieve", "file_write"]}
    lib._run_gemini_agent = fake("OK")
    info = {}
    check("the check agrees: the draft stands", lib._self_checked("You spent $412.", gm, info), "You spent $412.")
    check("…logged ok, with its time", (info["self_check"], "self_check_ms" in info), ("ok", True))
    check("the check sees the draft and the instruction", (calls[-1]["draft"], calls[-1]["last"].startswith("Before this reply")),
          ("You spent $412.", True))
    check("…as the person's turn: a Gemini request may not end on the model's (HTTP 400, 10-04)",
          (calls[-1]["roles"][-2:]), ["assistant", "user"])
    check("the check may read, never write", (sorted(calls[-1]["tools"] & {"find_records", "kb_retrieve"}),
                                              calls[-1]["tools"] & {"create_reminder", "file_write"}),
          (["find_records", "kb_retrieve"], set()))
    lib._run_gemini_agent = fake("You spent $421 in September (receipts total $421, not $412).")
    info = {}
    check("the check finds an error: its correction replaces the draft",
          (lib._self_checked("You spent $412.", gm, info), info["self_check"]),
          ("You spent $421 in September (receipts total $421, not $412).", "revised"))

    def boom(*a, **k):
        raise RuntimeError("HTTP 503")
    lib._run_gemini_agent = boom
    info = {}
    check("the check fails: the draft stands", (lib._self_checked("You spent $412.", gm, info), info["self_check"]),
          ("You spent $412.", "failed"))
    lib._run_gemini_agent = fake("")
    info = {}
    check("an empty check: the draft stands", (lib._self_checked("You spent $412.", gm, info), info["self_check"]),
          ("You spent $412.", "ok"))
    info = {}
    check("an empty draft isn't checked", (lib._self_checked("", gm, info), info["self_check"]), ("", "skipped"))
    # Through the real request builder (only the HTTP call faked): the body Gemini gets ends on a user turn.
    lib._run_gemini_agent = real
    bodies = []
    real_call = lib._gemini_call

    def capture(body, model=None, timeout=None):
        bodies.append(json.loads(json.dumps(body)))
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}}]}
    lib._gemini_call = capture
    try:
        info = {}
        out = lib._self_checked("You spent $412.", [{"role": "system", "content": "Be brief."}] + gm, info)
    finally:
        lib._gemini_call = real_call
    check("the real Gemini body ends on the person's turn, and the check passes",
          (bodies and bodies[0]["contents"][-1]["role"], out, info["self_check"]), ("user", "You spent $412.", "ok"))
finally:
    lib._run_gemini_agent = real
    lib._TURN_INFO.info = None

print("── the request ──")
import agent_api_server as api  # noqa: E402
check("effort parsed: high, normal, else titan decides",
      [api._parse_effort(b) for b in ({"effort": "high"}, {"effort": "Normal"}, {"effort": "max"}, {})],
      ["high", "normal", None, None])
check("the turn log keeps effort, why and the check", all(k in open(lib.__file__).read() for k in
      ('"effort", "effort_why", "self_check", "self_check_ms"',)), True)

print("── the scorecard ──")
import scorecard  # noqa: E402
rows = ([{"source": "user", "ms": 2000, "effort": "high", "self_check": "revised"}] +
        [{"source": "user", "ms": 2000, "effort": "high", "self_check": "ok"}] * 3 + [{"source": "user", "ms": 1500}] * 6)
r = scorecard.service(24, rows)
check("revised rate over checked high-effort turns", (r["effort_high"], r["self_checked"], r["self_revised_pct"]), (4, 4, 25.0))
check("…in the service line", "thought harder on 40.0% (self-check revised 25.0% of 4)" in scorecard._summary("service", r),
      True)
r = scorecard.service(24, [{"source": "user", "ms": 2000, "effort": "high", "self_check": "ok"}] * scorecard.SELF_CHECK_MIN)
check("40 checks, none revised: the note to turn auto off", any("AGENT_EFFORT_AUTO" in m for m in r["misses"]), True)

print("── 10-10: 'total' and 'sum' are money only near a money word ──")
check("the canary's 'total number of letters' isn't money (it paid a self-check on every run since 10-04)",
      effort._why_auto("Use the count_chars tool on the text 'canary bird' and reply with only the total number of letters."),
      None)
check("…nor 'sum these numbers'", effort._why_auto("sum these numbers: 3, 4"), None)
check("…but 'the total I spent' is", effort._why_auto("what's the total I spent on groceries"), "auto: money")
check("…and 'sum of the bills' is", effort._why_auto("sum of the bills this month"), "auto: money")
check("…and 'the total cost' is", effort._why_auto("what's the total cost"), "auto: money")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
