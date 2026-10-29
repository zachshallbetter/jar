#!/usr/bin/env python3
"""Calendar and reminder listings in order and marked against now (schedule_lines). Hermetic.
Run: python3 scripts/test_schedule_lines.py"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))
import schedule_lines as S  # noqa: E402

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 1, 9, 30, tzinfo=TZ)   # Thursday
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"\n      got:      {got!r}\n      expected: {expected!r}"))


cal = "\n".join([
    "• Fri Oct 2, 3:00 PM–4:00 PM — Piano lesson",
    "• Thu Oct 1, 9:00 AM–10:00 AM — Dentist [at: Pearl Dental]",
    "• Fri Oct 2, all day — Ava's field trip",
    "• Fri Oct 2, 3:30 PM–4:30 PM — Leo soccer [calendar: Family, shared]",
    "• Thu Oct 1, 8:00 AM–8:30 AM — Standup",
    "• Thu Oct 1, 11:00 AM — Call with Sam",
    "• Mon Oct 5, all day – Wed Oct 7 — Spokane trip",
    "• Thu Oct 1, 10:00 PM – Fri Oct 2, 1:00 AM — Movie",
])
out = S.order_events(cal, NOW).split("\n")
check("events: sorted by start (all-day first on its day)", [l.split(" — ")[1].split(" [")[0] for l in out],
      ["Standup", "Dentist", "Call with Sam", "Movie", "Ava's field trip", "Piano lesson", "Leo soccer", "Spokane trip"])
check("…the one under way: now, until when", out[1], "• Thu Oct 1, 9:00 AM–10:00 AM — Dentist [at: Pearl Dental] [now · until 10:00 AM]")
check("…the next: next, in how long (an old line with no end)", out[2], "• Thu Oct 1, 11:00 AM — Call with Sam [next · in 1 h 30 min]")
check("…done today: ended", out[0], "• Thu Oct 1, 8:00 AM–8:30 AM — Standup [ended]")
check("…past midnight", out[3], "• Thu Oct 1, 10:00 PM – Fri Oct 2, 1:00 AM — Movie [in 12 h 30 min]")
check("…all day tomorrow", out[4], "• Fri Oct 2, all day — Ava's field trip [tomorrow]")
check("…overlaps, both ways", (out[5], out[6].split(" [", 2)[-1]),
      ("• Fri Oct 2, 3:00 PM–4:00 PM — Piano lesson [tomorrow · overlaps Leo soccer]", "tomorrow · overlaps Piano lesson]"))
check("…the next one tomorrow", S.order_events("• Fri Oct 2, 9:00 AM–10:00 AM — Dentist", NOW),
      "• Fri Oct 2, 9:00 AM–10:00 AM — Dentist [next · tomorrow]")
check("…a multi-day trip", out[7], "• Mon Oct 5, all day – Wed Oct 7 — Spokane trip [in 4 days]")
check("no events: unchanged", S.order_events("No events between Thu Oct 01 and Fri Oct 02.", NOW),
      "No events between Thu Oct 01 and Fri Oct 02.")
check("a header line stays first", S.order_events("Events (2):\n• Thu Oct 1, 11:00 AM — B\n• Thu Oct 1, 9:00 AM — A", NOW).split("\n")[0],
      "Events (2):")
check("an all-day trip under way", S.order_events("• Tue Sep 29, all day – Fri Oct 2 — Kids with Mom", NOW),
      "• Tue Sep 29, all day – Fri Oct 2 — Kids with Mom [under way, until Fri Oct 2]")
check("a.m. with dots and a narrow space", S.order_events("• Thu Oct 1, 11:00 a.m. — Late form", NOW),
      "• Thu Oct 1, 11:00 a.m. — Late form [next · in 1 h 30 min]")
check("a date late last year (seen in January)",
      S.order_events("• Wed Dec 30, 9:00 AM — Old", datetime(2027, 1, 2, 9, 0, tzinfo=TZ)).endswith("[past]"), True)

# Review fixes
check("year: the weekday decides (an old overdue one, not 106 days out)",
      S.order_reminders("• Old (due Thu Jan 15, 9:00 AM)", NOW), "• Old (due Thu Jan 15, 9:00 AM) [overdue since Thu Jan 15]")
check("year: next April when the weekday says so", S.order_reminders("• Taxes (due Mon Apr 12)", NOW),
      "• Taxes (due Mon Apr 12) [in 193 days]")
check("no end, started earlier today: never 'ended'", S.order_events("• Thu Oct 1, 8:00 AM — Workshop", NOW),
      "• Thu Oct 1, 8:00 AM — Workshop [started at 8:00 AM]")
check("under way for days: the end's day", S.order_events("• Mon Sep 28, 3:00 PM – Thu Oct 8, 3:00 PM — Kids with Mom", NOW),
      "• Mon Sep 28, 3:00 PM – Thu Oct 8, 3:00 PM — Kids with Mom [now · until Thu Oct 8, 3:00 PM]")
check("a title's own brackets stay in overlaps", S.order_events(
      "• Thu Oct 1, 11:00 AM–12:00 PM — Standup [remote]\n• Thu Oct 1, 11:30 AM–12:30 PM — Call [calendar: Home]", NOW).split("\n")[1],
      "• Thu Oct 1, 11:30 AM–12:30 PM — Call [calendar: Home] [in 2 h · overlaps Standup [remote]]")
check("a week-long event doesn't overlap everything", S.order_events(
      "• Mon Sep 28, 3:00 PM – Thu Oct 8, 3:00 PM — Kids with Mom\n• Thu Oct 1, 11:00 AM–12:00 PM — Dentist", NOW).split("\n")[1],
      "• Thu Oct 1, 11:00 AM–12:00 PM — Dentist [next · in 1 h 30 min]")
check("an older iPhone's 12:00 AM is a day-only due date", S.order_reminders("• Milk (due Thu Oct 1, 12:00 AM)", NOW),
      "• Milk (due Thu Oct 1, 12:00 AM) [due today]")
check("a span's end is never before its start (Dec 30 – Jan 2)",
      S.order_events("• Wed Dec 30, all day – Sat Jan 2 — Holidays", datetime(2026, 12, 31, 10, 0, tzinfo=TZ)),
      "• Wed Dec 30, all day – Sat Jan 2 — Holidays [under way, until Sat Jan 2]")

check("a repeating event's note isn't part of its title (overlaps name the event)", S.order_events(
      "• Thu Oct 1, 11:00 AM–12:00 PM — Garbage out [calendar: Home] [repeats: every week on Thursday]\n"
      "• Thu Oct 1, 11:30 AM–12:30 PM — Call", NOW).split("\n")[1],
      "• Thu Oct 1, 11:30 AM–12:30 PM — Call [in 2 h · overlaps Garbage out]")

rem = "\n".join([
    "• Call the plumber",
    "• Pay the water bill (due Thu Oct 1, 8:00 AM)",
    "• Renew registration (due Mon Oct 5)",
    "• Pick up dry cleaning (due Thu Oct 1, 5:00 PM)",
    "• Return library books (due Tue Sep 29)",
    "• Buy milk (due Thu Oct 1)",
])
r = S.order_reminders(rem, NOW).split("\n")
check("reminders: overdue first, then soonest, no due date last", [l.split(" (due")[0].split(" [")[0] for l in r],
      ["• Return library books", "• Buy milk", "• Pay the water bill", "• Pick up dry cleaning", "• Renew registration", "• Call the plumber"])
check("…overdue by", r[2], "• Pay the water bill (due Thu Oct 1, 8:00 AM) [overdue by 1 h 30 min]")
check("…a day-only one from days ago", r[0], "• Return library books (due Tue Sep 29) [overdue since Tue Sep 29]")
check("…due today, no time", r[1], "• Buy milk (due Thu Oct 1) [due today]")
check("…due later today", r[3], "• Pick up dry cleaning (due Thu Oct 1, 5:00 PM) [due today · in 7 h 30 min]")
check("…in days", r[4], "• Renew registration (due Mon Oct 5) [in 4 days]")
check("no reminders: unchanged", S.order_reminders("No incomplete reminders.", NOW), "No incomplete reminders.")
check("span words", [S.span(30), S.span(300), S.span(7200), S.span(7500), S.span(3 * 86400)],
      ["under a minute", "5 min", "2 h", "2 h 5 min", "3 days"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
