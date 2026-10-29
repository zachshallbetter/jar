#!/usr/bin/env python3
# gate: hermetic
"""Gemini's narrowed tool declarations (#683): the core plus the groups a question implies and the tools already used;
on by default, off with AGENT_TOOL_NARROW=0; only the declarations narrow (every tool still runs), and a round that
reaches outside the set gets every tool next. The prose tool list stays whole.
Run: python3 scripts/test_tool_narrow.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-narrow-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "HOUSEHOLD_PREFS": "household.json", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("AGENT_TOOL_NARROW", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


print("── which tools a question gets ──")
cal = lib.narrowed_tools("What's on my calendar tomorrow?")
check("a calendar question: the core and the calendar group, not the house or ops",
      ({"kb_retrieve", "list_calendar_events", "create_reminder"} <= cal, "home_control" in cal, "run_shell" in cal),
      (True, False, False))
home = lib.narrowed_tools("Turn off the living room lights")
check("a lights question: the house group", ("home_control" in home, "list_calendar_events" in home), (True, False))
check("the skill pick adds its group", "web_search" in lib.narrowed_tools("hmm", skills=["web-research"]), True)
check("a tool already used this turn stays", "find_my_locate" in lib.narrowed_tools("ok", used=["find_my_locate"]), True)
check("the core is always there", lib.TOOL_CORE <= lib.narrowed_tools(""), True)
check("every grouped tool exists (a registry or device tool)",
      sorted(t for _, (_, _, ts) in lib.TOOL_GROUPS.items() for t in ts
             if t not in lib._TOOL_REGISTRY and t not in lib._DEVICE_TOOLS), [])

print("── on by default, off with AGENT_TOOL_NARROW=0 ──")
check("on by default", lib._narrow_on(), True)
os.environ["AGENT_TOOL_NARROW"] = "0"
check("off with 0", lib._narrow_on(), False)
os.environ.pop("AGENT_TOOL_NARROW")

print("── reaching outside the set ──")
narrow = lib.narrowed_tools("What's on my calendar tomorrow?")
check("a declared call: no", lib._reaches_outside([], [{"name": "list_calendar_events", "args": {}}], narrow), False)
check("tool_help about an undeclared tool: yes",
      lib._reaches_outside([], [{"name": "tool_help", "args": {"name": "home_control"}}], narrow), True)
check("a text-protocol call to an undeclared tool: yes",
      lib._reaches_outside([{"text": '<tool_call>{"tool": "home_control", "device": "lamp"}</tool_call>'}], [], narrow), True)

print("── the rounds: narrowed, then every tool once it reaches outside ──")
sent: list[int] = []
replies = [{"candidates": [{"content": {"parts": [{"functionCall": {"name": "tool_help", "args": {"name": "home_control"}}}]}}]},
           {"candidates": [{"content": {"parts": [{"text": "Done."}]}}]}]
saved = (lib._gemini_call, lib.execute_tool)
lib._gemini_call = lambda body, model=None: sent.append(len(body["tools"][0]["function_declarations"])) or replies[len(sent) - 1]
lib.execute_tool = lambda name, args: "ok"
lib._TURN_INFO.info = {}
lib._TURN_IO.role, lib._TURN_IO.person, lib._TURN_IO.source = "admin", {"id": "zach", "name": "Zach"}, "user"
try:
    out = lib._gemini_agent_rounds([{"role": "system", "content": "rules"},
                                    {"role": "user", "content": "What's on my calendar tomorrow?"}],
                                   3, "gemini-test", None, None, None)
finally:
    lib._gemini_call, lib.execute_tool = saved
full = len(lib._gemini_function_declarations(None))
check("round 1 declared the narrowed set, round 2 every tool", (sent[0] < full, sent[1] == full), (True, True))
check("…and the turn log says it widened", (lib._TURN_INFO.info.get("tools_declared"), lib._TURN_INFO.info.get("tools_widened")),
      (sent[0], True))
check("…the reply came back", out.strip(), "Done.")
sent.clear()
os.environ["AGENT_TOOL_NARROW"] = "0"
replies[0] = replies[1]
lib._gemini_call = lambda body, model=None: sent.append(len(body["tools"][0]["function_declarations"])) or replies[0]
try:
    lib._TURN_INFO.info = {}
    lib._gemini_agent_rounds([{"role": "system", "content": "rules"}, {"role": "user", "content": "Calendar tomorrow?"}],
                             3, "gemini-test", None, None, None)
finally:
    lib._gemini_call = saved[0]
    os.environ.pop("AGENT_TOOL_NARROW")
check("off: every tool from the first round", sent, [full])
lib._TURN_INFO.info = None

print("── a skill's guide names only tools its pick declares (#1754) ──")
import re  # noqa: E402
import agent_skills  # noqa: E402
_known = set(lib._TOOL_REGISTRY)
for _name, _sk in sorted(lib._turn_skills().items()):
    _named = {w for w in re.findall(r"`([a-z_]+)`", agent_skills.skill_guide_prompt(_sk)) if w in _known}
    check(f"{_name}: every titan tool its guide names is declared", sorted(_named - lib.narrowed_tools("", [_name])), [])
check("…the code-execution pick declares add_todo, its route for titan's or the apps' code",
      "add_todo" in lib.narrowed_tools("Could you change the titan code yourself?", ["code-execution"]), True)

print("── the prose tool list stays whole ──")
check("build_tool_system_prompt still lists every tool", "home_control" in lib.build_tool_system_prompt(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
