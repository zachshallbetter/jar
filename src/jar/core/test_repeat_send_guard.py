#!/usr/bin/env python3
# gate: hermetic   (no stores written, nothing sent: the executor's own call is a stub)
"""#1251: a repeating text to someone, never a one-off reminder. "Every weekday at 4:30, text Ava…" made one reminder for
the asker on the box brain (6 of 6, 10-08). The reminder is held and the model told to use schedule_action with the
repeat, or save_routine. A reminder they asked for themselves and a text with no repeat are untouched. Views x
viewers: every role. Fictional people.
Run: python3 scripts/test_repeat_send_guard.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-repeat-send-"))
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "SCHEDULED_DB": "scheduled.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import repeat_send_guard as G  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── what asks for a repeating text to someone ──")
YES = {"Every weekday at 4:30, text Ava to get ready for ballet.": "Every weekday",
       "Text Robin every morning at 7 that the bus is coming.": "every morning",
       "Send a text to Leo daily at 3 saying pickup is at 3:30.": "daily",
       "Message Jordan on Fridays at 5: pizza night.": "Fridays"}
check("each one", {q: G.repeat_send_words(q) for q in YES}, YES)
NO = ["Remind me every weekday at 4:30.", "Remind me every weekday to text Ava.", "Text Ava at 4:30 today.",
      "Every weekday at 4:30, remind me about ballet.", "Text me every morning with the weather.",
      "Read me the texts from today."]
check("not these (a reminder for themselves, a text with no repeat, a text to themselves)",
      {q: G.repeat_send_words(q) for q in NO}, {q: None for q in NO})

print("── in the executor ──")
import agent_runtime_lib as lib  # noqa: E402
io = lib._TURN_IO
real_exec = lib._execute_tool
lib._execute_tool = lambda name, args: "STUB: ran"   # nothing real runs, whatever the guard decides


def run(tool, args, words, role="adult", jev_source=None):
    saved = {k: getattr(io, k, None) for k in ("repeat_send", "everyone", "later", "jev_source", "role")}
    try:
        io.repeat_send, io.everyone, io.later, io.jev_source, io.role = words, None, None, jev_source, role
        return str(lib.execute_tool(tool, args))
    finally:
        for k, v in saved.items():
            setattr(io, k, v)


lib._TURN_INFO.info = {}
out = run("create_reminder", {"title": "Text Ava: ballet", "due": "2026-10-08T16:30"}, "Every weekday")
check("'every weekday, text Ava': the reminder doesn't run; told schedule_action with the repeat, or save_routine",
      (out.startswith("Not run: they asked for a text to someone on a repeat"), "schedule_action" in out,
       "repeat (“Every weekday”)" in out, "save_routine" in out), (True, True, True, True))
check("…counted in the turn log", lib._TURN_INFO.info.get("repeat_send_held"), 1)
check("schedule_action itself runs", run("schedule_action", {"tool_name": "send_message"}, "Every weekday"), "STUB: ran")
check("save_routine itself runs", run("save_routine", {"name": "Ava ballet"}, "Every weekday"), "STUB: ran")
check("no repeat-send words (a reminder for themselves): the reminder runs as before",
      run("create_reminder", {"title": "Ballet"}, G.repeat_send_words("Remind me every weekday at 4:30.")), "STUB: ran")
check("'text Ava at 4:30 today' (no repeat): nothing held",
      run("create_reminder", {"title": "x"}, G.repeat_send_words("Text Ava at 4:30 today.")), "STUB: ran")
check("a background turn: never held (nobody asked)",
      run("create_reminder", {"title": "x"}, "Every weekday", jev_source="proactive"), "STUB: ran")
check("views x viewers: the hold is the same for every role and widens nothing (the call simply doesn't run)",
      {role: run("create_reminder", {"title": "x"}, "Every weekday", role=role).startswith("Not run:")
       for role in ("admin", "adult", "kid", "household")},
      {role: True for role in ("admin", "adult", "kid", "household")})
src = (HERE / "agent_runtime_lib.py").read_text()
check("set from the last user message, cleared at the turn's end, in the turn log",
      ("_TURN_IO.repeat_send = _rsg.repeat_send_words(_users_words(" in src,
       "_TURN_IO.later = _TURN_IO.later_asked = _TURN_IO.everyone = _TURN_IO.repeat_send = None" in src, '"repeat_send_held"' in src),
      (True, True, True))

lib._execute_tool = real_exec
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
