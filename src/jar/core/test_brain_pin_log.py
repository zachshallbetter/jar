#!/usr/bin/env python3
"""A turn pinned to a brain (an eval's eval_brain, a background turn) says so in the turn log (`pin`), so a
local-vs-Gemini comparison can keep exactly its own pinned turns: unpinned evals (the scorecard's hourly
consistency run, privacy_eval) are routed as usual and must not be read as a pin that leaked. Hermetic.

Run: python3 scripts/test_brain_pin_log.py
"""
# gate: hermetic
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-pin-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "REFLECTION_DB": "reflection.db", "PROACTIVE_DB": "proactive.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "eval"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


lib._run_local_agent = lambda *a, **k: "Local answer."
lib._with_situation = lambda m: m
lib._with_schedule_digest = lambda m: m


def last():
    return json.loads((_TMP / "turns.jsonl").read_text().splitlines()[-1])


lib._TURN_IO.brain = "local"
lib._run_agent_turn([{"role": "user", "content": "What's 2+2?"}])
check("a pinned turn logs its pin and the brain that answered", (last().get("pin"), last().get("brain")), ("local", "local"))
lib._TURN_IO.brain = None
os.environ["AGENT_TOOL_BRAIN"] = "local"
lib._run_agent_turn([{"role": "user", "content": "What's 2+2?"}])
check("an unpinned turn logs no pin", "pin" in last(), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
