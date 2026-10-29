#!/usr/bin/env python3
# gate: hermetic
"""The daily brief (titan-node#1038, 10-05 17:03 PT): one event through two calendars is one event; no stray
"Correction:" under a brief whose days the app already shows; Gemini re-asks before it corrects; the brief answers
from its one prefetched read instead of reading again (each read drew another card); and its calendar comes from the
asker's own device, never titan's household schedule. Fictional names only (Alex, Robin, Ava); device tools faked.
Run: python3 scripts/test_daily_brief.py"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# A throwaway household store (#1230): calendar reads are cut by its claims, and an unreadable one fails closed.
import tempfile as _tf  # noqa: E402
os.environ.setdefault("HOUSEHOLD_DB", os.path.join(_tf.mkdtemp(prefix="titan-hh-"), "household.db"))
os.environ["AGENT_GEMINI_SCHEDULE_PREFETCH"] = "1"
import agent_runtime_lib as lib  # noqa: E402
import claim_checks as cc  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


TODAY = date(2026, 10, 5)
MON = TODAY + timedelta(days=7)
DUP = (f"• {TODAY:%a %b} {TODAY.day}, all day — Ava's Birthday [calendar: Birthdays]\n"
       f"• {TODAY:%a %b} {TODAY.day}, all day — ava’s birthday [calendar: Birthdays (Google)]\n"
       f"• {TODAY:%a %b} {TODAY.day}, 6:15 PM — Ballet [calendar: Family]\n"
       f"• {TODAY:%a %b} {TODAY.day}, 7:15 PM — Ballet [calendar: Family]")

print("── 1. one event through two calendars is one event ──")
text, dropped = cc.dedupe_event_lines(DUP, TODAY)
check("the second birthday line (another calendar, case and apostrophe aside) is dropped",
      (dropped, text.lower().count("ava")) == (1, 1), str((dropped, text)))
check("…the same title at another time stays", text.count("Ballet") == 2, text)
check("other lines are untouched", cc.dedupe_event_lines("Calendar access is off.", TODAY) == ("Calendar access is off.", 0))

lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls, lib._TURN_IO.brief_reads = [], {}, None
lib._TURN_INFO.info = {}
device_calls: list[tuple[str, dict]] = []


def fake_device(name, args):
    device_calls.append((name, dict(args or {})))
    return DUP if name == "list_calendar_events" else "• Return library books (due Oct 6)"


lib._execute_tool = fake_device
lib._local_now = lambda: datetime(2026, 10, 5, 17, 3)
out = lib.execute_tool("list_calendar_events", {"start": "2026-10-05T00:00:00", "end": "2026-10-05T23:59:59"})
check("what the model reads holds the birthday once", out.lower().count("ava") == 1, out)
check("…and the turn log counts what was dropped", lib._TURN_INFO.info.get("calendar_dupes") == 1, str(lib._TURN_INFO.info))
check("the claim checks read the deduped list", lib._TURN_IO.outcomes[-1]["full"].lower().count("ava") == 1)

print("── 2. no stray Correction under a brief ──")
week = "\n".join([f"• {MON:%a %b} {MON.day}, all day — Garbage Day [calendar: House]",
                  f"• {MON:%a %b} {MON.day}, all day — Recycling [calendar: House]",
                  f"• {MON:%a %b} {MON.day}, 4:00 PM — Robin: piano lesson [calendar: Family]"])
evs = cc.parse_events(week, TODAY)
reply = "Monday is busy: Garbage Day and Recycling."
q_list = "What's on my calendar this week?"
check("a listing that leaves one out still gets its note", bool(cc.check_omitted(reply, q_list, evs, TODAY)))
check("not when the app shows that day in full (an agenda card)", cc.check_omitted(reply, q_list, evs, TODAY, {MON}) == [])
BRIEF = ("This is my daily brief. My Mac already shows me today's weather, my calendar and reminders, and the house, "
         "so don't list those. In one or two short sentences, tell me the one thing worth knowing or doing today.")
check("not when the question says not to list (the Mac's brief)", cc.check_omitted(reply, BRIEF, evs, TODAY) == [])
reads = [{"tool": "list_calendar_events", "text": week, "range": [MON.isoformat(), MON.isoformat()]}]
check("notes(): a read the app drew as a card covers its day",
      not any("also has" in n for n in cc.notes(reply, q_list, [{**reads[0], "shown": True}], datetime(2026, 10, 5, 17))))
check("notes(): a read with no card still checks", any("also has" in n for n in cc.notes(reply, q_list, reads,
                                                                                           datetime(2026, 10, 5, 17))))
lib._TURN_IO.outcomes = [{"tool": "list_calendar_events", "ok": True, "write": False, "text": week, "full": week,
                          "range": [MON.isoformat(), MON.isoformat()]}]
for client, want in (("mac", False), ("iphone", False), ("tv", True), (None, True)):
    lib._TURN_IO.client = client
    got = any("also has" in n for n in lib._claim_notes(reply, q_list))
    check(f"client {client}: a calendar read is {'not ' if want else ''}drawn as a card", got == want, str(got))
lib._TURN_IO.client = None
asked: list[list] = []


def regemini(answer):
    def run(m):
        asked.append(m)
        return answer
    return run


lib._can_retry = lambda: True
lib._turn_left = lambda: 60.0
fixed = lib._gemini_notes_retry(reply, [{"role": "user", "content": q_list}],
                                regemini("Monday: Garbage Day, Recycling, and Robin has piano at 4."))
check("Gemini is asked once more when a check fires, not corrected in text",
      (len(asked), "piano" in fixed, "Correction" in fixed), str((len(asked), fixed)))
check("…with the check's own words as the instruction", "Robin: piano lesson" in asked[0][-1]["content"])
check("…and the fix is counted", lib._TURN_INFO.info.get("claim_retry_fixed") == 1)
asked.clear()
right = "Monday: Garbage Day, Recycling, and Robin has piano at 4."
check("a right reply isn't asked again", lib._gemini_notes_retry(right, [{"role": "user", "content": q_list}],
                                                                   regemini("x")) == right and not asked)
lib._turn_left = lambda: 5.0
check("no time left: the first reply stands (its Correction is the last resort)",
      lib._gemini_notes_retry(reply, [{"role": "user", "content": q_list}], regemini("x")) == reply and not asked)
lib._turn_left = lambda: 60.0

print("── 3/4. the brief answers from its one read ──")
lib._TURN_IO.outcomes, lib._TURN_IO.brief_reads, lib._TURN_INFO.info = [], None, {}
lib._device_bridge_enabled = lambda: True
lib._device_tool_local = lambda name: True
real_exec = lib.execute_tool
prefetch_calls: list[str] = []


def prefetch_exec(name, args):
    prefetch_calls.append(name)
    return real_exec(name, args)


lib.execute_tool = prefetch_exec
brief_msgs = [{"role": "system", "content": "prefix"}, {"role": "user", "content": BRIEF}]
lib._with_schedule_digest_gemini(brief_msgs)
lib.execute_tool = real_exec
check("the brief's prefetch reads the calendar and reminders once each", sorted(prefetch_calls) ==
      ["list_calendar_events", "list_reminders"], str(prefetch_calls))
device_calls.clear()
again = lib.execute_tool("list_calendar_events", {"start": "2026-10-05T00:00:00", "end": "2026-10-12T23:59:59"})
check("a week read inside the prefetched window is answered from it: no device call (no second card)",
      (device_calls, "Ballet" in again), str(device_calls))
lib.execute_tool("list_reminders", {})
check("…the reminders too", device_calls == [], str(device_calls))
check("…counted", lib._TURN_INFO.info.get("brief_reads_served") == 2, str(lib._TURN_INFO.info))
check("…and the served read is one the checks see, not drawn as a card",
      lib._TURN_IO.outcomes[-2].get("served") is True and lib._TURN_IO.outcomes[-2]["range"] == ["2026-10-05", "2026-10-12"])
lib.execute_tool("list_calendar_events", {"start": "2026-10-20T00:00:00", "end": "2026-10-21T23:59:59"})
check("a range the prefetch didn't cover is read from the device", [c[0] for c in device_calls] == ["list_calendar_events"])
lib._TURN_IO.outcomes, lib._TURN_IO.brief_reads = [], None
device_calls.clear()
lib._with_schedule_digest_gemini([{"role": "user", "content": "What's on my calendar this week?"}])
device_calls.clear()
lib.execute_tool("list_calendar_events", {"start": "2026-10-05T00:00:00", "end": "2026-10-06T23:59:59"})
check("not a brief: the model's own read still goes to the device (its agenda card is the answer)", len(device_calls) == 1)
lib._TURN_IO.brief_reads = {"list_reminders": ({}, "x")}
lib._TURN_IO.tools_used = []
check("the turn's end clears what the brief read", "lib._TURN_IO.brief_reads = None" in
      Path(lib.__file__).read_text().replace("            _TURN_IO.brief_reads = None", "lib._TURN_IO.brief_reads = None"))

print("── 5. the brief's calendar is the asker's own device's ──")
import schedule_view  # noqa: E402
touched: list[str] = []
real_fv = schedule_view.for_viewer
schedule_view.for_viewer = lambda *a, **k: touched.append("household schedule") or real_fv(*a, **k)
lib._TURN_IO.outcomes, lib._TURN_IO.brief_reads = [], None
prefetch_calls.clear()
lib.execute_tool = prefetch_exec
lib._with_schedule_digest_gemini(brief_msgs)
lib.execute_tool = real_exec
schedule_view.for_viewer = real_fv
check("read through the device bridge (the asker's own Mac or iPhone calendars)", "list_calendar_events" in prefetch_calls)
check("titan's household schedule is never read for a brief", touched == [], str(touched))

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all daily brief checks passed'}")
sys.exit(1 if failures else 0)
