#!/usr/bin/env python3
"""#344: replies checked against the calendar and reminders the turn read. The cases are the truth eval's
round-5 replies (10-01) over its fixtures (truth_eval.event_line / reminder_answer formats). Hermetic."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claim_checks as cc  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


NOW = datetime(2026, 10, 1, 9, 55)
CAL = """• Fri Oct 2, all day — Ava's field trip
• Fri Oct 2, 9:00 AM–10:00 AM — Dentist [at: Pearl Dental]
• Fri Oct 2, 3:00 PM–4:00 PM — Piano lesson
• Fri Oct 2, 3:30 PM–4:30 PM — Leo soccer
• Sat Oct 3, 7:00 PM–9:00 PM — Team dinner
• Mon Oct 5, all day – Wed Oct 7 — Spokane trip"""
REM = """• Return library books (due Tue Sep 29)
• Pay the water bill (due Thu Oct 1, 8:25 AM)
• Pick up dry cleaning (due Thu Oct 1, 12:50 PM)
• Renew the car registration (due Fri Oct 2)
• Call the plumber"""
CAL_READ = [{"tool": "list_calendar_events", "text": CAL}]
REM_READ = [{"tool": "list_reminders", "text": REM}]


def notes(reply, question, reads):
    return cc.notes(reply, question, reads, NOW)


# Denials the tools contradict (local misses).
n = notes("I don't have any calendar events for piano tomorrow. Would you like me to check?", "What time is piano tomorrow?", CAL_READ)
check("'no events for piano' with piano in the calendar: it's there", len(n) == 1 and "Piano lesson" in n[0], str(n))
n = notes("I don't have information about the Spokane trip.", "When is the Spokane trip and how many days is it?", CAL_READ)
check("'no information about the Spokane trip': it's there", n and "Spokane trip" in n[0], str(n))
n = notes("I don't have any information about your dentist appointment.", "When is my dentist appointment?", [])
check("nothing read at all: it says it didn't check", n == ["I didn't check your calendar or reminders for this; ask again and I'll look"], str(n))
n = notes("I don't have any information about that band.", "Who sang that song we talked about?", [])
check("chit-chat with a denial, nothing read: no 'didn't check' line", n == [], str(n))
n = notes("I don't have any information about a vet visit.", "When is the vet?", CAL_READ)
check("a denial the tools agree with is left alone", n == [], str(n))
n = notes("You have no events on Sunday.", "Do I have anything on Sunday?", CAL_READ)
check("'no events on Sunday' (true) is left alone", n == [], str(n))

# All-day events given a time.
n = notes("You have a field trip with Ava tomorrow at 11 AM. Would you like me to reschedule?", "Am I free tomorrow at 11 AM?", CAL_READ)
check("an all-day event stated at 11 AM: corrected", len(n) == 1 and "all day" in n[0] and "11 AM" in n[0], str(n))
n = notes("You're free at 11 AM; Ava's field trip is all day.", "Am I free tomorrow at 11 AM?", CAL_READ)
check("a reply that already says all day, and free: left alone", n == [], str(n))
n = notes("You're not free at 11 AM because of Ava's field trip.", "Am I free tomorrow at 11 AM?", CAL_READ)
check("'not free at 11 because of the field trip': corrected", len(n) == 1 and "all day" in n[0], str(n))
n = notes("At 9 AM you have the dentist, during Ava's field trip.", "What's at 9?", CAL_READ)
check("a timed event covering the time: no correction", n == [], str(n))

# Overdue.
n = notes("Nothing is overdue. The October bill is due on October 27th.", "Is anything overdue?", REM_READ)
check("'nothing is overdue' with overdue reminders: lists them",
      len(n) == 1 and "water bill" in n[0] and "library books" in n[0] and "dry cleaning" not in n[0], str(n))
n = notes("Yes, two items are overdue: Return library books and Pay the water bill.", "Is anything overdue?", REM_READ)
check("a right answer about overdue is left alone", n == [], str(n))
n = notes("Nothing is overdue.", "Is anything overdue?", [{"tool": "list_reminders", "text": "• Call the plumber"}])
check("'nothing is overdue' when nothing is: left alone", n == [], str(n))

n = notes("Nothing is overdue. The October bill is due on October 27th.", "Is anything overdue?", [])
check("'nothing is overdue' without reading reminders: says it didn't check", n == ["I didn't check your reminders for this; ask again and I'll look"], str(n))
n = notes('The next reminder is to "Renew the car registration" due tomorrow (Fri Oct 2).', "Which reminder comes up next?", REM_READ)
check("a later reminder named as next: the earlier one is named", len(n) == 1 and "dry cleaning" in n[0], str(n))
n = notes("Next up is Pick up dry cleaning at 12:50 PM.", "Which reminder comes up next?", REM_READ)
check("the right next reminder: left alone", n == [], str(n))

MARKED = [{"tool": "list_reminders", "text": "• Pay the water bill (due Thu Oct 1, 8:25 AM) [overdue by 1 h 30 min]\n"
                                          "• Pick up dry cleaning (due Thu Oct 1, 12:50 PM) [due today · in 3 h]"}]
n = notes("Nothing is overdue.", "Is anything overdue?", MARKED)
check("reminder lines with markers still back the overdue check (truth r7)", len(n) == 1 and "water bill" in n[0], str(n))

# Gemini's right answers from round 5 pass untouched.
for r in ["You have a conflict tomorrow from 3:00 to 4:00 PM between Piano lesson and Leo soccer (which runs 3:30 to 4:30 PM).",
          "The Spokane trip is from Monday, October 5 to Wednesday, October 7, 2026, and it is 3 days long.",
          "Saturday has a team dinner from 7:00 PM to 9:00 PM.",
          "Piano tomorrow is from 3:00 PM to 4:00 PM (it overlaps with Leo's soccer at 3:30)."]:
    check(f"right answer untouched: {r[:40]}…", notes(r, "q", CAL_READ + REM_READ) == [])

# The wiring: weekday forms the first check missed, and the reply pipeline.
import agent_runtime_lib as lib  # noqa: E402
lib._local_now = lambda: NOW.replace(tzinfo=None)
check("'October 13, 2026 is a Wednesday' is corrected",
      lib._with_true_weekdays("October 13, 2026 is a Wednesday.") == "October 13, 2026 is a Tuesday.")
check("'Sunday (October 2)' is corrected in place",
      lib._with_true_weekdays("You have events on Sunday (October 2).") == "You have events on Friday (October 2).")
check("'October 13 is a Tuesday' (true) is left alone", lib._with_true_weekdays("October 13 is a Tuesday.") == "October 13 is a Tuesday.")
lib._TURN_IO.outcomes = []
lib._record_outcome("list_calendar_events", {}, CAL)
check("a read keeps its full text for the turn", lib._TURN_IO.outcomes[-1].get("full", "").endswith("Spokane trip"))
out = lib._with_checked_answers("I don't have any calendar events for piano tomorrow.", "What time is piano tomorrow?")
check("wired: the reply gets one Correction line", out.count("\nCorrection: ") == 1 and "Piano lesson" in out, out)
lib._TURN_IO.outcomes = []
lib._record_outcome("list_calendar_events", {"start": "2026-10-02T00:00:00", "end": "2026-10-02T23:59:59"}, CAL)
out = lib._with_checked_answers("Tomorrow you have the dentist at 9. You're free on Saturday.", "this weekend?")
check("wired: a read's range is kept, and a claim past it is corrected",
      (lib._TURN_IO.outcomes[-1]["range"], out.endswith("I didn't read your calendar for Saturday, October 3; ask about that "
                                                        "day and I'll check it.")), (["2026-10-02T00:00:00", "2026-10-02T23:59:59"], True))
lib._TURN_IO.outcomes = None
check("no turn context: untouched", lib._with_checked_answers("I don't have any events.", "q") == "I don't have any events.")

print("\n── latest: the timeline, newest first ──")
TL = """Household timeline, newest first (now Thu Oct 1 09:55):
- Thu Oct 1 09:40 (15 min ago) ⟦untrusted source=timeline from=robin n=1a2b3c4d⟧Picked up Leo from soccer⟦/untrusted n=1a2b3c4d⟧
- Thu Oct 1 08:10 (1 h ago) Nightly backup finished
- Wed Sep 30 17:12 (yesterday) Filed a bill from PG&E"""
TL_READ = [{"tool": "recall_events", "text": TL}]
got = notes("The most recent entry is the nightly backup finishing at 8:10.", "what happened last?", TL_READ)
check("an older entry called the most recent: the newest one is stated, without its markers",
      got == ["the most recent timeline entry is from Thu Oct 1 09:40 (15 min ago): Picked up Leo from soccer"], str(got))
check("naming the newest: nothing", notes("Latest: Robin picked up Leo from soccer.", "q", TL_READ) == [])
check("no 'latest' claim: nothing", notes("The nightly backup finished at 8:10.", "q", TL_READ) == [])
check("a latest claim naming no entry: nothing", notes("The latest is that things are quiet.", "q", TL_READ) == [])
check("two listings read (different filters): nothing", notes("The most recent entry is the nightly backup.", "q",
      TL_READ + TL_READ) == [])
check("a calendar read isn't a timeline", notes("The most recent entry is the nightly backup.", "q", CAL_READ) == [])

print("\n── range: a calendar claim about a day the turn didn't read (NOW is Thu Oct 1) ──")
FRI = [{"tool": "list_calendar_events", "text": CAL, "range": ["2026-10-02T00:00:00", "2026-10-02T23:59:59"]}]
WEEK = [{"tool": "list_calendar_events", "text": CAL, "range": ["2026-09-30T00:00:00", "2026-10-08T23:59:59"]}]
got = notes("Tomorrow you have the dentist at 9. You're free on Saturday.", "what's on this weekend?", FRI)
check("Friday read, a claim about Saturday: corrected with the day",
      got == ["I didn't read your calendar for Saturday, October 3; ask about that day and I'll check it"], str(got))
check("the week read (the local digest's window): nothing", notes("You're free on Saturday.", "q", WEEK) == [])
check("the claimed day read: nothing", notes("Tomorrow you have the dentist at 9.", "q", FRI) == [])
check("'tomorrow' read as the model's own words ('tomorrow' to 'tomorrow')",
      notes("You have nothing on tomorrow after the piano lesson.", "q",
            [{"tool": "list_calendar_events", "text": CAL, "range": ["tomorrow", "tomorrow"]}]) == [])
check("a date by name outside the read", notes("You have nothing scheduled on October 9.", "q", FRI)
      == ["I didn't read your calendar for Friday, October 9; ask about that day and I'll check it"])
check("a reminder due another day isn't the calendar", notes("You have a reminder due Saturday to pay the bill.", "q", FRI) == [])
check("a sentence with no calendar claim: nothing", notes("Saturday looks sunny.", "q", FRI) == [])
check("a read whose range isn't known (an older app, a default): nothing",
      notes("You're free on Saturday.", "q", [{"tool": "list_calendar_events", "text": CAL}]) == [])
check("no calendar read at all: not this check's (check_denial says 'didn't check')",
      notes("You're free on Saturday.", "q", REM_READ) == [])

print("\n── source and omitted: truth r7's calendar misses as regression cases ──")
KB_READ = [{"tool": "kb_retrieve", "text": "Take out garbage: every Wednesday, 8:00 PM"}]
got = notes("The garbage goes out every Wednesday from 8:00 PM to 9:00 PM, according to the shared household calendar.",
            "How often does the garbage go out?", KB_READ)
check("r7 gemini garbage_how_often: cites the calendar it never read",
      got == ["I didn't read the calendar this turn; that came from what I remember and may be out of date"], str(got))
check("…citing it after reading it: nothing", notes("According to your calendar, the dentist is at 9.", "q", CAL_READ) == [])
check("…no citation: nothing", notes("The garbage goes out every Wednesday.", "q", KB_READ) == [])
R7_TOMORROW = ("Tomorrow (Friday, October 2):\n- 9 AM–10 AM: Dentist\n- 3 PM–4 PM: Piano lesson\n- 3:30 PM–4:30 PM: Leo soccer")
got = notes(R7_TOMORROW, "What's on my calendar tomorrow?", CAL_READ)
check("r7 local tomorrow_list: the all-day field trip left out",
      got == ["the calendar also has “Ava's field trip” (all day) on Friday"], str(got))
check("…the whole day listed: nothing", notes(R7_TOMORROW + "\n- All day: Ava's field trip", "What's on tomorrow?",
                                             CAL_READ) == [])
check("…a question about one time isn't a listing", notes("At 3:30 you have the piano lesson and Leo soccer.",
                                                          "Am I free tomorrow at 3:30 PM?", CAL_READ) == [])
WEEKLY = CAL + "\n• Mon Oct 5, 3:00 PM–4:00 PM — Piano lesson\n• Mon Oct 5, 3:30 PM–4:30 PM — Leo soccer\n• Mon Oct 5, 6:00 PM–7:00 PM — Book club"
check("…a weekly event named for Friday doesn't make Monday a listing",
      notes(R7_TOMORROW + "\n- All day: Ava's field trip", "What's on tomorrow?",
            [{"tool": "list_calendar_events", "text": WEEKLY}]) == [])
check("…a listing that doesn't name its day: nothing", notes("You have the dentist, the piano lesson and Leo soccer.",
                                                             "What's on my calendar?", CAL_READ) == [])
check("…naming one event isn't a listing", notes("Tomorrow you have the dentist at 9.", "What's on tomorrow?", CAL_READ) == [])

print("\n── day words resolved in code, for the local model (#423; the 10-02 nightly's misses; NOW is Thu Oct 1) ──")
from datetime import date as _d  # noqa: E402
T0 = NOW.date()
cases = {"What's on Saturday?": [("Saturday", _d(2026, 10, 3))],
         "Do I have anything on Sunday?": [("Sunday", _d(2026, 10, 4))],
         "What was on my calendar yesterday evening?": [("yesterday", _d(2026, 9, 30))],
         "What day of the week is October 13?": [("October 13", _d(2026, 10, 13))],
         "What was on Monday?": [("Monday", _d(2026, 9, 28))],
         "What's on Thursday?": [("Thursday", _d(2026, 10, 1))],
         "Anything next Thursday?": [("Thursday", _d(2026, 10, 8))],
         "What did I do last Saturday?": [("Saturday", _d(2026, 9, 26))],
         "What's on the day after tomorrow?": [("the day after tomorrow", _d(2026, 10, 3))],
         "When is the Spokane trip?": []}
for q, want in cases.items():
    check(f"asked_days: {q}", cc.asked_days(q, T0) == want, str(cc.asked_days(q, T0)))
check("range: Friday read for Saturday is moved to Saturday",
      cc.range_for_question("2026-10-02", "2026-10-02", [_d(2026, 10, 3)], T0) == ("2026-10-03T00:00:00", "2026-10-03T23:59:59"))
check("range: 'yesterday evening' read on today keeps its hours, on yesterday",
      cc.range_for_question("2026-10-01T17:00", "2026-10-01T20:00", [_d(2026, 9, 30)], T0) == ("2026-09-30T17:00", "2026-09-30T20:00"))
check("range: the digest's week covers Saturday: unchanged",
      cc.range_for_question("2026-09-30T00:00:00", "2026-10-08T23:59:59", [_d(2026, 10, 3)], T0) is None)
check("range: 'tomorrow' (the model's own word) for tomorrow: unchanged",
      cc.range_for_question("tomorrow", "tomorrow", [_d(2026, 10, 2)], T0) is None)
check("range: a range it can't read is left alone", cc.range_for_question("next week", "", [_d(2026, 10, 3)], T0) is None)

print("\n── wired into the local loop ──")
lib._TURN_INFO.info = {}
replies = iter(['<tool_call>{"tool": "list_calendar_events", "args": {"start": "2026-10-02", "end": "2026-10-02"}}</tool_call>',
                "Saturday: Team dinner at 7 PM."])
seen_msgs, ran = [], []
real_http, real_exec = lib._http_json, lib.execute_tool
lib._http_json = lambda url, payload=None, timeout=30: (seen_msgs.append([m["content"] for m in payload["messages"]]) or
                 {"choices": [{"message": {"content": next(replies)}}]})
lib.execute_tool = lambda name, args: ran.append((name, dict(args.get("args") or args))) or "• Sat Oct 3, 7:00 PM–9:00 PM — Team dinner"
lib._TURN_IO.device, lib._TURN_IO.role = (lambda req: {"result": ""}), "admin"   # a Mac turn: calendar tools offered
try:
    msgs = lib._with_named_dates([{"role": "system", "content": "sys"}, {"role": "user", "content": "What's on Saturday?"}])
    out = lib._run_local_agent(msgs, endpoint="http://x", model="m")
finally:
    lib._http_json, lib.execute_tool = real_http, real_exec
    lib._TURN_IO.device = lib._TURN_IO.role = None
check("the model is told Saturday's date before it answers", any("Saturday = Saturday, October 3, 2026" in c for c in seen_msgs[0]),
      str(seen_msgs[0][-2:]))
check("its Friday read runs as Saturday's", ran == [("list_calendar_events", {"start": "2026-10-03T00:00:00",
                                                                             "end": "2026-10-03T23:59:59"})], str(ran))
check("…and it's told why, with the result", "the question names Sat Oct 3" in seen_msgs[1][-1], seen_msgs[1][-1][:200])
check("counted for the turn log", lib._TURN_INFO.info.get("calendar_args_fixed") == 1)
check("no day words: the messages are untouched", lib._with_named_dates([{"role": "user", "content": "hi"}]) == [{"role": "user", "content": "hi"}])

print("\n── the local model asked once more when a claim check fires (#423) ──")
lib._TURN_INFO.info = {}
lib._TURN_IO.outcomes = []
lib._record_outcome("list_reminders", {}, REM)
q = [{"role": "user", "content": "Is anything overdue?"}]
asked: list = []


def runner(reply):
    def run(msgs):
        asked.append(msgs[-1]["content"])
        return reply
    return run


out = lib._local_claim_retry("Nothing is overdue.", q, runner("Yes: the water bill and the library books are overdue."))
check("a reply its check catches is asked again, with the check's notes as the instruction",
      (out, "Return library books" in asked[0], lib._TURN_INFO.info.get("claim_retry"), lib._TURN_INFO.info.get("claim_retry_fixed")),
      str((out, asked[:1], lib._TURN_INFO.info)))
lib._TURN_INFO.info, asked = {}, []
check("a right reply isn't asked again", (lib._local_claim_retry("The water bill and the library books are overdue.", q,
      runner("x")), asked), ("The water bill and the library books are overdue.", []))
check("still wrong after the retry: kept, not marked fixed (the usual Correction follows)",
      (lib._local_claim_retry("Nothing is overdue.", q, runner("Nothing is overdue at all.")),
       lib._TURN_INFO.info.get("claim_retry_fixed")) == ("Nothing is overdue at all.", None))
lib._TURN_INFO.info = {}


def boom(msgs):
    raise lib.TurnBudgetExceeded("late")


check("out of time in the retry: the first answer stands", lib._local_claim_retry("Nothing is overdue.", q, boom),
      "Nothing is overdue.")
check("an empty retry: the first answer stands", lib._local_claim_retry("Nothing is overdue.", q, runner("  ")), "Nothing is overdue.")
lib._TURN_IO.outcomes.append({"tool": "create_reminder", "ok": True, "write": True, "text": "Added"})
asked = []
check("never after a write in the turn", (lib._local_claim_retry("Nothing is overdue.", q, runner("x")), asked),
      ("Nothing is overdue.", []))
lib._TURN_IO.outcomes = []
lib._record_outcome("list_reminders", {}, REM)
import time as _t  # noqa: E402
lib._TURN_IO.deadline, lib._TURN_IO.budget_s = _t.monotonic() + 30, 60   # 30 s in: past the 25 s cut-off
asked = []
check("not when the turn is already past ~25 s", (lib._local_claim_retry("Nothing is overdue.", q, runner("x")), asked),
      ("Nothing is overdue.", []))
lib._TURN_IO.deadline = lib._TURN_IO.budget_s = lib._TURN_IO.outcomes = None

print("\n── travel: arrive_by from the event the turn read (#423) ──")
EVS = cc.parse_events(CAL, NOW.date())
e = cc.event_for_destination("the dentist", EVS, [], NOW)
check("'the dentist' is tomorrow's 9 AM Dentist", (e.title, str(e.start), str(e.t0)) == ("Dentist", "2026-10-02", "09:00:00"),
      str(e))
check("…its place works too ('Pearl Dental')", cc.event_for_destination("Pearl Dental", EVS, [], NOW) is e)
check("a place no event names: nothing", cc.event_for_destination("the airport", EVS, [], NOW) is None)
check("an all-day event isn't a time to arrive by", cc.event_for_destination("Ava's field trip", EVS, [], NOW) is None)
check("a day the question names wins", cc.event_for_destination("dentist", EVS, [_d(2026, 10, 3)], NOW) is None)

lib._TURN_INFO.info = {}
lib._TURN_IO.outcomes = []
lib._record_outcome("list_calendar_events", {"start": "2026-09-30T00:00:00", "end": "2026-10-08T23:59:59"}, CAL)
replies = iter(['<tool_call>{"tool": "travel_time", "args": {"destination": "the dentist", "mode": "drive", '
                '"arrive_by": "2026-10-02T15:00"}}</tool_call>', "Leave by 8:42 AM."])
seen_msgs, ran = [], []
real_http, real_exec = lib._http_json, lib.execute_tool
lib._http_json = lambda url, payload=None, timeout=30: (seen_msgs.append([m["content"] for m in payload["messages"]]) or
                 {"choices": [{"message": {"content": next(replies)}}]})
lib.execute_tool = lambda name, args: ran.append((name, dict(args.get("args") or args))) or \
    "18 min by car to Pearl Dental (7.2 mi), with current traffic."
lib._TURN_IO.device, lib._TURN_IO.role, lib._TURN_IO.client = (lambda req: {"result": ""}), "admin", "iphone"
lib._TURN_IO.device_tools = lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS))
try:
    out = lib._run_local_agent([{"role": "system", "content": "sys"},
                                {"role": "user", "content": "When should I leave for the dentist tomorrow?"}],
                               endpoint="http://x", model="m")
finally:
    lib._http_json, lib.execute_tool = real_http, real_exec
    lib._TURN_IO.device = lib._TURN_IO.role = lib._TURN_IO.client = lib._TURN_IO.device_tools = None
    lib._TURN_IO.outcomes = None
check("the model's 15:00 becomes the dentist's 9:00", ran and ran[0][1].get("arrive_by") == "2026-10-02T09:00", str(ran))
check("…and the leave time is worked out here", (seen_msgs[1][-1].splitlines()[-1] if len(seen_msgs) > 1 else "")
      == "Arrive by 9:00 AM (Fri Oct 2, Dentist): leave by 8:42 AM.", str(seen_msgs[1:][-1:])[-300:])
check("counted for the turn log", lib._TURN_INFO.info.get("travel_args_fixed") == 1)
check("a result that already says Leave by is left as it is",
      lib._travel_leave_line("18 min. Leave by 8:42 AM.", datetime(2026, 10, 2, 9, 0)) == "18 min. Leave by 8:42 AM.")
check("an hour and a quarter", lib._travel_leave_line("1 h 15 min by car", datetime(2026, 10, 2, 9, 0)).endswith(
      "leave by 7:45 AM."))

print("\n── when: an event's date and days-until, against the read (#423; the 10-02 nightly's trip misses) ──")
got = notes("The Spokane trip is scheduled for October 2nd and lasts for 2 days.", "When is the Spokane trip?", CAL_READ)
check("r-10-02 trip_when: the trip on the wrong date", got == ["“Spokane trip” is Mon Oct 5 to Wed Oct 7"], str(got))
got = notes("There are 1 days until the Spokane trip.", "How many days until the Spokane trip?", CAL_READ)
check("r-10-02 days_until_trip: the wrong count", got == ["“Spokane trip” starts Mon Oct 5, 4 days from today"], str(got))
check("right date and count: nothing", notes("The Spokane trip is Monday, October 5, four days away.", "q", CAL_READ) == [])
check("a date inside a multi-day event: nothing", notes("You'll be on the Spokane trip on October 6.", "q", CAL_READ) == [])
WEEKLY2 = CAL + "\n• Fri Oct 9, 3:00 PM–4:00 PM — Piano lesson"
check("a title that repeats: not this check's", notes("Your piano lesson is on October 9.", "q",
      [{"tool": "list_calendar_events", "text": WEEKLY2}]) == [])
lib._local_now = lambda: NOW.replace(tzinfo=None)
line = lib._with_named_dates([{"role": "user", "content": "What day of the week is October 13?"}])[0]["content"]
check("the dates line gives the weekday and how far away", "October 13 = Tuesday, October 13, 2026 (in 12 days)" in line, line)

print("\n── first: 'which happened first' over a newest-first listing (#423) ──")
CTX = """Recent household activity, newest first (now Thu Oct 1 09:55):
- Thu Oct 1 09:40 (15 min ago) Filled up gas ($4.90/gal)
- Thu Oct 1 08:10 (1 h ago) Sent a message to the school
- Wed Sep 30 17:12 (yesterday) Filed a bill from PG&E"""
Q = "Which happened first: “Filled up gas ($4.90/gal)” or “Sent a message to the school”? Name just that one."
got = notes('The "Filled up gas ($4.90/gal)" happened first.', Q, [{"tool": "context_timeline", "text": CTX}])
check("the later one named first: corrected, from the turn's own context listing",
      got == ["“Sent a message to the school” came first (Thu Oct 1 08:10), then “Filled up gas ($4.90/gal)” (Thu Oct 1 09:40)"],
      str(got))
check("the earlier one named: nothing", notes("Sent a message to the school.", Q, [{"tool": "context_timeline", "text": CTX}]) == [])
check("…from a recall_events read too", notes("Filled up gas happened first.", Q, [{"tool": "recall_events", "text": CTX}]) != [])
check("both named in the reply: not this check's", notes("Sent a message, then filled up gas.", Q,
                                                        [{"tool": "context_timeline", "text": CTX}]) == [])
check("not a which-first question: nothing", notes("Filled up gas.", "What did I do this morning?",
                                                  [{"tool": "context_timeline", "text": CTX}]) == [])
lib._TURN_IO.outcomes, lib._TURN_IO.context_timeline = [], CTX
check("wired: the context listing reaches the checks", lib._claim_notes('"Filled up gas" happened first.', Q) != [])
lib._TURN_IO.outcomes = lib._TURN_IO.context_timeline = None

print("── a source the person named but nothing read (#953 reopened) ──")
R = lambda *tools: [{"tool": t, "text": ""} for t in tools]  # noqa: E731
Q = "Check my email and my texts for anything from the dealer"
check("texts read, email silently skipped: corrected",
      (cc.check_asked_unread("Nothing from the dealer in your texts.", Q, R("search_messages")))
      == (["I couldn't check your email this turn"]))
check("…both read: nothing",
      (cc.check_asked_unread("Nothing in either.", Q, R("search_messages", "search_mail")))
      == ([]))
check("…the reply says email wasn't checked: nothing",
      ([cc.check_asked_unread(t, Q, R("search_messages")) for t in (
          "Nothing in your texts. I couldn't check your email.", "Nothing in your texts; I don't have access to your inbox.",
          "I wasn't able to look at your mail, but your texts have nothing.")])
      == ([[], [], []]))
check("…nothing read at all: both named",
      (cc.check_asked_unread("Nothing from the dealer.", Q, R()))
      == (["I couldn't check your email this turn", "I couldn't check your messages this turn"]))
check("a reply citing unread email gets one note, not two",
      (cc.notes("Your email says the dealer called.", Q, R("search_messages"), __import__("datetime").datetime(2026, 10, 5, 9)))
      == (["I didn't read your email this turn, so that wasn't from your email"]))
check("a question about what titan can do with the mail isn't a request to read it (10-16 live)",
      ([cc.check_asked_unread("I can search, mark and restore it.", q, R("capabilities")) for q in (
          "what can you do with my mail now?", "What else could you help with in my inbox?",
          "what are your capabilities with my email?")])
      == ([[], [], []]))
check("…but asking it to check still asks", cc.check_asked_unread("Here's what I can do.", "can you check my mail for the plumber?",
                                                                   R()) == ["I couldn't check your email this turn"])
check("the server mail reads count as reading email (#1642, #1644)",
      [cc.check_asked_unread("Two are waiting.", "who's waiting on a reply in my email?", R(t))
       for t in ("mail_awaiting_reply", "mail_move_preview", "mail_unsubscribe_preview")] == [[], [], []])
print("── a guessed cause for a failure (#1610, live 10-16) ──")
QW = "Are you sure? Home Assistant runs locally, it doesn't use the cloud."
GUESS = ("Home Assistant runs locally on Titan rather than in the cloud. A timeout would stem from local network routing "
         "over the tailnet or LAN, or container response lag on the server.")
check("a hedged cause nothing read shows: noted as a guess",
      cc.check_unsupported_cause(GUESS, QW, R("host_status")) == ["nothing I checked shows what caused it; that was a guess"])
check("…backed when a tool's output names that cause",
      cc.check_unsupported_cause(GUESS, QW, [{"tool": "titan_service", "text": "error: network unreachable"}]) == [])
check("…an honest 'I don't know' needs no backing",
      cc.check_unsupported_cause("I don't know what caused it; it could stem from many things.", QW, []) == [])
check("…and it's only for a question about why something failed",
      cc.check_unsupported_cause("It was probably due to network lag.", "When is my dentist?", []) == [])
check("…wired into notes()", "nothing I checked shows what caused it; that was a guess" in
      cc.notes(GUESS, QW, R("host_status"), __import__("datetime").datetime(2026, 10, 16, 9)))
check("words that don't name a source to check: nothing",
      ([cc.check_asked_unread("Done.", q, R()) for q in ("Email Robin that I'm late", "Text Jordan I'm on my way",
                                                         "What's on my calendar?", "Draft a reply to the dealer")])
      == ([[], [], [], []]))
check("a place to put something isn't a source to check",
      [cc.check_asked_unread("Sent.", q, R()) for q in ("Send the receipt to my email", "Forward that to my email please",
                                                        "Save this into my notes", "Add it to my notes")]
      == [[], [], [], []])
check("…but looking in it still is",
      cc.check_asked_unread("Nothing.", "Is there anything in my email from Hale?", R()) == ["I couldn't check your email this turn"])
check("…my notes too",
      (cc.check_asked_unread("The vet is on Elm.", "What do my notes say about the vet?", R()))
      == (["I couldn't check your notes this turn"]))

print("── a source cited but not read (#953) ──")
R = lambda *tools: [{"tool": t, "text": ""} for t in tools]  # noqa: E731
check("the emails confirm, after only find_records: flagged",
      cc.check_unread_source("The emails confirm those bills were old notices from September.", R("find_records")) ==
      ["I didn't read your email this turn, so that wasn't from your email"])
check("…after reading the KB (where synced mail lives): fine",
      cc.check_unread_source("The emails confirm those bills were old notices.", R("kb_retrieve")) == [])
check("…after opening the item: fine", cc.check_unread_source("Your email says the order shipped.", R("open_item")) == [])
check("your messages say, no messages read: flagged",
      cc.check_unread_source("Your messages say Robin is picking up.", R("list_calendar_events")) ==
      ["I didn't read your messages this turn, so that wasn't from your messages"])
check("…search_messages read them: fine", cc.check_unread_source("Your texts say Robin is picking up.", R("search_messages")) == [])
check("notes and documents too", (bool(cc.check_unread_source("According to your notes, the vet is on Elm.", R())),
                                  bool(cc.check_unread_source("The letter says $42 is due.", R("find_records")))) == (True, True))
check("a statement after the bill tools (records filed from mail): fine",
      [cc.check_unread_source("The statement shows $40 due on the 12th.", R(t)) for t in ("find_records", "open_obligations")]
      == [[], []])
check("…after nothing that reads one: corrected",
      cc.check_unread_source("The statement shows $40 due.", R("list_calendar_events")) ==
      ["I didn't read that statement this turn, so that wasn't from that statement"])
check("search_mail reads email (#955)", cc.check_unread_source("Your email says the order shipped.", R("search_mail")) == [])
check("words that don't cite a source: nothing",
      [cc.check_unread_source(t, R()) for t in ("I'll check your email.", "Do you want me to search your messages?",
                                                "Emails from Maple Health go to your inbox.", "I can read your notes if you like.")]
      == [[], [], [], []])
check("in notes() with the other checks", "I didn't read your email this turn, so that wasn't from your email" in
      cc.notes("The emails confirm it was paid.", "was it paid?", R("find_records"), NOW))

# 10-10: "the text 'canary bird'" read as the Messages app put a source retry (+2 model calls) on every canary turn
CANARY_Q = "Use the count_chars tool on the text 'canary bird' and reply with only the total number of letters."
check("a piece of text isn't the Messages app (the canary's question)", cc.asked_unread(CANARY_Q, "10", []) == [])
check("…nor 'summarize the text I pasted'", cc.asked_unread("summarize the text I pasted", "", []) == [])
check("…but 'check my texts' still is", [k for k, _, _ in cc.asked_unread("check my texts from Robin", "", [])] == ["messages"])
check("…and 'did the texts say' still is", [k for k, _, _ in cc.asked_unread("did the texts say when?", "", [])] == ["messages"])
check("…and 'what do the messages say' still is",
      [k for k, _, _ in cc.asked_unread("what do the messages say", "", [])] == ["messages"])

print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all claim checks passed'}")
sys.exit(1 if failures else 0)
