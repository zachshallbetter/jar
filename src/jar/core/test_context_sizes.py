#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""#770: the turn log records what the model was given by section and size only (context_sizes). Section names come
from a fixed list mapped from each block's marker in code. No header text, no content, ever: a calendar name, a skill
guide's name, an unknown block's header all stay out of the log.
Run: python3 scripts/test_context_sizes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

LOG = Path(tempfile.mkdtemp()) / "turns.jsonl"
os.environ["AGENT_TURN_LOG"] = str(LOG)
os.environ["HOUSEHOLD_DB"] = str(Path(tempfile.mkdtemp()) / "household.db")
os.environ["AGENT_TURN_SOURCE"] = "test"
sys.path.insert(0, str(Path(__file__).parent))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SECRET = ("Zanzibar", "Ottoline", "Quillfeather", "Marrowby", "Fenwick")   # words that must never reach the log
msgs = [
    {"role": "system", "content": "## Who you are\nYou are titan.\n[MACHINES] Robin's Mac\n[SKILL GUIDE: Zanzibar]\nsteps"},
    {"role": "system", "content": "[MEMORY — relevant facts]\nOttoline likes tea"},
    {"role": "system", "content": "[SITUATION — OS field state]\nfrontmost: Quillfeather.app"},
    {"role": "system", "content": "[CUSTOM Marrowby's diary]\nsomething new nobody mapped"},
    {"role": "user", "content": "earlier question"},
    {"role": "assistant", "content": "earlier answer"},
    {"role": "user", "content": "Read by titan for this question just now (…)\n[SCHEDULE, read from the calendar just now]\n"
                                "Fri 9 AM Fenwick dentist"},
    {"role": "user", "content": "[MEMORY — relevant facts]\nprepended fact\n\nWhat's on Friday?"},
]
sizes = lib.context_sizes(msgs)
check("only fixed section names", set(sizes) <= lib.CONTEXT_SECTIONS, True)
check("the sections it saw", sorted(sizes),
      sorted({"system_prompt", "machines", "skill_guide", "memory", "situation", "other", "history", "schedule_digest", "question"}))
check("sizes are counts", all(isinstance(v, int) and v > 0 for v in sizes.values()), True)
check("the skill guide's name is a size, not a key", ("Zanzibar" in json.dumps(sizes), sizes["skill_guide"] > 0), (False, True))
check("an unmapped header counts as other", sizes.get("other", 0) >= len("[CUSTOM Marrowby's diary]"), True)
check("earlier turns are history", sizes["history"], len("earlier question") + len("earlier answer"))
check("the prepended recall is memory, the question after its blank line is question",
      sizes.get("question"), len("\nWhat's on Friday?\n"))

print("── into the turn log ──")
lib._TURN_INFO.info, lib._TURN_INFO.route = {}, {}
lib._note_context_sizes(msgs)
lib._note_context_sizes([{"role": "user", "content": "a second call doesn't overwrite"}])
check("noted once per turn", lib._TURN_INFO.info["context_sizes"] == sizes, True)
lib._log_turn(time.monotonic(), 4, None)
line = LOG.read_text().strip().splitlines()[-1]
check("the log line carries context_sizes", json.loads(line).get("context_sizes") == sizes, True)
check("no content word reaches the log", [w for w in SECRET if w in line], [])

lib._TURN_INFO.info, lib._TURN_INFO.route = {}, {}
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
