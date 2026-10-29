#!/usr/bin/env python3
# gate: hermetic
"""The Mac search budget (#1513; the nightly's find-not-found): after two find_files results that found nothing in one
turn, further Mac searches that turn (find_files, list_files, search_file_contents) aren't run and say to answer now;
a hit doesn't count, other tools and the shell aren't stopped, and a new turn starts fresh. The device is stubbed.
Run: python3 scripts/test_search_budget.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-search-budget-"))
os.environ.update({"SPACES_DB": str(_TMP / "spaces.db"), "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "AGENT_TURN_SOURCE": "user"})
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SENT = []
EMPTY = "No file names matching '{q}' in Spotlight (your home folder). To search inside files, use search_file_contents with a folder."


def inner(name, args):
    SENT.append(name)
    if name == "find_files":
        return "/Users/robin/Documents/budget.xlsx" if args.get("query") == "budget" else EMPTY.format(q=args.get("query"))
    return "ok"


lib._device_request_inner = inner
lib._TURN_IO.empty_finds = 0
lib._TURN_IO.source = "user"
first = lib._device_request("find_files", {"query": "resume"})
second = lib._device_request("find_files", {"query": "CV"})
check("two empty searches reach the Mac", (SENT, lib._TURN_IO.empty_finds), (["find_files", "find_files"], 2))
n = len(SENT)
third = lib._device_request("find_files", {"query": "curriculum"})
listing = lib._device_request("list_files", {"path": "~/Documents"})
inside = lib._device_request("search_file_contents", {"query": "resume", "folder": "~"})
check("then a third search, a folder listing and a content search aren't run, and say to answer now",
      ([third, listing, inside] == [lib.SEARCH_SPENT] * 3, len(SENT) - n), (True, 0))
check("…other tools still run (the shell, a calendar read)",
      ([lib._device_request(t, {}) for t in ("run_shell", "list_calendar_events")], len(SENT) - n), (["ok", "ok"], 2))
lib._TURN_IO.empty_finds = 0
lib._device_request("find_files", {"query": "budget"})
check("a search that finds something doesn't count", lib._TURN_IO.empty_finds, 0)
check("the eval's fake 'No files found.' counts like the Mac's words", (lib._empty_find("No files found."),
                                                                        lib._empty_find("/Users/robin/a.pdf")), (True, False))
src = (Path(__file__).parent / "agent_runtime_lib.py").read_text()
check("every turn starts with a fresh budget", src.count("_TURN_IO.empty_finds = 0") >= 2, True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
