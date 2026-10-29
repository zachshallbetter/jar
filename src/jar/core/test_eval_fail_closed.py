#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Eval turns never write, fail closed (10-05: four new write tools weren't simulated, and review_decide never was):
every titan-side tool that writes (its registry risk, the write and change lists, #952's alters-existing list) is
either simulated on an eval turn or listed as handling eval itself, and each listed one's handler checks the source.
A new write tool in neither fails here.
Run: python3 scripts/test_eval_fail_closed.py
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-evalfc-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as L  # noqa: E402
import scheduled  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


reg, device = L._TOOL_REGISTRY, set(L._DEVICE_TOOLS)
writes = ({n for n, s in reg.items() if s.get("risk") in ("write", "dangerous")} | set(L._ALTERS_EXISTING)
          | set(L._WRITE_TOOLS) | set(getattr(L, "_MORE_CHANGE_TOOLS", ())))
titan_side = sorted(w for w in writes if w in reg and w not in device)
check("every titan-side write is simulated on an eval turn or handles eval itself",
      [w for w in titan_side if w not in L._EVAL_SIMULATED and w not in L._EVAL_SELF_HANDLED], [])
check("…the two lists don't overlap", sorted(L._EVAL_SIMULATED & L._EVAL_SELF_HANDLED), [])
src = inspect.getsource(L)
for name in sorted(L._EVAL_SELF_HANDLED):
    if name in ("schedule_action", "cancel_scheduled"):
        body = inspect.getsource(scheduled.tool)
    elif name in ("add_todo", "complete_todo"):
        body = inspect.getsource(L._todo_tool)
    elif name == "todo_later":
        body = inspect.getsource(L._todo_later)
    else:
        i = src.find(f'if name == "{name}"', src.find("def _execute_tool("))
        body = src[i:i + 4000] if i >= 0 else ""
    check(f"{name}'s handler answers an eval turn itself", '"eval"' in body and "turn_source()" in body, True)
check("the 10-05 writes are simulated", all(t in L._EVAL_SIMULATED for t in
      ("mark_bills_paid", "mail_move_preview", "mail_move", "mail_move_undo", "review_decide")), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
