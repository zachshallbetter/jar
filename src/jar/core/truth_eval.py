#!/usr/bin/env python3
"""Truth eval: does titan get the facts right about time, the calendar, the schedule, the timeline and
the knowledge base? (Zach, 10-01: "knowledge and information refinement and tuning: calendar,
schedule, timeline, self verification".)

Each question goes through titan's HTTP API as an eval turn (source "eval": nothing remembered,
logged or shown), pinned to one brain, with a hard tool allowlist (`eval_tools`: read tools only,
so no turn can write anything whatever the model tries). The right answer is worked out here, in
code, at run time, never written by a model:

  - time: the household clock;
  - calendar and schedule: a realistic week placed around *now* (an event under way, the next one,
    an overlapping pair tomorrow, an all-day day, a multi-day trip, last evening's class), answered
    by a fake device like turn_eval's, so nothing touches a real calendar;
  - timeline and knowledge: titan's own read APIs for the same person (GET v1/timeline, GET v1/kb).

  python3 scripts/truth_eval.py [--brain local|gemini|both] [--case NAME] [-v]   (on titan)

Output: per-category scores, then "truth: N/M" per brain. Misses print the question, what was
expected and the reply, ready to become verification cases.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import turn_eval as te  # noqa: E402  (the eval turn, the token, the fake device loop)

TZ = te.TZ
# Read tools only. A tool off this list is refused by titan on this turn (agent_api_server
# _parse_eval_pins → agent_runtime_lib._role_allows).
LOCAL_WHERE: str | None = None   # #627's arm (--local-where short): the short identity on local turns
READ_TOOLS = sorted({
    "list_calendar_events", "list_reminders", "recall_events", "kb_retrieve", "kb_list", "kb_relations",
    "get_datetime", "get_weather", "skill_guide", "list_routines", "list_todos", "search_notes", "travel_time",
    "about",
})


# ── the week, around now ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Ev:
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    place: str | None = None
    repeats: str | None = None


def _fmt(d: datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0")


def week(now: datetime) -> list[Ev]:
    """A household's week placed around now. Minute-rounded so times read naturally."""
    n = now.replace(second=0, microsecond=0)
    n = n - timedelta(minutes=n.minute % 5)
    day = n.replace(hour=0, minute=0)
    tom = day + timedelta(days=1)
    return [
        Ev("Focus block", n - timedelta(minutes=20), n + timedelta(minutes=40)),
        Ev("Call with Sam", n + timedelta(minutes=50), n + timedelta(minutes=80)),
        Ev("Ballet", day - timedelta(days=1) + timedelta(hours=17, minutes=15), day - timedelta(days=1) + timedelta(hours=18, minutes=15)),
        Ev("Ava's field trip", tom, tom + timedelta(days=1), all_day=True),
        Ev("Dentist", tom + timedelta(hours=9), tom + timedelta(hours=10), place="Pearl Dental"),
        Ev("Piano lesson", tom + timedelta(hours=15), tom + timedelta(hours=16)),
        Ev("Leo soccer", tom + timedelta(hours=15, minutes=30), tom + timedelta(hours=16, minutes=30)),
        Ev("Team dinner", tom + timedelta(days=1, hours=19), tom + timedelta(days=1, hours=21)),
        Ev("Spokane trip", tom + timedelta(days=3), tom + timedelta(days=6), all_day=True),
    ] + compost(now)


def compost(now: datetime) -> list[Ev]:
    """A weekly "Compost out" at 8 PM on the weekday five days from now (clear of tomorrow, the day
    after and the empty day), as the apps list a repeating event: each occurrence with its
    "[repeats: every week on …]" note (jellybean's EventText, 0.13.6+)."""
    day = (now + timedelta(days=5)).replace(hour=20, minute=0, second=0, microsecond=0)
    rule = f"every week on {day:%A}"
    return [Ev("Compost out", day + timedelta(weeks=k), day + timedelta(weeks=k, minutes=15), repeats=rule)
            for k in (-1, 0, 1, 2)]


def calendar_answer(evs: list[Ev], args: dict) -> str:
    """Like the devices' list_calendar_events: events overlapping [start, end) (default today + 7 days)."""
    start = te._parse(args.get("start")) or te._today()
    raw_end = str(args.get("end") or "")
    end = te._parse(raw_end) or start + timedelta(days=7)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_end.strip()):
        end += timedelta(days=1)
    hits = sorted((e for e in evs if e.start < end and e.end > start), key=lambda e: e.start)
    if not hits:
        return f"No events between {start:%a %b %d, %I:%M %p} and {end:%a %b %d, %I:%M %p}."
    return "\n".join(event_line(e) for e in hits)


def event_line(e: Ev) -> str:
    """Exactly the apps' EventText.eventLine (0.13.3+: a timed event says when it ends)."""
    def day(d: datetime) -> str:
        return f"{d:%a %b} {d.day}"

    def stamp(d: datetime) -> str:
        return f"{day(d)}, {_fmt(d)}"
    last = e.end - timedelta(seconds=1) if e.end > e.start else e.start
    spans = last.date() != e.start.date()
    place = (f" [at: {e.place}]" if e.place else "") + (f" [repeats: {e.repeats}]" if e.repeats else "")
    if e.all_day:
        return f"• {day(e.start)}, all day" + (f" – {day(last)}" if spans else "") + f" — {e.title}{place}"
    if spans:
        return f"• {stamp(e.start)} – {stamp(e.end)} — {e.title}{place}"
    return f"• {stamp(e.start)}–{_fmt(e.end)} — {e.title}{place}"


@dataclass(frozen=True)
class Rem:
    title: str
    due: datetime | None = None
    timed: bool = True


def reminders(now: datetime) -> list[Rem]:
    """Open reminders placed around now: one overdue, one later today, one due tomorrow (no time),
    one from two days ago (no time), one with no due date."""
    n = now.replace(second=0, microsecond=0)
    n = n - timedelta(minutes=n.minute % 5)
    day = n.replace(hour=0, minute=0)
    return [Rem("Call the plumber"),
            Rem("Pay the water bill", n - timedelta(minutes=90)),
            Rem("Renew the car registration", day + timedelta(days=1), timed=False),
            Rem("Pick up dry cleaning", n + timedelta(hours=3)),
            Rem("Return library books", day - timedelta(days=2), timed=False)]


def reminder_answer(rs: list[Rem]) -> str:
    """Like the apps' list_reminders (EventText.reminderLines: soonest due first, no due last)."""
    dated = sorted((r for r in rs if r.due), key=lambda r: r.due)
    out = [f"• {r.title} (due {r.due:%a %b} {r.due.day}" + (f", {_fmt(r.due)})" if r.timed else ")") for r in dated]
    return "\n".join(out + [f"• {r.title}" for r in rs if not r.due])


def device_answer(evs: list[Ev]) -> Callable[[str, dict, dict | None], str]:
    def answer(tool: str, args: dict, overrides: dict | None = None) -> str:
        if tool == "list_calendar_events":
            return calendar_answer(evs, args)
        if tool == "list_reminders":
            return reminder_answer(reminders(datetime.now(TZ)))
        if tool == "travel_time":
            return "18 min by car to Pearl Dental (7.2 mi), with current traffic."
        return "Device action was not completed: the truth eval only reads."
    return answer


# ── checks ───────────────────────────────────────────────────────────────────────────────────

def _has(text: str, *words: str) -> bool:
    low = text.lower()
    return all(w.lower() in low for w in words)


def _time_variants(d: datetime) -> list[str]:
    """3:00 PM / 3 PM / 3pm / 15:00."""
    h12 = d.strftime("%I").lstrip("0")
    m = d.strftime("%M")
    ap = d.strftime("%p")
    v = [f"{h12}:{m} {ap}", f"{h12}:{m}{ap}", f"{h12}:{m} {ap.lower()}", f"{h12}:{m}{ap.lower()}", d.strftime("%H:%M")]
    if m == "00":
        v += [f"{h12} {ap}", f"{h12}{ap}", f"{h12} {ap.lower()}", f"{h12}{ap.lower()}"]
    return v


def _norm(text: str) -> str:
    """Lower case, any space (U+202F before AM/PM too) as a space, "p.m." as "pm"."""
    t = re.sub(r"[\u00a0\u202f\u2009]", " ", text.lower())
    return re.sub(r"\b([ap])\.m\.", r"\1m", t)


def _mentions_time(text: str, d: datetime) -> bool:
    t = _norm(text)
    return any(v.lower() in t for v in _time_variants(d))


def _in_order(text: str, words: list[str]) -> bool:
    """Each word appears after the one before it (a lead-in that names them out of order is fine)."""
    low, at = text.lower(), -1
    for w in words:
        at = low.find(w.lower(), at + 1)
        if at < 0:
            return False
    return True


def _says_no(text: str) -> bool:
    """Not free at the asked time. Whole words only (#1144: "no" inside "not scheduled for anything else" passed a
    reply that said "free at 3:30"), and never a reply that opens by saying free."""
    return bool(re.search(r"\b(no|not free|aren't free|busy|booked|conflicts?|overlap(?:s|ping)?|clash(?:es)?)\b", text, re.I)) \
        and not _says_yes_free(text)


def _says_yes_free(text: str) -> bool:
    """Free at the asked time; a reply may still mention being busy at another time."""
    t = text.lower()
    first = re.split(r"(?<=[.!?;])\s|,\s*but\b", t)[0]
    return bool(re.search(r"\b(yes|you're free|you are free|free then|free at|nothing (?:on|scheduled|booked)|open)\b", first)) \
        and not re.search(r"\b(not free|aren't free|no,)", first)


@dataclass
class Case:
    name: str
    category: str
    question: str
    check: Callable[[str], list[str]]      # reply → problems
    expected: str                          # what a right answer says, for the report
    extra: dict = field(default_factory=dict)   # this case's own eval pins (the world group: eval_world)


def _day_word_of(d: datetime) -> str:
    today = datetime.now(TZ).date()
    return {0: "today", 1: "tomorrow"}.get((d.date() - today).days, d.strftime("%A"))


def _next_run(schedule: dict, now: datetime) -> datetime | None:
    """A routine's next run: {time: "HH:MM", days: [0-6], 0 = Monday} in the household's time."""
    try:
        h, m = (int(x) for x in str(schedule.get("time") or "").split(":"))
    except ValueError:
        return None
    days = set(schedule.get("days") or range(7))
    for k in range(8):
        d = (now + timedelta(days=k)).replace(hour=h, minute=m, second=0, microsecond=0)
        if d.weekday() in days and d > now:
            return d
    return None


def cases(now: datetime, timeline: list[dict], kb: list[dict], routines: list[dict] | None = None,
          people: list[dict] | None = None) -> list[Case]:
    routines = routines or []
    people = people or []
    evs = {e.title: e for e in week(now)}
    tom = (now + timedelta(days=1))
    after = (now + timedelta(days=2))
    out: list[Case] = []

    def add(name: str, cat: str, q: str, expected: str, check: Callable[[str], list[str]]) -> None:
        out.append(Case(name, cat, q, check, expected))

    # Time
    add("today_date", "time", "What's today's date?", f"{now:%A}, {now:%B} {now.day}",
        lambda r: [] if _has(r, now.strftime("%A")) and re.search(rf"\b{now:%B}\s+0?{now.day}\b|\b{now.month}/{now.day}\b", r, re.I)
        else ["wrong or missing date"])
    add("tomorrow_day", "time", "What day is tomorrow?", f"{tom:%A}, {tom:%B} {tom.day}",
        lambda r: [] if _has(r, tom.strftime("%A")) else ["wrong weekday for tomorrow"])
    add("time_now", "time", "What time is it?", _fmt(now),
        lambda r: [] if any(_mentions_time(r, now + timedelta(minutes=k)) for k in range(-3, 4)) else ["wrong or missing time"])

    # Calendar and schedule
    call, focus = evs["Call with Sam"], evs["Focus block"]
    # Near midnight the call is after midnight and the all-day field trip starts first: either is right.
    trip_first = call.start.date() != now.date()
    add("next_event", "calendar", "What's next on my calendar?", f"Call with Sam at {_fmt(call.start)}",
        lambda r: [] if (_has(r, "Sam") and _mentions_time(r, call.start)) or (trip_first and _has(r, "field trip"))
        else ["didn't give the next event and its time"])
    add("now_event", "calendar", "What's on my calendar right now?", f"Focus block until {_fmt(focus.end)}",
        lambda r: [] if _has(r, "focus") else ["missed the event under way"])
    add("until_next", "schedule", "How long until my next meeting?", "about 50 minutes (Call with Sam)",
        lambda r: [] if re.search(r"\b(4[5-9]|5[0-5])\s*(?:min|minutes)\b|\bunder an hour\b|\babout an hour\b", r, re.I)
        else ["wrong time until the next meeting"])
    want_tom = ["Dentist", "Piano", "soccer", "field trip"]
    add("tomorrow_list", "calendar", "What's on my calendar tomorrow?", "Dentist 9 AM, Piano 3 PM, Leo soccer 3:30 PM, Ava's field trip (all day)",
        lambda r: ([f"missed {w}" for w in want_tom if not _has(r, w)]
                   + (["listed the day after's Team dinner"] if _has(r, "team dinner") else [])
                   + (["listed yesterday's Ballet"] if _has(r, "ballet") else [])))
    add("busy_tomorrow_330", "schedule", "Am I free tomorrow at 3:30 PM?", "No: Piano lesson and Leo soccer overlap then",
        lambda r: [] if _says_no(r) and (_has(r, "piano") or _has(r, "soccer")) else ["didn't say busy (piano/soccer)"])
    add("free_tomorrow_11", "schedule", "Am I free tomorrow at 11 AM?", "Yes (only Ava's all-day field trip)",
        lambda r: [] if _says_yes_free(r) else ["didn't say free at 11"])
    add("conflicts_tomorrow", "schedule", "Do I have any conflicts tomorrow?", "Piano lesson 3–4 PM overlaps Leo soccer 3:30–4:30 PM",
        lambda r: [] if _has(r, "piano") and _has(r, "soccer") else ["missed the piano/soccer overlap"])
    dent = evs["Dentist"]
    add("when_dentist", "calendar", "When is my dentist appointment?", f"Tomorrow ({dent.start:%A}) at 9 AM",
        lambda r: [] if _mentions_time(r, dent.start) and (_has(r, "tomorrow") or _has(r, dent.start.strftime("%A"))) else ["wrong day or time"])
    add("yesterday_evening", "calendar", "What was on my calendar yesterday evening?", "Ballet at 5:15 PM",
        lambda r: [] if _has(r, "ballet") else ["missed yesterday's Ballet"])
    trip = evs["Spokane trip"]
    add("trip_when", "calendar", "When is the Spokane trip and how many days is it?",
        f"{trip.start:%A, %B} {trip.start.day} for 3 days",
        lambda r: [] if _has(r, trip.start.strftime("%A")) and re.search(r"\b(3|three)\b", r, re.I) else ["wrong start day or length"])
    dinner = evs["Team dinner"]
    add("day_after", "calendar", f"What's on {after:%A}?", f"Team dinner at 7 PM",
        lambda r: [] if _has(r, "team dinner") and not _has(r, "dentist") else ["wrong day's events"])

    empty = now + timedelta(days=3)   # between Team dinner and the Spokane trip: nothing
    add("empty_day", "schedule", f"Do I have anything on {empty:%A}?", "No, nothing that day",
        lambda r: [] if re.search(r"\b(nothing|no events|don't have anything|do not have anything|free|clear|not have any|no plans)\b", r, re.I)
        and not re.search(rf"\b(?:you have|there's|there is)\b[^.]*\b(?:spokane|team dinner)\b[^.]*\b{empty:%A}\b", r, re.I)
        else ["didn't say the day is clear"])
    piano = evs["Piano lesson"]
    add("piano_time", "calendar", "What time is piano tomorrow?", "3 PM",
        lambda r: [] if _mentions_time(r, piano.start) else ["wrong piano time"])
    # Reminders (the device's list, as the apps send it)
    rs = {x.title: x for x in reminders(now)}
    add("overdue", "reminders", "Is anything overdue?", "Pay the water bill (and the library books)",
        lambda r: [] if _has(r, "water bill") and not re.match(r"\s*(?:no|nothing)\b", r, re.I) else ["missed the overdue water bill"])
    later = rs["Pick up dry cleaning"].due
    # Late in the evening the dry cleaning (now + 3 h) is due after midnight, after tomorrow's day-only one.
    either = later.date() != now.date()
    add("next_reminder", "reminders", "Which reminder comes up next (not counting overdue ones)?", f"Pick up dry cleaning at {_fmt(later)}",
        lambda r: [] if _has(r, "dry cleaning") or (either and _has(r, "registration")) else ["wrong next reminder"])
    add("reminders_tomorrow", "reminders", "Which reminders are due tomorrow?", "Renew the car registration",
        lambda r: [] if _has(r, "registration") and not _has(r, "water bill") else ["wrong reminders for tomorrow"])
    add("no_due_date", "reminders", "Which of my reminders has no due date?", "Call the plumber",
        lambda r: [] if _has(r, "plumber") else ["missed the undated reminder"])

    # Order of operations
    order = ["Dentist", "Piano", "soccer"]
    add("first_tomorrow", "order", "What's my first appointment tomorrow?", "Dentist at 9 AM (after the all-day field trip)",
        lambda r: [] if _has(r, "dentist") else ["wrong first appointment"])
    add("after_dentist", "order", "What comes right after the dentist tomorrow?", "Piano lesson at 3 PM",
        lambda r: [] if _has(r, "piano") else ["wrong next event after the dentist"])
    add("tomorrow_in_order", "order", "List tomorrow's appointments in order.", "Dentist 9 AM, Piano 3 PM, Leo soccer 3:30 PM",
        lambda r: [] if _in_order(r, order) else ["out of order or missing"])
    add("leave_for_dentist", "order", "When should I leave for the dentist tomorrow?", "about 8:40 AM (18 min by car, 9 AM start)",
        lambda r: [] if re.search(r"(?<!\d)0?8[:.](?:2\d|3\d|4[0-2])\b", _norm(r)) else ["leave time not 8:20–8:42"])

    # A repeating event (the apps' "[repeats: …]" note)
    # A chore the household doesn't have: its real garbage night is in the world model, and both brains
    # rightly answered with it over the fixture (10-02 nightly).
    g = compost(now)[1]
    add("compost_next", "calendar", "When does the compost go out next?", f"{g.start:%A}, {g.start:%B} {g.start.day} at 8 PM",
        lambda r: [] if _has(r, g.start.strftime("%A")) and _mentions_time(r, g.start) else ["wrong next compost day or time"])
    add("compost_how_often", "calendar", "How often does the compost go out?", f"every week, on {g.start:%A}s",
        lambda r: [] if re.search(r"\bevery week\b|\bweekly\b|\bonce a week\b", r, re.I) and _has(r, g.start.strftime("%A"))
        else ["didn't say every week on that day"])

    # Day arithmetic
    trip_days = (trip.start.date() - now.date()).days
    add("days_until_trip", "time", "How many days until the Spokane trip?", f"{trip_days} days",
        lambda r: [] if re.search(rf"\b(?:{trip_days}|{['zero','one','two','three','four','five','six','seven'][trip_days] if trip_days < 8 else '#'})\s+days?\b", r, re.I)
        else ["wrong number of days"])
    ahead = now + timedelta(days=12)
    add("weekday_of_date", "time", f"What day of the week is {ahead:%B} {ahead.day}?", ahead.strftime("%A"),
        lambda r: [] if _has(r, ahead.strftime("%A")) else ["wrong weekday"])
    add("not_stored", "knowledge", "What's the code for the shed lock?", "titan doesn't know (nothing stored)",
        lambda r: (["made up a code"] if re.search(r"\b(?!(?:19|20)\d\d\b)\d{3,8}\b", r) else [])
        + ([] if re.search(r"don't|do not|can't|cannot|couldn't|could not|isn't|is not|no (?:record|note|code|saved|stored)|"
                           r"not (?:sure|find|stored|saved|in)|haven't|there's no|nothing (?:saved|stored)", r, re.I)
           else ["didn't say it doesn't know"]))

    # Timeline (titan's own read API for this person)
    if timeline:
        last = timeline[0]
        names = {str(e.get("person") or "").lower() for e in timeline} | {"zach", "robin", "ava", "leo", "jordan"}
        core = re.sub(r"\([^)]*\)", " ", last["text"])   # "(from Zach's iPhone)" is detail, not the entry
        stop = {"from", "with", "into", "onto", "that", "this", "your", "their"}
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z']{3,}", core) if w.lower() not in names | stop][:4]
        # The entry's own words, mostly (a reply says "you" for the asker; the newest may change mid-run).
        add("timeline_latest", "timeline", "What's the most recent thing on the household timeline?", last["text"],
            lambda r, words=words: [] if words and sum(_has(r, w) for w in words) >= max(1, len(words) - 1)
            else ["didn't give the latest entry"])
        older = next((e for e in timeline[1:6] if len(e["text"]) > 12), None)
        if older:
            day = _when(older["at"])
            key = [w for w in re.findall(r"[A-Za-z][A-Za-z']{4,}", older["text"])][:2]
            if day and key:
                add("timeline_when", "timeline", f"When did this happen: \u201c{older['text']}\u201d?", f"{day:%A %B} {day.day}",
                    lambda r, day=day: [] if (_has(r, day.strftime("%A")) or _has(r, f"{day:%B} {day.day}")
                                              or (day.date() == now.astimezone(TZ).date() and _has(r, "today"))
                                              or (day.date() == (now.astimezone(TZ) - timedelta(days=1)).date() and _has(r, "yesterday")))
                    else ["wrong day"])

    # Timeline order: which of two real entries came first (titan's times, worked out here)
    plain = sorted((e for e in timeline if e.get("kind") != "note" and len(e.get("text", "")) > 12 and _when(e.get("at"))),
                   key=lambda e: _when(e["at"]), reverse=True)
    if len(plain) >= 3 and (_when(plain[0]["at"]) - _when(plain[2]["at"])).total_seconds() >= 600:   # no near-ties
        newer, older = plain[0], plain[2]
        def key(e: dict) -> str:
            ws = [w for w in re.findall(r"[A-Za-z][A-Za-z']{4,}", e["text"]) if w.lower() not in ("zach", "robin", "ava", "leo", "jordan")]
            return ws[0] if ws else e["text"][:12]
        a, b = key(older), key(newer)
        if a.lower() != b.lower():
            add("timeline_order", "timeline",
                f"Which happened first: \u201c{newer['text']}\u201d or \u201c{older['text']}\u201d? Name just that one.",
                older["text"],
                lambda r, a=a, b=b: [] if _has(r, a) and not re.search(rf"\b{re.escape(b.lower())}\b[^.]*\b(?:first|earlier)\b", r.lower())
                else ["named the later entry"])

    # Routines (titan's own, as this person sees them)
    for rt in [x for x in routines if len(x.get("steps") or []) >= 2][:2]:
        steps = rt["steps"]
        word = [w for w in re.findall(r"[A-Za-z]{4,}", steps[1])][:2]
        if word:
            add(f"routine_step_{rt['id'][-6:]}", "routines", f"What's the second step of my \u201c{rt['name']}\u201d routine?", steps[1],
                lambda r, word=word: [] if all(_has(r, w) for w in word) else ["wrong step"])
        sch = rt.get("schedule") or {}
        nxt = _next_run(sch, now) if rt.get("enabled", True) else None
        if nxt:
            add(f"routine_next_{rt['id'][-6:]}", "routines", f"When does my \u201c{rt['name']}\u201d routine run next?",
                f"{nxt:%A} at {_fmt(nxt)}",
                lambda r, nxt=nxt: [] if _mentions_time(r, nxt) and (_has(r, nxt.strftime("%A")) or _has(r, _day_word_of(nxt))) else ["wrong next run"])

    # Travel (#423): when to leave, with traffic. The fake device's travel_time says 18 min by car; titan
    # may add a few minutes' margin, so leaving 18–40 min before 9 AM is right, after 8:42 is late.
    add("leave_dentist", "travel", "When do I need to leave for my dentist appointment tomorrow?",
        "about 8:30–8:42 AM (18 min by car, before 9 AM)",
        lambda r: [] if any(_mentions_time(r, dent.start - timedelta(minutes=m)) for m in range(18, 41))
        else ["didn't give a leave time 18–40 min before 9 AM"])

    # The household roster (#423): who lives here and how old they are, from titan's people store.
    named = [p for p in people if (p.get("name") or "").strip() and p.get("role") != "household"]
    if len(named) >= 2:
        names = [p["name"].split()[0] for p in named]
        add("household_members", "people", "Who lives in our household?", ", ".join(names),
            lambda r, names=names: [f"missed {n}" for n in names if not _has(r, n)])
    for p in [p for p in named if isinstance(p.get("age"), int)][:3]:
        first, age = p["name"].split()[0], p["age"]
        # From a birth year: before this year's birthday they're a year younger. Either is right.
        add(f"age_{first.lower()}", "people", f"How old is {first}?", f"{age - 1} or {age}",
            lambda r, age=age: [] if re.search(rf"\b({age - 1}|{age})\b", r) else [f"didn't say {age - 1} or {age}"])

    # Knowledge (titan's own read API for this person)
    # The household's own notes (not titan's runbook), owned by or shared with this person.
    homey = [k for k in kb if (k.get("category") or "") in ("household", "family", "home", "personal", "note", "general")
             and len(k.get("body") or "") > 40 and not str(k.get("title") or "").lower().startswith(("titan", "agent"))]
    for note in homey[:3]:
        title = note.get("title") or ""
        body = note.get("body") or ""
        # A note saved from chat stores "The household said: …\nAssistant: Saved — …": only what the
        # person said is the note (10-01 round 5 expected the word "Saved" from titan's own reply).
        said = re.search(r"The household said:\s*(.*?)(?:\nAssistant:|$)", body, re.S)
        if said:
            body = said.group(1)
        # Names inside a sentence (not a sentence's first word) and numbers, written as stored.
        facts = re.findall(r"(?<=[a-z,;:] )[A-Z][a-z]{3,}\b|\b\d+(?:[:/.]\d+)*\b", body)
        facts = [f for f in dict.fromkeys(facts) if f.lower() not in title.lower()][:2]
        if not facts:
            continue
        add(f"kb_{note['id'][:24]}", "knowledge", f"What does my note \u201c{title}\u201d say?", ", ".join(facts),
            lambda r, facts=facts: [f"missed {f!r}" for f in facts if f.lower() not in r.lower()])

    # When a note was saved (#423, "when did I tell you about …"): the date titan stored when it was saved, as
    # GET v1/kb says. Right: the month and day (or "today"/"yesterday" when it's that recent).
    dated = [k for k in homey if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k.get("saved") or ""))]
    for note in dated[:2]:
        d = date.fromisoformat(note["saved"])
        ago = (now.date() - d).days
        rel = "today" if ago == 0 else "yesterday" if ago == 1 else None

        def saved_check(r: str, d=d, rel=rel) -> list[str]:
            low = r.lower()
            if rel and rel in low:
                return []
            month_day = re.search(rf"\b{d:%B}\s+{d.day}\b|\b{d:%b}\.?\s+{d.day}\b|\b{d.month}/{d.day}\b", r, re.I)
            return [] if month_day else [f"didn't say it was saved {d:%b} {d.day}"]
        add(f"kb_saved_{note['id'][:20]}", "knowledge", f"When did I save my note \u201c{note.get('title') or ''}\u201d?",
            f"{d:%A, %B} {d.day}" + (f" ({rel})" if rel else ""), saved_check)

    # The world model (#325): household lookups answered from claims, asked with it on for the turn.
    # Built from what world.db holds on this host (world_eval.lookups); none when it's empty.
    try:
        import world
        import world_eval
        for c in world_eval.lookups(world.get(), limit=4, unknown=1):
            out.append(Case(f"world_{c.name}"[:48], "world", c.question, c.check, c.expected, {"eval_world": True}))
    except Exception as exc:  # noqa: BLE001 - no world.db here: the group is just absent
        print(f"(world cases skipped: {exc})", file=sys.stderr)
    return out


def _when(raw: Any) -> datetime | None:
    """titan's event time (fractions of any length, a zone or none = UTC) in the household's zone."""
    from datetime import timezone
    s = str(raw or "").strip().replace("Z", "+00:00")
    m = re.match(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if not m:
        return None
    try:
        d = datetime.fromisoformat(m.group(1).replace(" ", "T") + (m.group(3) or ""))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(TZ)


# ── live reads (titan's own APIs, as the same person) ────────────────────────────────────────

def _get(path: str) -> Any:
    req = urllib.request.Request(f"{te.API}/{path}", headers={"X-Agent-Token": te._token()})
    import eval_names as _names   # live reads come back with fictional names, like the replies (#878)
    with urllib.request.urlopen(req, timeout=30) as r:
        return _names.back(json.loads(r.read()))


def _get_retrying(path: str) -> Any:
    try:
        return _get(path)
    except Exception as exc:  # noqa: BLE001
        if _RESTART.search(str(exc)) and _wait_for_titan():
            return _get(path)
        raise


def live_timeline() -> list[dict]:
    try:
        return [e for e in _get_retrying("v1/timeline?which=household&limit=20").get("events", []) if e.get("text")]
    except Exception as exc:  # noqa: BLE001
        print(f"(timeline unavailable: {exc})")
        return []


def live_routines() -> list[dict]:
    try:
        return _get_retrying("v1/routines").get("routines", [])
    except Exception as exc:  # noqa: BLE001
        print(f"(routines unavailable: {exc})")
        return []


def live_people() -> list[dict]:
    """The household's people with their ages (GET v1/people, the admin's view: the eval's token)."""
    try:
        return _get_retrying("v1/people").get("people", [])
    except Exception as exc:  # noqa: BLE001
        print(f"(people unavailable: {exc})")
        return []


def live_kb() -> list[dict]:
    try:
        return _get_retrying("v1/kb").get("entries", [])
    except Exception as exc:  # noqa: BLE001
        print(f"(knowledge base unavailable: {exc})")
        return []


# ── run ──────────────────────────────────────────────────────────────────────────────────────

_RESTART = re.compile(r"Connection refused|RemoteDisconnected|Connection reset|IncompleteRead", re.I)   # a slow turn is a real miss


def _wait_for_titan(limit: float = 180) -> bool:
    """titan restarts while it's deployed; wait for /health before asking again."""
    import time
    end = time.time() + limit
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"{te.API}/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(5)
    return False


EFFORT: str | None = None   # #876's A/B: --effort high | normal


def ask(case: Case, brain: str, evs: list[Ev], client: str | None) -> te.Turn:
    """One eval turn. A turn cut off by titan restarting (a deploy mid-run) is asked again once titan
    is back, up to twice: a restart isn't a wrong answer."""
    te.fake_mac = device_answer(evs)   # turn_eval's device loop answers from this week
    for attempt in range(3):
        t = te.ask(case.question, client=client, extra={"eval_brain": brain, "eval_tools": READ_TOOLS, **case.extra,
                                                        **({"eval_local_where": LOCAL_WHERE} if LOCAL_WHERE else {}),
                                                        **({"effort": EFFORT} if EFFORT else {})})
        if not (t.error and _RESTART.search(t.error)) or attempt == 2 or not _wait_for_titan():
            return t
    return t


def in_set(c: "Case", which: str) -> bool:
    """#1144: the case subsets an A/B can be judged on (the code path the change reaches)."""
    if which == "wants_schedule":
        import schedule_digest
        return schedule_digest.wants_schedule(c.question)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", choices=["local", "gemini", "both"], default="both")
    ap.add_argument("--client", choices=["iphone", "mac"], default="iphone")
    ap.add_argument("--case")
    ap.add_argument("--set", choices=["all", "wants_schedule"], default="all",
                    help="#1144: only the cases a change can reach (wants_schedule: the questions that get the schedule digest)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--jsonl", help="also write one line per turn: {brain, name, category, ok, problems, question, "
                                    "expected, reply, tools, answered, at}")
    ap.add_argument("--summary-jsonl", help="also append one line per brain: {ts, source, brain, client, score, "
                                            "total, by_category, misses} (the nightly's, for the scorecard: #385)")
    ap.add_argument("--source", default="manual", help="manual | nightly (the scorecard reads nightly lines)")
    ap.add_argument("--local-where", choices=["short"], help="#627's arm: the short identity on local turns")
    ap.add_argument("--effort", choices=["high", "normal"], help="#876's arms: every turn asks this effort")
    a = ap.parse_args()
    global LOCAL_WHERE, EFFORT
    LOCAL_WHERE = a.local_where
    EFFORT = a.effort
    out_jsonl = open(a.jsonl, "a", encoding="utf-8") if a.jsonl else None
    timeline, kb, routines, people = live_timeline(), live_kb(), live_routines(), live_people()
    names = [c.name for c in cases(datetime.now(TZ), timeline, kb, routines, people)
             if (not a.case or c.name == a.case) and in_set(c, a.set)]
    brains = ["local", "gemini"] if a.brain == "both" else [a.brain]
    code = 0
    for brain in brains:
        by_cat: dict[str, list[int]] = {}
        misses = []
        for name in names:
            # Each turn gets its own now: the week and every time-relative answer are placed around it.
            now = datetime.now(TZ)
            if name.startswith("timeline"):
                timeline = live_timeline() or timeline   # the household keeps adding entries mid-run
            c = next((x for x in cases(now, timeline, kb, routines, people) if x.name == name), None)
            if c is None:   # e.g. the event under way ended between building the list and this turn
                continue
            if a.client == "mac" and c.name == "leave_for_dentist":
                continue   # travel_time is the iPhone's tool
            t = ask(c, brain, week(now), None if a.client == "mac" else a.client)
            problems = [f"error: {t.error}"] if t.error else c.check(t.text)
            really = (t.answered or {}).get("brain")
            if really and not str(really).startswith(brain):
                problems = [f"answered by {really}, not {brain} (fallback {t.answered.get('fallback')!r})"]
            if out_jsonl:
                out_jsonl.write(json.dumps({"brain": brain, "name": c.name, "category": c.category, "ok": not problems,
                                            "problems": problems, "question": c.question, "expected": c.expected,
                                            "reply": t.text, "tools": [x for x, _ in t.device], "answered": t.answered,
                                            "ms": t.ms, "at": now.isoformat()}) + "\n")
                out_jsonl.flush()
            by_cat.setdefault(c.category, [0, 0])[1] += 1
            if problems:
                misses.append((c, problems, t))
            else:
                by_cat[c.category][0] += 1
            if a.verbose:
                print(f"── [{brain}] {c.name} ({t.ms / 1000:.1f}s) {'ok' if not problems else 'MISS'} "
                      f"tools={[x for x, _ in t.device]}: {t.text[:300]!r}")
        for c, problems, t in misses:
            print(f"  [{brain}] {c.name}: {'; '.join(problems)}\n     asked: {c.question}\n     expected: {c.expected}\n"
                  f"     device tools: {[(x, a) for x, a in t.device]}\n     reply: {t.text[:400]!r}")
        cats = ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by_cat.items()))
        ok = sum(v[0] for v in by_cat.values())
        total = sum(v[1] for v in by_cat.values())
        print(f"{brain}: {cats}\ntruth [{brain}]: {ok}/{total}\n")
        if a.summary_jsonl:
            import time as _time
            try:
                with open(a.summary_jsonl, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": _time.time(), "source": a.source, "brain": brain, "client": a.client,
                                        "score": ok, "total": total, "by_category": by_cat,
                                        "misses": [c.name for c, _, _ in misses]}) + "\n")
            except OSError as exc:
                print(f"(couldn't append to {a.summary_jsonl}: {exc})", file=sys.stderr)
        code |= ok != total
    return code


if __name__ == "__main__":
    sys.exit(main())
