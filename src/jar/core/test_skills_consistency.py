#!/usr/bin/env python3
"""The skill guides never contradict titan's core rules, and every tool they name exists (09-30
rules review, F-23/F-25: media said to check Plex first, calendar said to always confirm, and
code-execution described tools titan doesn't have).

Run: python3 scripts/test_skills_consistency.py
"""
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


SKILLS = Path(__file__).parent / "skills"
# Phrases that contradict a core rule, and the rule each breaks.
CONTRADICTS = {
    r"get_plex_state[^.\n]*before": "there is no Plex server; the TVs are Home Assistant's",
    r"Plex (?:Shortcuts|API)": "never Shortcuts or the shell for a TV",
    r"(?i)always confirm": "act, don't ask: ask only for what's missing",
    r"(?i)get confirmation": "the device asks; never also ask in words",
    r"(?i)workspace_id|cubesandbox|\bcube\b": "titan's model has no workspace tools",
    r"(?i)never say .not found. after one attempt": "a dedicated tool's answer is the answer",
    r"(?i)answer from general knowledge": "a household fact not stored is said to be not stored, never guessed",
}
known = set(lib._TOOL_REGISTRY) | set(lib._DEVICE_TOOLS)
for f in sorted(SKILLS.glob("*/SKILL.md")):
    text = f.read_text()
    hits = [why for rx, why in CONTRADICTS.items() if re.search(rx, text)]
    check(f"{f.parent.name}: contradicts no core rule", hits, [])
    named = {n for n in re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text)}
    check(f"{f.parent.name}: every tool it names exists", sorted(named - known), [])

prompt = lib.build_tool_system_prompt()
check("the prompt no longer says to keep searching after a miss", "Never say 'not found' after one attempt" in prompt, False)
check("… nor to search with the Mac's shell", "mdfind 'kMDItemKind" in prompt, False)
# The inverse, so the prompt and _shell_redirect can't drift (the owner's review of #270): the shell is
# the fallback after the dedicated tools, which is what the code allows.
check("the prompt allows one shell search after thin dedicated results, as _shell_redirect does",
      ("one narrow shell search is allowed" in prompt, "Never fall back to the shell" in prompt), (True, False))
lib._TURN_IO.tools_used = set()   # inside a turn, before any dedicated tool
try:
    first = lib._shell_redirect("run_shell", {"command": "mdfind -name resume"}) or ""
    lib._TURN_IO.tools_used = {"find_files"}   # find_files came back thin
    after = lib._shell_redirect("run_shell", {"command": "mdfind -name resume"})
finally:
    lib._TURN_IO.tools_used = None
check("_shell_redirect: find_files first, then the shell may run", (first.startswith("Not run: use"), after), (True, None))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
