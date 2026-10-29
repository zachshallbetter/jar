#!/usr/bin/env python3
# gate: hermetic
"""#836: a reply that says files were deleted is backed only when a command showed it. 10-03: "I have deleted the other
resume files" after a run_shell that removed nothing (the files were still on disk). Also: run_shell's "[exit N]" from
the Mac is read (a nonzero exit is a failure) and the turn log keeps only a digest of the output.

Run: python3 scripts/test_honest_deletions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


def turn(*calls):
    """A fresh turn's outcomes and info, from (tool, args, result) calls."""
    lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls = [], {}
    lib._TURN_INFO.info = {}
    for name, args, result in calls:
        lib._record_outcome(name, args, result)


CLAIM = "I have deleted the other resume files from your Downloads folder as well."
CORR = "Correction: I can't confirm anything was deleted"

print("── the 10-03 case: a silent rm, then \"deleted\" ──")
turn(("run_shell", {"command": "rm ~/Downloads/Alex-Resume-*.pdf"}, "[exit 0]"))
check("a command that reported nothing doesn't back it", CORR in lib._with_honest_outcome(CLAIM), True)
turn(("run_shell", {"command": "rm ~/Downloads/Alex-Resume-*.pdf"}, ""))
check("an app that doesn't send the exit status doesn't either", CORR in lib._with_honest_outcome(CLAIM), True)

print("── a failed command ──")
turn(("run_shell", {"command": "rm ~/Downloads/x.pdf"}, "rm: /Users/alex/Downloads/x.pdf: No such file or directory\n[exit 1]"))
out = lib._with_honest_outcome(CLAIM)
check("nonzero exit: the reply says it failed, with the command's words", ("the command failed" in out, "No such file" in out),
      (True, True))
check("…and the outcome counts as failed", lib._TURN_IO.outcomes[-1]["ok"], False)
turn(("run_shell", {"command": "find ~ -name x -delete"}, "[timed out]"))
check("a timeout is a failure", lib._TURN_IO.outcomes[-1]["ok"], False)

print("── backed claims stay as they are ──")
turn(("run_shell", {"command": "rm -v ~/Downloads/Alex-Resume.pdf"}, "/Users/alex/Downloads/Alex-Resume.pdf\n[exit 0]"))
check("exit 0 with a report backs it", lib._with_honest_outcome(CLAIM), CLAIM)
turn(("run_shell", {"command": "ls"}, "a\n[exit 0]"), ("delete_calendar_event", {"id": "e1"}, "Deleted “Dentist”."))
check("another tool did the deleting: its own result speaks", lib._with_honest_outcome("I've deleted the Dentist event."),
      "I've deleted the Dentist event.")
turn(("list_reminders", {}, "Milk"))
check("no shell this turn: not this guard's business", lib._with_honest_outcome(CLAIM), CLAIM)
turn(("run_shell", {"command": "rm x"}, "[exit 0]"))
check("no deletion claim: untouched", lib._with_honest_outcome("Here's what's in Downloads."), "Here's what's in Downloads.")
check("'moved them to the Trash' is a deletion claim", CORR in lib._with_honest_outcome("I've moved them to the Trash."), True)

print("── the turn log keeps a digest, never the output ──")
turn(("run_shell", {"command": "ls ~/Downloads"}, "Alex-Resume.pdf\nRobin-Letter.docx\n[exit 0]"))
d = lib._TURN_INFO.info["shell"][0]
check("exit and line count", (d["tool"], d["exit"], d["lines"]), ("run_shell", 0, 2))
check("a 12-char hash, no file names", (len(d["sha"]), "Alex" in str(lib._TURN_INFO.info)), (12, False))

print("── trash_files (the Mac's own delete, 0.13.34) ──")
turn(("trash_files", {"paths": ["~/Downloads/Alex-Resume.pdf"]},
      "Trashed: /Users/alex/Downloads/Alex-Resume.pdf\n[checked] all 1 are gone from where they were.\nUndo: one Undo puts all 1 back."))
check("a trash with a Trashed line backs it", lib._with_honest_outcome("I've moved it to the Trash."), "I've moved it to the Trash.")
turn(("trash_files", {"paths": ["~/Downloads/x.pdf"]}, "Skipped: /Users/alex/Downloads/x.pdf (not found)\n[checked] nothing was trashed."))
check("all skipped: not a success", lib._TURN_IO.outcomes[-1]["ok"], False)
check("…and a claim gets the correction", "Correction" in lib._with_honest_outcome("I've moved it to the Trash."), True)
turn(("trash_files", {"paths": ["a"] * 201}, "Too many paths (201); at most 200 per call. Nothing was trashed."))
check("over the limit: not a success", lib._TURN_IO.outcomes[-1]["ok"], False)

print("── the shell never deletes ──")
lib._TURN_IO.tools_used, lib._TURN_IO.denied = set(), {}
for cmd in ("rm ~/Downloads/a.pdf", "cd ~ && rm -rf old", "find ~/Downloads -name '*.tmp' -delete", "sudo rm x",
            "ls | xargs rm", "find . -name x -exec rm {} \\;", "ls | xargs -0 rm -f"):
    check(f"refused: {cmd}", (lib._shell_redirect("run_shell", {"command": cmd}) or "").startswith(
        "Not run: files are deleted only with trash_files"), True)
check("AppleScript 'delete file' too", (lib._shell_redirect("run_applescript", {"script": 'tell application "Finder" to delete file "x"'})
      or "").startswith("Not run: files are deleted only with trash_files"), True)
check("a plain find still goes to find_files, not this rule",
      "trash_files" in (lib._shell_redirect("run_shell", {"command": "find ~/Downloads -name '*.pdf'"}) or ""), False)
check("'grep -rm' isn't rm", (lib._shell_redirect("run_shell", {"command": "echo firmware"}) or ""), "")
check("trash_files is a write tool with a hold verb", ("trash_files" in lib._WRITE_TOOLS, lib._HOLD_VERB.get("trash_files")),
      (True, "move these files to the Trash"))
import grants  # noqa: E402
check("grants: approved ahead only for the exact call", "trash_files" in grants.EXACT_ONLY, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
