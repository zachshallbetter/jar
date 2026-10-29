#!/usr/bin/env python3
"""Plain words, enforced: tool identifiers and check names in a reply become what they are to the
user (10-01: "Add todos for Claude (`add_todo for=claude`)", "using `create_skill`", "Run run_check
for jellybean-selftests"). Code the user asked for, and questions about the code, are left alone.

Run: python3 scripts/test_plain_words.py
"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(__file__))
_TMP = tempfile.mkdtemp(prefix="titan-plain-words-")
os.environ["AGENT_TURN_LOG"] = os.path.join(_TMP, "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


f = lib._with_plain_tool_names
print("\n── the 10-01 replies ──")
check("Claude's todo list", f("* Specific project specs or todos for new tools via `add_todo for=claude`.", "What could I give you?"),
      "* Specific project specs or todos for new tools via Claude's to-do list.")
check("saved skills", f("Save recurring workflows as reusable skills using `create_skill` so I can run them.", "how can you help"),
      "Save recurring workflows as reusable skills using saved Mac skills so I can run them.")
check("a check by its name", f("Next step: Run run_check for jellybean-selftests", "run the tests"), "Next step: Run Jellybean's self-tests")
check("what's new", f("Added `name_images` for auto-naming photos.", "what's new"), "Added picture naming for auto-naming photos.")
check("'capabilities' the English word stays", f("You can improve my capabilities by adding skills.", "what could I give you?"),
      "You can improve my capabilities by adding skills.")
check("`capabilities` the tool goes", f("I ran `capabilities` to check.", "is it set up?"), "I ran a live check of what's set up to check.")
check("a bare `for=claude`", f("Add todos for Claude (`for=claude`).", "hi"), "Add todos for Claude (Claude's to-do list).")
check("the honesty correction names no tool", f("Correction: that didn't go through. create_reminder said: “No access.”", "remind me"),
      "Correction: that didn't go through. Reminders said: “No access.”")
print("\n── reads right ──")
check("no doubled article", f("The `jellybean-build` check failed.", "did it build?"), "The Jellybean build check failed.")
check("'the X tool' → the thing", f("I'll use the `run_check` tool.", "hi"), "I'll use the self-tests.")
check("the phrase's own 'your'", f("Use the list_calendar_events tool.", "hi"), "Use your calendar.")
check("an unmapped tool: its words", f("I used get_frontmost_app.", "hi"), "I used get frontmost app.")
print("\n── left alone ──")
code = "```python\nadd_todo(title='x')\n```\nThat calls add_todo."
check("a reply with code", f(code, "how do I call it"), code)
check("the user named the tool", f("add_todo handles it.", "where's the add_todo code?"), "add_todo handles it.")
check("'change the code yourself?' still gets plain words (10-01 live)",
      f("I add a task with `add_todo for=claude`.", "Could you change the titan or Jellybean code yourself?"),
      "I add a task with Claude's to-do list.")
check("a question about the code", f("It's create_skill in agent_runtime_lib.", "which function saves skills?"),
      "It's create_skill in agent_runtime_lib.")
check("ordinary words and snake_case that isn't a tool", f("Your snake_case variable is fine. I'll share it.", "hi"),
      "Your snake_case variable is fine. I'll share it.")
check("'theory' isn't 'the'", f("There's a theory about the Apple TV.", "hi"), "There's a theory about the Apple TV.")
print("\n── wired in ──")
check("capabilities tells the model its names are internal", "never show these to the user" in __import__("capabilities").abilities_part(), True)
check("the user's words, not the memory block", lib._last_user_words([{"role": "user", "content": "[MEMORY — x]\n\nhi there"}]), "hi there")

orig = lib._run_agent_turn_inner
lib._run_agent_turn_inner = lambda *a, **k: "Save it as a skill with `create_skill`."
try:
    check("every turn's reply goes through it", lib._run_agent_turn([{"role": "user", "content": "can you remember this workflow?"}]),
          "Save it as a skill with saved Mac skills.")
finally:
    lib._run_agent_turn_inner = orig

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
