#!/usr/bin/env python3
"""#347: a schedule or reminders question on the local model gets the calendar and reminders read for it and
laid out in code. Hermetic: fixtures in the apps' line formats; no device, no model."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_digest as sd  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


NOW = datetime(2026, 10, 1, 9, 50)
CAL = """• Wed Sep 30, 5:15 PM–6:15 PM — Ballet
• Thu Oct 1, 9:25 AM–10:25 AM — Focus block
• Thu Oct 1, 10:40 AM–11:10 AM — Call with Sam
• Fri Oct 2, all day — Ava's field trip
• Fri Oct 2, 9:00 AM–10:00 AM — Dentist [at: Pearl Dental]
• Fri Oct 2, 3:00 PM–4:00 PM — Piano lesson
• Sat Oct 3, 7:00 PM–9:00 PM — Team dinner
• Mon Oct 5, all day – Wed Oct 7 — Spokane trip"""
REM = """• Return library books (due Tue Sep 29)
• Pay the water bill (due Thu Oct 1, 8:25 AM)
• Pick up dry cleaning (due Thu Oct 1, 12:50 PM)
• Renew the car registration (due Fri Oct 2)
• Call the plumber"""

d = sd.calendar_digest(CAL, NOW)
check("days are labelled the way people ask", "- Tomorrow (Fri Oct 2): " in d and "- Yesterday (Wed Sep 30): " in d, d)
check("an empty day says nothing", "- Sun Oct 4: nothing" in d, d)
check("an all-day event doesn't take up time", "Ava's field trip (all day; doesn't take up any time)" in d, d)
check("a multi-day event gives its span and length", "Spokane trip (all day, Mon Oct 5 – Wed Oct 7, 3 days" in d, d)
check("now and next are computed", "Now: Focus block (until 10:25 AM)" in d and "Next timed event: Call with Sam at 10:40 AM (in 50 min)" in d, d)
check("yesterday's evening event is there", "5:15 PM–6:15 PM Ballet" in d)
check("unreadable lines are handed over as they are, never as 'nothing'",
      "nothing" not in sd.calendar_digest("• Friday the 2nd: dentist at nine", NOW))
check("no events at all: every day says nothing",
      sum(1 for l in sd.calendar_digest("No events between …", NOW).splitlines() if l.startswith("- ") and l.endswith(": nothing")) == 9)
check("an error or refusal gives no digest at all", sd.calendar_digest("Calendar access not granted.", NOW) == ""
      and sd.reminders_digest("Reminders access not granted.", NOW) == "")

# The apps' 40-event cap: a full listing never claims the days after it are empty.
busy = "\n".join(f"• Thu Oct 1, {h}:00 AM–{h}:30 AM — Item {i}" for i, h in enumerate([8, 9, 10, 11] * 5)) + "\n" + \
       "\n".join(f"• Fri Oct 2, {h}:00 PM–{h}:30 PM — Later {i}" for i, h in enumerate([1, 2, 3, 4] * 5))
capped = sd.calendar_digest(busy, NOW)
check("a 40-event listing marks the days after its last event (not read)",
      "- Sat Oct 3: (not read" in capped and "- Sun Oct 4: (not read" in capped and "- Sun Oct 4: nothing" not in capped, capped[-300:])
check("and drops 'complete'", "it is complete" not in capped and "stopped at 40 events" in capped.splitlines()[0])
check("days it did read keep their events", "Item 0" in capped and "- Tomorrow (Fri Oct 2): " in capped)

MARKED = """• Pay the water bill (due Thu Oct 1, 8:25 AM) [overdue by 1 h 25 min]
• Pick up dry cleaning (due Thu Oct 1, 12:50 PM) [due today · in 3 h]
• Renew the car registration (due Fri Oct 2) [tomorrow]
• Call the plumber"""
m = sd.reminders_digest(MARKED, NOW)
check("reminder lines with markers ([overdue by …], [tomorrow]) are read", "- Overdue: Pay the water bill" in m
      and "- Next: Pick up dry cleaning" in m and "- No due date: Call the plumber" in m, m)
check("a reminder line without a due date is kept, as it is",
      "Water bill — sometime soon!!" in sd.reminders_digest("• Water bill — sometime soon!!", NOW))
check("the short window is yesterday through two days ahead", sum(1 for l in sd.calendar_digest(CAL, NOW, days=3).splitlines()
      if l.startswith("- ")) == 4)

r = sd.reminders_digest(REM, NOW)
check("reminders: overdue computed", "- Overdue: Return library books (due Tue Sep 29); Pay the water bill (due Thu Oct 1, 8:25 AM)" in r, r)
check("reminders: next is the earliest not yet due", "- Next: Pick up dry cleaning (due Thu Oct 1, 12:50 PM)" in r, r)

check("schedule questions are recognised", all(sd.wants_schedule(q) for q in (
    "What's on my calendar tomorrow?", "Am I free tomorrow at 11 AM?", "When is my dentist appointment?",
    "What's on Saturday?", "How many days until the Spokane trip?", "What's next?", "When should I leave for the dentist?")))
check("chit-chat isn't", not any(sd.wants_schedule(q) for q in ("Tell me a joke", "What's the capital of France?")))
check("reminder questions are recognised", sd.wants_reminders("Is anything overdue?") and sd.wants_reminders("Which reminder comes up next?"))
check("the window is yesterday through a week ahead", sd.window(NOW) == {"start": "2026-09-30T00:00:00", "end": "2026-10-08T23:59:59"})

# Wired into the local path only.
import agent_runtime_lib as lib  # noqa: E402
calls = []
lib._local_now = lambda: NOW
lib._device_bridge_enabled = lambda: True
lib._device_tool_local = lambda name: True
lib.execute_tool = lambda name, args: calls.append((name, args)) or (CAL if name == "list_calendar_events" else REM)
msgs = [{"role": "system", "content": "rules"}, {"role": "user", "content": "Am I free tomorrow at 11 AM?"}]
out = lib._with_schedule_digest(msgs)
check("a schedule question gets the digest before the question (and, #1064 B2, tomorrow's own line after it)",
      len(out) == 4 and out[1]["content"].startswith("[SCHEDULE") and out[2] == msgs[-1]
      and out[3]["content"].startswith("[From the calendar above, read just now (it outranks any saved note), for this question: Tomorrow ("), str([m["content"][:30] for m in out]))
check("it read the calendar once, for the window", calls == [("list_calendar_events", sd.window(NOW))], str(calls))
calls.clear()
out = lib._with_schedule_digest([{"role": "user", "content": "Is anything overdue?"}])
check("a reminders question gets the reminders", "[REMINDERS" in out[0]["content"] and calls[-1][0] == "list_reminders", str(calls))
calls.clear()
chat = [{"role": "user", "content": "Tell me a joke"}]
check("chit-chat: nothing read, nothing added", lib._with_schedule_digest(chat) == chat and not calls)
# The local context is nearly full (truth r7: ~16,030 of 16,384 tokens): the digest takes only the room left.
lib.execute_tool = lambda name, args: CAL
_REAL_LOCAL_MESSAGES = lib._local_messages
lib._local_messages = lambda ms: ms   # measure just these messages
lib._TURN_IO.detail = None
big = [{"role": "system", "content": "x" * int((lib.LOCAL_CTX_TOKENS - 1024 - 200) * lib._CHARS_PER_TOKEN)},
       {"role": "user", "content": "What's on my calendar tomorrow?"}]
lib._TURN_INFO.info = {}
out = lib._with_schedule_digest(big)
added = [m["content"] for m in out if m["content"].startswith("[SCHEDULE")]
check("little room: the short window, not the week", added and sum(1 for l in added[0].splitlines() if l.startswith("- ")) == 4,
      str([len(a) for a in added]))
full = [{"role": "system", "content": "x" * int((lib.LOCAL_CTX_TOKENS - 1024 - 40) * lib._CHARS_PER_TOKEN)},
        {"role": "user", "content": "What's on my calendar tomorrow?"}]
lib._TURN_INFO.info = {}
check("no room: nothing added, and it says why", lib._with_schedule_digest(full) == full
      and lib._TURN_INFO.info.get("schedule_prefetch_skipped") == "no room in the local context")
waits = []
def slow_device(name, args):
    waits.append(lib._TURN_IO.device_wait)
    return CAL
lib.execute_tool = slow_device
lib._TURN_INFO.info = {}
lib._with_schedule_digest(msgs)
check("the prefetch asks the device for a 4 s answer and clears it after", waits == [4.0] and lib._TURN_IO.device_wait is None, str(waits))
check("and records how long it took", isinstance(lib._TURN_INFO.info.get("schedule_prefetch_ms"), int))
lib.execute_tool = lambda name, args: "Calendar access not granted."
check("a failed read adds nothing", lib._with_schedule_digest(msgs) == msgs)

# #1064 (Box's #402 gap): facts in code, not model arithmetic
from datetime import datetime as _dt  # noqa: E402
F = CAL + ("\n• Thu Oct 1, 8:00 PM–8:15 PM — Compost out [repeats: every week]"
           "\n• Thu Oct 8, 8:00 PM–8:15 PM — Compost out [repeats: every week]"
           "\n• Fri Oct 2, 3:30 PM–4:30 PM — Leo soccer")
fd = sd.calendar_digest(F, _dt(2026, 10, 1, 8, 30))
facts = next((l for l in fd.splitlines() if l.startswith("Facts: ")), "")
check("facts: the next event and how long until it, in hours and minutes", "Focus block at 9:25 AM (in 55 min)" in fd, fd[:300])
check("facts: the first timed event tomorrow, after the all-day item", "first tomorrow: Dentist 9 AM (after the all-day Ava's field trip)" in facts, facts)
check("facts: tomorrow's overlap, both events with their times",
      "conflicts/overlaps tomorrow: Piano lesson 3 PM–4 PM and Leo soccer 3:30 PM–4:30 PM" in facts, facts)
check("facts: no overlap today says none (an explicit 'no')", "conflicts/overlaps today: none" in facts, facts)
check("facts: a repeating event's next time and its rule, without the note in its name",
      "next Compost out: Today (Thu Oct 1) 8 PM (repeats every week)" in facts and "[repeats" not in facts, facts)
late = sd.calendar_digest(F, _dt(2026, 10, 1, 21, 0))
check("…once tonight's has passed, the next one is a week on", "next Compost out: Thu Oct 8 8 PM (repeats every week)" in late, late[:400])
far = sd.calendar_digest("• Thu Oct 1, 4:40 PM–5:00 PM — Pickup", _dt(2026, 10, 1, 14, 30))
check("later today: '2 h 10 min', not a bare day", "(in 2 h 10 min)" in far, far)
check("facts=False: the day list alone (a nearly full local context)", "Facts:" not in sd.calendar_digest(F, _dt(2026, 10, 1, 8, 30), facts=False))

# #1064 B2 (Box's option 2): the facts the question asks for, as one line right after it
for label, q, want in (
        ("conflicts tomorrow → tomorrow's overlap only", "Do I have any conflicts tomorrow?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: conflicts/overlaps tomorrow: Piano lesson 3 PM–4 PM and Leo soccer 3:30 PM–4:30 PM]"),
        ("double-booked today → today's explicit none", "Am I double-booked today?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: conflicts/overlaps today: none]"),
        ("first thing tomorrow", "What's my first thing tomorrow?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: first tomorrow: Dentist 9 AM (after the all-day Ava's field trip)]"),
        ("when is compost next → that series only", "When is compost out next?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: next Compost out: Today (Thu Oct 1) 8 PM (repeats every week)]"),
        ("what's next → the next timed event", "What's next on my calendar?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Next timed event: Focus block at 9:25 AM (in 55 min)]"),
        ("an empty day → its own line, an explicit nothing (empty_day, Box's B2 arm)", "Do I have anything on Sunday?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Sun Oct 4: nothing]"),
        ("a day with events → that day's line", "What's on Saturday?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Sat Oct 3: 7 PM–9 PM Team dinner]"),
        ("tomorrow, asked plainly", "Am I busy tomorrow?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Tomorrow (Fri Oct 2): Ava's field trip (all day; doesn't take up any time); 9 AM–10 AM Dentist; 3 PM–4 PM Piano lesson; 3:30 PM–4:30 PM Leo soccer]"),
        ("Wednesday: the coming one, never yesterday", "Anything on Wednesday?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Wed Oct 7: Spokane trip (all day, Mon Oct 5 – Wed Oct 7, 3 days; doesn't take up any time)]"),
        ("a question with no day and no fact → nothing", "Is my calendar synced?", "")):
    got = sd.answer_line(q, fd)
    check(f"answer line: {label}", got == want, got)
check("answer line: no Facts line (facts=False) → nothing",
      sd.answer_line("Any conflicts tomorrow?", sd.calendar_digest(F, _dt(2026, 10, 1, 8, 30), facts=False)) == "")
lib.execute_tool = lambda name, args: F if name == "list_calendar_events" else "No reminders."
qmsgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "Do I have any conflicts tomorrow?"}]
lib._TURN_INFO.info = {}
got = lib._with_schedule_digest(qmsgs)
check("the turn: digest before the question, the answer line right after it (last)",
      [m["role"] for m in got] == ["system", "system", "user", "system"] and got[-1]["content"].startswith("[From the calendar")
      and "Piano lesson" in got[-1]["content"] and lib._TURN_INFO.info.get("schedule_answer_line") == 1, [m["role"] for m in got])
lm = _REAL_LOCAL_MESSAGES(got)
check("…and the question still gets /no_think with the answer line after it (Qwen3 thinks to empty otherwise)",
      lm[-2]["role"] == "user" and lm[-2]["content"].endswith("/no_think") and "/no_think" not in lm[-1]["content"],
      str([(m["role"], str(m["content"])[-12:]) for m in lm]))

# #1064 B3 (Box's B2 arm: mac reminders_tomorrow 2/3 → 0/3): reminders say outright which are due today and tomorrow,
# and a reminders question gets that line after it, never the calendar's
rd = sd.reminders_digest(REM, NOW)
check("reminders: due today and due tomorrow, said outright",
      "- Due today (Thu Oct 1): Pay the water bill (due Thu Oct 1, 8:25 AM); Pick up dry cleaning (due Thu Oct 1, 12:50 PM)" in rd
      and "- Due tomorrow (Fri Oct 2): Renew the car registration (due Fri Oct 2)" in rd, rd)
check("…a reminders answer line for 'tomorrow'", sd.reminders_answer_line("Which reminders are due tomorrow?", rd)
      == "[From the reminders above, for this question: Due tomorrow (Fri Oct 2): Renew the car registration (due Fri Oct 2)]",
      sd.reminders_answer_line("Which reminders are due tomorrow?", rd))
check("…none for a question with no day", sd.reminders_answer_line("Which of my reminders has no due date?", rd) == "")
lib.execute_tool = lambda name, args: F if name == "list_calendar_events" else REM
rq = "Which reminders are due tomorrow?"
rmsgs = lib._with_schedule_digest([{"role": "system", "content": "sys"}, {"role": "user", "content": rq}])
check("the turn: a reminders question ends with the reminders' due-tomorrow line, never a calendar line",
      str(rmsgs[-1].get("content")).startswith("[From the reminders above") and "car registration" in str(rmsgs[-1].get("content"))
      and not any("[From the calendar above" in str(m.get("content")) for m in rmsgs), str([str(m.get("content"))[:40] for m in rmsgs]))
check("…and its question keeps /no_think", _REAL_LOCAL_MESSAGES(rmsgs)[-2]["content"].endswith("/no_think"))

# #1064 follow-up (Box's option 3): "free at <time>" answered in code from the day's own line
for label, q, want in (
        ("free at 3:30 tomorrow → busy, both overlapping events named", "Am I free tomorrow at 3:30 PM?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Tomorrow (Fri Oct 2) at 3:30 PM: busy (3 PM–4 PM Piano lesson; "
         "3:30 PM–4:30 PM Leo soccer)]"),
        ("free at 11 tomorrow → free (the all-day trip takes no time)", "Am I free tomorrow at 11 AM?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Tomorrow (Fri Oct 2) at 11 AM: free (nothing timed then)]"),
        ("an event's end is free (4 PM: piano is over, soccer isn't)", "Am I busy tomorrow at 4 pm?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: Tomorrow (Fri Oct 2) at 4 PM: busy (3:30 PM–4:30 PM Leo soccer)]"),
        ("no time in the question → the day's line, as before", "Am I free tomorrow?",
         "[From the calendar above, read just now (it outranks any saved note), for this question: " + next(l[2:] for l in fd.splitlines() if l.startswith("- Tomorrow")) + "]"),
        ("a time with no day → nothing (no guessing which day)", "Am I free at 3 PM?", "")):
    got = sd.answer_line(q, fd)
    check(f"free-at: {label}", got == want, got)

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all schedule digest checks passed'}")
sys.exit(1 if failures else 0)
