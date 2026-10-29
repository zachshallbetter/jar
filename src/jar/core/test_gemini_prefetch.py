#!/usr/bin/env python3
"""titan-node#445: a schedule question to Gemini gets titan's calendar digest before the first round, so it can
answer in one round. The digest goes in as a user-role note before the question, marked as data, never into
the system text (Gemini's cached prefix). Off unless AGENT_GEMINI_SCHEDULE_PREFETCH is set. Hermetic: the
device tools are faked.
# gate: hermetic
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_runtime_lib as lib  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


now = lib._local_now().replace(tzinfo=None)
when = (now + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
CAL = f"• {when:%a %b} {when.day}, 3:00 PM — Dentist [calendar: Family]"
calls: list[str] = []


def fake_execute(tool, args):
    calls.append(tool)
    return CAL if tool == "list_calendar_events" else "No reminders."


lib.execute_tool = fake_execute
lib._device_bridge_enabled = lambda: True
lib._device_tool_local = lambda name: True
SYSTEM = {"role": "system", "content": "the cached prefix"}
ask = [SYSTEM, {"role": "user", "content": "What's on my calendar tomorrow?"}]

os.environ.pop("AGENT_GEMINI_SCHEDULE_PREFETCH", None)
check("off by default: nothing read, messages unchanged", lib._with_schedule_digest_gemini(ask) == ask and not calls)

os.environ["AGENT_GEMINI_SCHEDULE_PREFETCH"] = "1"
lib._TURN_INFO.info = {}
out = lib._with_schedule_digest_gemini(ask)
check("on: the calendar is read before the first round", "list_calendar_events" in calls, str(calls))
check("the system text is untouched (the cached prefix)", [m for m in out if m["role"] == "system"] == [SYSTEM])
check("a user-role note just before the question", len(out) == 3 and out[1]["role"] == "user"
      and out[2] == ask[1] and "Dentist" in out[1]["content"], str(out))
check("marked as data (#473)", "Dentist" in out[1]["content"] and out[1]["content"] != CAL
      and lib._TURN_IO.untrusted_spans >= 1)
check("counted for the turn log", lib._TURN_INFO.info.get("schedule_prefetch") == 1
      and "schedule_prefetch_ms" in lib._TURN_INFO.info, str(lib._TURN_INFO.info))
calls.clear()
other = [SYSTEM, {"role": "user", "content": "Write me a haiku about rain."}]
check("not a schedule question: nothing read", lib._with_schedule_digest_gemini(other) == other and not calls)
lib._device_bridge_enabled = lambda: False
check("no device bridge: nothing read", lib._with_schedule_digest_gemini(ask) == ask)

print(f"\n{'PASS' if not failures else 'FAIL'}: {len(failures)} failed")
sys.exit(1 if failures else 0)
