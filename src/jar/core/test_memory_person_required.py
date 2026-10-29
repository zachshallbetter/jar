#!/usr/bin/env python3
# gate: hermetic
"""Every per-person AgentMemory method needs person_id, by keyword (#1070): with no default, a caller that forgets it
fails loudly instead of reading or writing the admin's memory for whoever is asking. The list is read from the class
itself, so a new per-person method is covered the day it's added. A temporary memory; no model.
Run: python3 scripts/test_memory_person_required.py"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile

os.environ["AGENT_MEMORY_DIR"] = tempfile.mkdtemp(prefix="titan-mem-person-")
os.environ["MEMORY_HYBRID"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_memory  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


# What each method's other required arguments get (fictional values); person_id is never passed.
ARGS = {"conv_id": "c-test", "text": "Robin likes tea.", "texts": ["Robin likes tea."], "fact_id": "f-none",
        "pinned": True, "group": "food", "token": "fb-none", "query": "tea"}

INTERNAL = ("_noted", "_move_to_forgotten", "_restore_row")   # take the owner positionally from a caller that has it
per_person = []
for name, fn in inspect.getmembers(agent_memory.AgentMemory, inspect.isfunction):
    p = inspect.signature(fn).parameters.get("person_id")
    if p is None or name == "total_fact_count" or name in INTERNAL:   # total_fact_count: everyone's, unless asked
        continue
    per_person.append(name)
    check(f"{name}: person_id is keyword-only with no default", (p.kind, p.default),
          (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.empty))

print("── calling without person_id is refused ──")
mem = agent_memory.AgentMemory()
for name in per_person:
    sig = inspect.signature(getattr(agent_memory.AgentMemory, name))
    args = [ARGS.get(n, None) for n, p in list(sig.parameters.items())[1:]
            if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD and p.default is inspect.Parameter.empty]
    try:
        getattr(mem, name)(*args)
        check(f"{name}() without person_id", "ran", "TypeError")
    except TypeError as exc:
        check(f"{name}() without person_id", ("TypeError", "person_id" in str(exc)), ("TypeError", True))

check("the per-person methods #1070 named are all covered",
      sorted({"add_fact", "get_facts", "list_facts", "pinned_facts", "set_pinned", "_match_fact", "_fact_exists"}
             - set(per_person)), [])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
