#!/usr/bin/env python3
"""An eval turn's own pins: `eval_brain` (score one brain) and `eval_tools` (a hard allowlist, so a
benchmark turn can't run a tool off its list whatever the model asks for). Other turns ignore both.

Run: python3 scripts/test_eval_pins.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["AGENT_MEMORY_DIR"] = tempfile.mkdtemp()

import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


check("eval: brain and tools", api._parse_eval_pins({"eval_brain": "Local", "eval_tools": ["kb_retrieve", 3]}, "eval"),
      ("local", frozenset({"kb_retrieve"})))
check("eval: gemini", api._parse_eval_pins({"eval_brain": "gemini"}, "eval"), ("gemini", None))
check("eval: an unknown brain is the usual routing", api._parse_eval_pins({"eval_brain": "gpt"}, "eval"), (None, None))
check("eval: an empty list allows nothing", api._parse_eval_pins({"eval_tools": []}, "eval"), (None, frozenset()))
check("not an eval turn: both ignored", api._parse_eval_pins({"eval_brain": "local", "eval_tools": ["x"]}, None), (None, None))

io = lib._TURN_IO
io.role = "admin"
io.eval_tools = frozenset({"get_datetime", "recall_events"})
check("allowlist: the prompt still advertises the usual tools (the eval sees the real prompt)",
      lib._role_allows("ingest_text"), True)
check("allowlist: a listed tool runs", lib._execute_tool("get_datetime", {}).startswith("Not run"), False)
check("allowlist: a write is refused when it runs, even for the admin",
      lib._execute_tool("ingest_text", {"title": "x", "content": "y"}), "Not run: this eval turn only reads (ingest_text isn't on its list).")
check("allowlist: so is a device write", lib._execute_tool("create_calendar_event", {"title": "x"}).startswith("Not run: this eval"), True)
io.eval_tools = None
io.role = None

# truth_eval's own logic (no titan): the week answers a range like a device, and checks read replies.
import truth_eval as T  # noqa: E402
from datetime import datetime  # noqa: E402
now = datetime(2026, 9, 30, 14, 7, tzinfo=T.TZ)
evs = T.week(now)
tomorrow = T.calendar_answer(evs, {"start": "2026-10-01", "end": "2026-10-01"})
check("truth: tomorrow's range, in the apps' own line format", tomorrow.splitlines(),
      ["• Thu Oct 1, all day — Ava's field trip", "• Thu Oct 1, 9:00 AM–10:00 AM — Dentist [at: Pearl Dental]",
       "• Thu Oct 1, 3:00 PM–4:00 PM — Piano lesson", "• Thu Oct 1, 3:30 PM–4:30 PM — Leo soccer"])
# The range is the fixture's own week: with none, calendar_answer reads around the wall clock, and from 10-07 the
# fixture's trip (Oct 4–6) fell out of that window and this check failed on every branch.
check("truth: a trip spans its days", any("Sun Oct 4, all day – Tue Oct 6 — Spokane trip" in l
                                           for l in T.calendar_answer(evs, {"start": "2026-09-30", "end": "2026-10-07"}).splitlines()), True)
c = {x.name: x for x in T.cases(now, [], [])}
check("truth: busy at 3:30 read right", c["busy_tomorrow_330"].check("No, you have Piano lesson then."), [])
check("truth: a made-up code is a miss", c["not_stored"].check("The shed code is 4417."),
      ["made up a code", "didn't say it doesn't know"])
check("truth: allowlist has no writes", [t for t in T.READ_TOOLS if t.startswith(("create", "update", "delete", "send", "ingest", "kb_forget", "log"))], [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
