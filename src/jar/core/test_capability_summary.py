#!/usr/bin/env python3
# gate: hermetic
"""#355 (decided 10-02): a plain summary of what titan can do, for everyone; titan's system details stay the admin's.

Run: python3 scripts/test_capability_summary.py
"""
import os
import re
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="titan-capsum-test-")
os.environ["HOUSEHOLD_DB"] = os.path.join(_TMP, "household.db")
os.environ["AGENT_TURN_LOG"] = os.path.join(_TMP, "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_runtime_lib as lib  # noqa: E402
import capabilities as cap  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


SYSTEM = re.compile(r"\b(titan-|runtime|gpu|qwen|gemini|jev|llama|service|port \d|systemd|railway|tool|host|server)\b", re.I)
for role in ("adult", "kid", "household"):
    s = cap.plain_summary(role)
    check(f"{role}: a summary", s.startswith("What titan can do for "), True)
    check(f"{role}: no system details", SYSTEM.findall(s.replace("titan can do", "")), [])
    check(f"{role}: says what it won't do from a chat", "can't unlock doors" in s, True)
adult, kid = cap.plain_summary("adult"), cap.plain_summary("kid")
check("an adult can send messages; it says they approve each", "send messages for you (you approve each one)" in adult, True)
check("a kid's summary follows a kid's permissions (no messages)", "send messages" in kid, False)
check("a shared screen speaks to everyone there", cap.plain_summary("household").startswith("What titan can do for everyone at this screen"), True)

print("\n── when it's offered ──")
for q in ("What can you do?", "what else can you do for me", "How can you help?", "What can I ask you?"):
    check(f"asked: {q}", bool(lib._CAN_DO_RX.search(q)), True)
for q in ("Can you do the dishes?", "What's for dinner?"):
    check(f"not: {q}", bool(lib._CAN_DO_RX.search(q)), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
