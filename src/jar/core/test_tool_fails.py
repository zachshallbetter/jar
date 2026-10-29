#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Which tools fail most (titan-node#651): a tool result that starts "Error", "Not run", "Not created", "Not done"
(and the other refusals) puts [tool, its first 40 characters] in the turn log's tool_fails, with quoted text and
numbers taken out first; the scorecard and turn_review rank them. Nothing else of the result is kept.
Run: python3 scripts/test_tool_fails.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-toolfails-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402
import scorecard as sc  # noqa: E402
import turn_review as tr  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


H = lib._failure_head
print("── the first words, never what it was about ──")
check("a list refusal: the shape, the name taken out", H("Not created: the list “Robin's” doesn't exist."),
      "Not created: the list … doesn't exist.")
check("apostrophes aren't quotes", H("Not run: it's not on Ann's list"), "Not run: it's not on Ann's list")
check("numbers taken out", H("Error: HTTP 404 for /v1/items/123"), "Error: HTTP N for /vN/items/N")
check("…and plain quotes", H('Error: no file "secret plan.txt" here'), "Error: no file … here")
check("at most 40 characters", len(H("Device action was not completed: The folder doesn't exist at all")), 40)
for ok in ("Added “Milk” to Reminders.", "• Fri Oct 9 — Dentist", "", None, "No events today.", "Nothing is scheduled."):
    check(f"not a failure: {ok!r}", H(ok), None)

print("── into the turn log ──")
lib.begin_turn_info()
real = lib._execute_tool_checked
lib._execute_tool_checked = lambda name, args: {"create_reminder": "Not created: the list “Family” doesn't exist.",
                                                "list_todos": "• Milk"}[name]
lib.execute_tool("create_reminder", {"title": "Call Ann at 555-0100"})
lib.execute_tool("list_todos", {})
info = lib._TURN_INFO.info
check("a failed result: [tool, first words]; a good one: nothing", info.get("tool_fails"),
      [["create_reminder", "Not created: the list … doesn't exist."]])
check("…the person's words never reach it", "555" in str(info) or "Ann" in str(info), False)
lib._execute_tool_checked = real
check("tool_fails is one of the logged keys", '"tool_fails"' in Path(lib.__file__).read_text(), True)

print("── ranked ──")
turns = ([{"source": "user", "ms": 3000, "tool_fails": [["create_reminder", "Not created: the list … doesn't exist."]]}] * 4
         + [{"source": "user", "ms": 3000, "tool_fails": [["run_shell", "Error: command not found: N"]]}] * 2
         + [{"source": "eval", "ms": 3000, "tool_fails": [["web_search", "Error: x"]]}] * 9
         + [{"source": "user", "ms": 3000}] * 10)
got = sc.service(6, turns)["failing_tools"]
check("scorecard: per tool, most first, real turns only", got, {
    "create_reminder": {"n": 4, "most": "Not created: the list … doesn't exist."},
    "run_shell": {"n": 2, "most": "Error: command not found: N"}})
ts = tr.turn_stats(turns)
check("turn_review counts them per tool", {k: sum(v.values()) for k, v in ts["tool_fails"].items()},
      {"create_reminder": 4, "run_shell": 2, "web_search": 9})
claims: list[str] = []
md = tr.render(7, ts, tr.word_signals([], []), tr.memory_sample([]), claims)
check("the weekly review: a candidate per tool failing 3+ times, with its most common words",
      [c for c in claims if "failed" in c and "time(s)" in c],
      ["`web_search` failed 9 time(s); most often “Error: x”.",
       "`create_reminder` failed 4 time(s); most often “Not created: the list … doesn't exist.”."])
import json as _json  # noqa: E402
import time as _time  # noqa: E402
log = _TMP / "summary-turns.jsonl"
log.write_text("".join(_json.dumps({**t, "ts": _time.time() - 60}) + "\n" for t in turns if t["source"] == "user"))
check("…the daily summary names them, most first",
      "Tools that failed most: create_reminder 4×; run_shell 2×" in tr.recent_summary(24, path=log), True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
