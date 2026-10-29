#!/usr/bin/env python3
# gate: hermetic
"""Plain words in the steps card (every titan tool while it runs; the 10-16 tools after too) (_TOOL_STATUS while it runs, _TOOL_DONE after): live, they showed
"Running mail unsubscribe preview…", the tool's own name. Each has both, in plain words: no tool id, no underscore.
Run: python3 scripts/test_tool_words.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-tool-words-"))
os.environ.update({"SPACES_DB": str(_TMP / "spaces.db"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "AGENT_TURN_SOURCE": "test"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


TONIGHT = ("mail_unsubscribe_preview", "mail_unsubscribe", "mail_awaiting_reply", "what_i_did", "list_projects",
           "project_items", "project_change", "project_merge", "project_split", "todo_later", "machine_status")
check("each has words while it runs and after", [t for t in TONIGHT if t not in lib._TOOL_STATUS or t not in lib._TOOL_DONE], [])
check("…plain words: no underscore, no tool id",
      [t for t in TONIGHT for w in (lib._TOOL_STATUS.get(t, ""), lib._TOOL_DONE.get(t, ""))
       if "_" in w or any(x in w for x in TONIGHT)], [])
check("…every one is a real tool", [t for t in TONIGHT if t not in lib._TOOL_REGISTRY], [])
check("every titan tool has words while it runs: a new one can't fall back to its own name",
      sorted(n for n in lib._TOOL_REGISTRY if n not in lib._TOOL_STATUS), [])
check("…and none of them shows an underscore", sorted(n for n in lib._TOOL_REGISTRY if "_" in lib._TOOL_STATUS.get(n, "")), [])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
