#!/usr/bin/env python3
# gate: hermetic
"""run_agent / agent_status / agent_discard (jellybean#209): the Mac's coding-agent tools as titan offers them. Adults
and the admin only (never a kid or a shared household device); start and discard are dangerous (the Mac asks every
time), status is a read. Definitions only: nothing here talks to a device."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    good = got == expected
    passed, failed = passed + good, failed + (not good)
    print(f"  [{'PASS' if good else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


tools = ["run_agent", "agent_status", "agent_discard"]
check("defined as device tools", [t in lib._DEVICE_TOOLS for t in tools], [True, True, True])
check("risk: start and discard dangerous, status a read", [lib._DEVICE_TOOLS[t]["risk"] for t in tools],
      ["dangerous", "read", "dangerous"])
check("run_agent's parameters", sorted(lib._DEVICE_TOOLS["run_agent"]["parameters"]), ["prompt", "repo"])
desc = lib._DEVICE_TOOLS["run_agent"]["description"]
check("it says what it may and may not do", all(w in desc for w in ("worktree", "no shell", "nothing merged or pushed", "allow-list")), True)
for role, want in (("admin", True), ("adult", True), ("kid", False), ("household", False)):
    check(f"roles: {role}", [hh.role_allows_tool(role, t) for t in tools], [want] * 3)
check("each has a status line", [t in lib._TOOL_STATUS for t in tools], [True, True, True])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
