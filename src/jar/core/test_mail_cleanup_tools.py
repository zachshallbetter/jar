#!/usr/bin/env python3
# gate: hermetic
"""mail_cleanup_preview / mail_archive (jellybean#211): the Mac's mail cleanup tools as titan offers them. Adults and the
admin only (never a kid or a shared household device); the preview is a read, the archive a write (the Mac asks every
time and keeps an Undo). Archive only: nothing here can delete mail. Definitions only: nothing talks to a device."""
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


tools = ["mail_cleanup_preview", "mail_archive"]
check("defined as device tools", [t in lib._DEVICE_TOOLS for t in tools], [True, True])
check("risk: the preview a read, the archive a write", [lib._DEVICE_TOOLS[t]["risk"] for t in tools], ["read", "write"])
check("the preview's parameters", sorted(lib._DEVICE_TOOLS["mail_cleanup_preview"]["parameters"]),
      ["accounts", "older_than_months", "only_lists"])
check("the archive's parameters: a preview and its senders, never message ids",
      sorted(lib._DEVICE_TOOLS["mail_archive"]["parameters"]), ["preview_id", "senders"])
desc = lib._DEVICE_TOOLS["mail_archive"]["description"]
check("the archive says it never deletes, asks every time, can be undone",
      all(w in desc for w in ("never deleted", "confirms on the Mac every time", "undone")), True)
check("no delete tool for mail", [t for t in lib._DEVICE_TOOLS if "mail" in t and "delete" in t], [])
for role, want in (("admin", True), ("adult", True), ("kid", False), ("household", False)):
    check(f"roles: {role}", [hh.role_allows_tool(role, t) for t in tools], [want] * 2)
check("each has a status line", [t in lib._TOOL_STATUS for t in tools], [True, True])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
