#!/usr/bin/env python3
"""Reply checks against what the turn's tools returned (titan-node#344). Pure functions; the agent runs
them after every reply (agent_runtime_lib._with_checked_answers) and appends one "Correction:" line when a
reply contradicts the calendar or reminders it was given. It never rewrites the reply: it states the fact.

From the truth eval (round 5, 10-01; local 17/32, Gemini 30/32):
  denial      "I don't have any calendar events for piano tomorrow" with piano in the calendar output, or
              "I don't have information about your dentist appointment" when nothing was read at all
  all day     "You have a field trip with Ava tomorrow at 11 AM" for an all-day event
  overdue     "Nothing is overdue" with an overdue reminder in the list
  latest      "The most recent entry is the backup finishing" when the timeline it read, newest first, has a
              newer one
  range       "You're free on Saturday" or "Tomorrow you have soccer at 4" when the calendar reads this turn
              covered other days only
  source      "…according to the shared household calendar" when the turn read no calendar (truth r7: an old
              note said Wednesday; the calendar says Tuesday); and email, messages, notes or a document cited with
              no tool that read them this turn (#953: "the emails confirm…" after only find_records)
  omitted     a day's events listed with one left out (truth r7: tomorrow's list without the all-day field trip)
  first       "Which happened first: A or B?" answered with the later one (10-02 nightly: the newest-first listing
              read top-down as oldest-first)
  when        an event put on the wrong date, or "N days until" it wrong (10-02 nightly: the Spokane trip, Mon Oct 5
              to Wed Oct 7, as "October 2nd, 2 days" and "1 day until")
Formats parsed: the apps' EventText (truth_eval.event_line mirrors it) and reminder lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

CALENDAR_TOOLS = frozenset({"list_calendar_events"})
TIMELINE_TOOLS = frozenset({"recall_events"})
REMINDER_TOOLS = frozenset({"list_reminders"})
READ_TOOLS = CALENDAR_TOOLS | REMINDER_TOOLS | frozenset({"search_notes", "kb_retrieve", "recall_events", "list_todos",
                                                          "find_records", "search_messages", "open_obligations", "threads"})
_MON = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DAY = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
_DATE = _DAY + r"\s+(?P<{p}mon>[A-Z][a-z]{{2}})\s+(?P<{p}d>\d{{1,2}})"
_CLOCK = r"(?P<{p}h>\d{{1,2}})(?::(?P<{p}m>\d{{2}}))?\s*(?P<{p}ap>[AaPp])\.?[Mm]\.?"
_EVENT_RX = re.compile(
    r"^•\s+" + _DATE.format(p="a") + r",\s+(?:(?P<allday>all day)|" + _CLOCK.format(p="s") + r")"
    r"(?:\s*[–-]\s*(?:" + _DATE.format(p="b") + r"(?:,\s+" + _CLOCK.format(p="be") + r")?|" + _CLOCK.format(p="e") + r"))?"
    r"\s+—\s+(?P<title>.+?)(?:\s+\[.*)?$", re.M)
_REM_RX = re.compile(r"^•\s+(?P<title>.+?)(?:\s+\(due\s+" + _DATE.format(p="") + r"(?:,\s+" + _CLOCK.format(p="t")
                     + r")?\))?(?:\s+\[[^\]\n]*\])*\s*$", re.M)   # markers like "[overdue by 1 h]" "[tomorrow]" after it
_TIME_IN_TEXT = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b", re.I)
_GENERIC = {"appointment", "appointments", "event", "events", "meeting", "calendar", "schedule", "information",
            "anything", "details", "plans", "today", "tomorrow", "tonight", "yesterday", "week", "weekend",
            "this", "that", "your", "the", "for", "about", "any", "with", "time", "day", "days", "date", "next",
            "reminder", "reminders", "task", "tasks", "note", "notes", "related", "lesson", "lessons", "check",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "morning", "evening",
            "afternoon", "night", "upcoming", "scheduled"}


@dataclass
class Event:
    title: str
    start: date
    end: date
    all_day: bool
    t0: time | None
    t1: time | None
    line: str


@dataclass
class Reminder:
    title: str
    due: datetime | None
    timed: bool
    line: str


def _year_for(mon: int, d: int, today: date) -> date:
    cands = []
    for y in (today.year - 1, today.year, today.year + 1):
        try:
            cands.append(date(y, mon, d))
        except ValueError:
            pass
    return min(cands, key=lambda c: abs((c - today).days))


def _clock(h: str | None, m: str | None, ap: str | None) -> time | None:
    if not h:
        return None
    hh = int(h) % 12 + (12 if ap and ap.lower() == "p" else 0)
    return time(hh, int(m or 0))


def parse_events(text: str, today: date) -> list[Event]:
    out = []
    for m in _EVENT_RX.finditer(text or ""):
        g = m.groupdict()
        try:
            start = _year_for(_MON[g["amon"].lower()], int(g["ad"]), today)
            end = _year_for(_MON[g["bmon"].lower()], int(g["bd"]), today) if g.get("bmon") else start
        except (KeyError, ValueError):
            continue
        t0 = _clock(g.get("sh"), g.get("sm"), g.get("sap"))
        t1 = _clock(g.get("eh"), g.get("em"), g.get("eap")) or _clock(g.get("beh"), g.get("bem"), g.get("beap"))
        out.append(Event(g["title"].strip(), start, end, bool(g.get("allday")), t0, t1, m.group(0).strip()))
    return out


def _title_key(title: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", str(title or "").lower()).split())


def dedupe_event_lines(text: str, today: date) -> tuple[str, int]:
    """One event seen through two calendars (iCloud and Google "Birthdays", a calendar shared twice) is one event
    (titan-node#1038): a line with the same title (case and punctuation aside), the same days and the same all-day or
    times as an earlier line is dropped. → (the text, how many lines were dropped). Other lines are untouched."""
    seen: set[tuple[Any, ...]] = set()
    out, dropped = [], 0
    for line in str(text or "").split("\n"):
        evs = parse_events(line, today) if line.lstrip().startswith("•") else []
        if len(evs) == 1:
            e = evs[0]
            key = (_title_key(e.title), e.start, e.end, e.all_day, e.t0, e.t1)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out), dropped


def parse_reminders(text: str, today: date) -> list[Reminder]:
    out = []
    for m in _REM_RX.finditer(text or ""):
        g = m.groupdict()
        due, timed = None, False
        if g.get("mon"):
            try:
                dd = _year_for(_MON[g["mon"].lower()], int(g["d"]), today)
            except (KeyError, ValueError):
                continue
            t = _clock(g.get("th"), g.get("tm"), g.get("tap"))
            timed = t is not None
            due = datetime.combine(dd, t or time(23, 59))
        out.append(Reminder(g["title"].strip(), due, timed, m.group(0).strip()))
    return out


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z'’]{2,}", (s or "").lower()) if w not in _GENERIC}


def _sentences(text: str) -> list[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+|\n+", text or "") if x.strip()]


_DENIAL_RX = re.compile(
    r"\b(?:I\s+)?(?:don'?t|do not|can'?t|cannot|couldn'?t|could not)\s+(?:have|find|see)\b[^.?!\n]{0,40}?"
    r"\b(?:information|events?|anything|record|details|mention|appointments?)\b(?P<rest>[^.?!\n]{0,60})"
    r"|\bno (?:calendar )?(?:events?|appointments?|information)\b(?P<rest2>[^.?!\n]{0,60})", re.I)


_HOUSEHOLD_FACT_RX = re.compile(
    r"\b(?:calendar|appointments?|schedule|events?|meetings?|trip|lessons?|practice|reminders?|due|overdue|notes?|"
    r"bills?|today|tomorrow|tonight|this week|weekend|(?:mon|tues|wednes|thurs|fri|satur|sun)day)\b", re.I)


def check_denial(reply: str, question: str, events: list[Event], reminders: list[Reminder], read_tools: set[str]) -> list[str]:
    """A reply that says it has nothing about X: if the calendar or reminders it was given hold X, say so; if it
    read nothing at all, say it didn't check."""
    notes = []
    for s in _sentences(reply):
        m = _DENIAL_RX.search(s)
        if not m:
            continue
        about = _words((m.group("rest") or m.group("rest2") or "")) or _words(question)
        if not about:
            continue
        hits = [e.line for e in events if about & _words(e.title)] + [r.line for r in reminders if about & _words(r.title)]
        if hits:
            shown = "; ".join(h.lstrip("• ") for h in hits[:2])
            notes.append(f"it is there: {shown}")
            break
        if not read_tools and _HOUSEHOLD_FACT_RX.search(f"{question} {s}"):   # never on chit-chat
            notes.append("I didn't check your calendar or reminders for this; ask again and I'll look")
            break
    return notes


def check_all_day(reply: str, events: list[Event]) -> list[str]:
    """An all-day event stated as happening at a clock time, or as making a time busy, when no timed event
    that day covers the time."""
    notes = []
    for s in _sentences(reply):
        tm = _TIME_IN_TEXT.search(s)
        if not tm or re.search(r"\ball[- ]day\b", s, re.I):
            continue   # no time, or the sentence already says it's all day
        if re.search(r"\bfree\b", s, re.I) and not re.search(r"\b(?:not|n't|aren't|isn't)\s+(?:\w+\s+)?free\b", s, re.I):
            continue   # "you're free at 11": the right answer
        t = _clock(tm.group(1), tm.group(2), tm.group(3))
        for e in events:
            tw = _words(e.title)
            if not e.all_day or not tw or len(tw & _words(s)) < min(2, len(tw)):
                continue   # not this all-day event the sentence is about
            busy = [x for x in events if not x.all_day and x.t0 and x.start <= e.start <= x.end
                    and x.t0 <= t < (x.t1 or time(23, 59))]
            if busy:
                continue
            notes.append(f"“{e.title}” is all day, not at {tm.group(0).strip()}; nothing else is scheduled then")
            return notes
    return notes


_NOTHING_OVERDUE_RX = re.compile(r"\b(?:nothing|no(?:thing| reminders?| tasks?| items?| bills?)?)\b[^.?!\n]{0,20}\boverdue\b|"
                                 r"\bnot (?:anything )?overdue\b|\bno overdue\b", re.I)


def check_overdue(reply: str, reminders: list[Reminder], now: datetime, read: bool = True) -> list[str]:
    if not _NOTHING_OVERDUE_RX.search(reply or ""):
        return []
    if not read:
        return ["I didn't check your reminders for this; ask again and I'll look"]
    late = [r for r in reminders if r.due and (r.due < now if r.timed else r.due.date() < now.date())]
    if not late:
        return []
    return ["overdue: " + "; ".join(r.line.lstrip("• ") for r in late[:3])]


_NEXT_RX = re.compile(r"\bnext (?:reminder|thing|task|to-?do)\b|\bcomes up next\b", re.I)


def check_next_reminder(reply: str, reminders: list[Reminder], now: datetime) -> list[str]:
    """"The next reminder is X" when an earlier one, not yet due, comes first."""
    if not _NEXT_RX.search(reply or ""):
        return []
    upcoming = sorted((r for r in reminders if r.due and (r.due >= now if r.timed else r.due.date() >= now.date())),
                      key=lambda r: (r.due.date(), r.due.time() if r.timed else datetime.max.time()))
    if not upcoming:
        return []
    first = upcoming[0]
    named = [r for r in reminders if _words(r.title) and _words(r.title) <= _words(reply)]
    if named and first not in named:
        return [f"the next one is {first.line.lstrip('• ')}"]
    return []


# recall_events' listing (agent_runtime_lib._timeline_listing): "…, newest first (now …):" then
# "- Wed Sep 30 17:12 (2 h ago) text" (someone else's text inside ⟦untrusted⟧ markers, #473).
_TL_ENTRY_RX = re.compile(r"^- (?P<when>[A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2} \d{1,2}:\d{2}) \((?P<ago>[^)]+)\) (?P<text>.+)$", re.M)
_TL_MARK_RX = re.compile(r"⟦/?untrusted\b[^⟦⟧]*⟧")
_LATEST_RX = re.compile(r"\b(?:most recent(?:ly)?|latest|newest|last (?:thing|entry|event|activity|update|post|one|"
                        r"logged|recorded|noted))\b", re.I)


def _named(text: str, reply_words: set[str]) -> bool:
    """Whether a reply names an entry: all its distinctive words, or at least two of them."""
    w = _words(text)
    return bool(w) and (w <= reply_words or len(w & reply_words) >= 2)


_FIRST_Q_RX = re.compile(r"\bwhich (?:one )?(?:happened|came|was) (?:first|earlier|before)\b|\bwhich (?:was|is) (?:older|earlier)\b|"
                         r"\bwhat happened first\b", re.I)


def _entries(listing: str) -> list[tuple[str, str, str]]:
    return [(m.group("when"), m.group("ago"), " ".join(_TL_MARK_RX.sub("", m.group("text")).split()))
            for m in _TL_ENTRY_RX.finditer(listing or "") if "newest first" in listing]


def check_first(reply: str, question: str, listings: list[str]) -> list[str]:
    """"Which happened first: A or B?" when the reply names the later of the two. The entries come from the
    turn's newest-first listings (a recall_events read, or the situation's recent activity), so the earlier one
    is the one further down."""
    if not _FIRST_Q_RX.search(question or ""):
        return []
    q_words, r_words = _words(question), _words(reply)
    for listing in listings:
        entries = _entries(listing)
        asked = [i for i, e in enumerate(entries) if _named(e[2], q_words)]
        if len(asked) != 2:
            continue
        named = [i for i in asked if _named(entries[i][2], r_words)]
        if len(named) != 1:
            return []
        first, later = max(asked), min(asked)   # newest first: the one further down came first
        if named[0] == first:
            return []
        clip = lambda t: t if len(t) <= 60 else t[:59].rstrip() + "…"   # noqa: E731
        return [f"“{clip(entries[first][2])}” came first ({entries[first][0]}), then “{clip(entries[later][2])}” "
                f"({entries[later][0]})"]
    return []


def check_latest(reply: str, listings: list[str]) -> list[str]:
    """"The most recent …" naming an entry when the one timeline listing read, newest first, starts with
    a newer one (the truth eval's "which happened last" cases)."""
    if len(listings) != 1 or "newest first" not in listings[0] or not _LATEST_RX.search(reply or ""):
        return []
    entries = [(m.group("when"), m.group("ago"), " ".join(_TL_MARK_RX.sub("", m.group("text")).split()))
               for m in _TL_ENTRY_RX.finditer(listings[0])]
    if len(entries) < 2:
        return []
    said = _words(reply)
    named = [i for i, e in enumerate(entries) if _named(e[2], said)]
    if not named or 0 in named:
        return []
    when, ago, text = entries[0]
    text = text if len(text) <= 80 else text[:79].rstrip() + "…"
    return [f"the most recent timeline entry is from {when} ({ago}): {text}"]


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS_FULL = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december")
_CAL_CLAIM_RX = re.compile(r"\b(?:you(?:'|’)?(?:ve| have| got)|you(?:'|’)re (?:free|busy|clear)|(?:nothing|no events?)\b"
                           r"[^.?!\n]{0,25}\b(?:on|scheduled|planned|booked)|your (?:calendar|schedule)|is on your calendar)\b", re.I)
_DAY_WORD_RX = re.compile(r"\b(?P<rel>today|tonight|tomorrow|yesterday)\b|\b(?P<wd>" + "|".join(_WEEKDAYS)
                          + r")\b|\b(?P<mon>" + "|".join(m[:3] for m in _MONTHS_FULL) + r")[a-z]*\.? (?P<d>\d{1,2})\b", re.I)


# A reminder, a bill or a to-do is due on a day; that's not the calendar ("you have a reminder due Oct 9").
_NOT_CALENDAR_RX = re.compile(r"\b(?:remind\w*|to-?dos?|tasks?|due|bills?|overdue|birthday)\b", re.I)


def _resolve(word: str, today: date) -> date | None:
    """'today', 'tomorrow', 'yesterday' or an ISO date (time optional) as a date. None: unreadable."""
    from datetime import timedelta
    w = (word or "").strip().lower()
    rel = {"today": 0, "tonight": 0, "now": 0, "tomorrow": 1, "yesterday": -1}
    if w in rel:
        return today + timedelta(days=rel[w])
    try:
        return date.fromisoformat(w[:10])
    except ValueError:
        return None


def read_days(reads: list[dict[str, Any]], today: date) -> set[date] | None:
    """Every day the turn's calendar reads covered. None when one read's range can't be worked out (an
    app default, a form not parsed here): then no range claim is checked."""
    from datetime import timedelta
    days: set[date] = set()
    for r in reads:
        if r["tool"] not in CALENDAR_TOOLS:
            continue
        rng = r.get("range") or ["", ""]
        a = _resolve(rng[0], today)
        b = _resolve(rng[1] or rng[0], today)
        if a is None or b is None or not 0 <= (b - a).days <= 62:
            return None
        while a <= b:
            days.add(a)
            a += timedelta(days=1)
    return days


def _claimed_days(sentence: str, today: date) -> list[list[date]]:
    """The days a sentence names, each as its candidates (a bare weekday: last week's, this week's, next week's)."""
    from datetime import timedelta
    out: list[list[date]] = []
    for m in _DAY_WORD_RX.finditer(sentence):
        if m.group("rel"):
            out.append([_resolve(m.group("rel"), today)])
        elif m.group("wd"):
            ahead = (_WEEKDAYS.index(m.group("wd").lower()) - today.weekday()) % 7
            out.append([today + timedelta(days=ahead + k) for k in (-7, 0, 7)])
        else:
            mon = next(n for n, name in enumerate(_MONTHS_FULL, 1) if name.startswith(m.group("mon").lower()[:3]))
            try:
                out.append([_year_for(mon, int(m.group("d")), today)])
            except ValueError:
                continue
    return out


_PAST_RX = re.compile(r"\b(?:was|were|did|had|happened|went)\b", re.I)
_TWO_DAYS_RX = re.compile(r"\bthe day (after tomorrow|before yesterday)\b", re.I)


def asked_days(question: str, today: date) -> list[tuple[str, date]]:
    """The days a question names, each resolved to one date in code (truth r7/10-02: the local model read
    Friday for "Saturday" and today for "yesterday"): today, tonight, tomorrow, yesterday, the day after
    tomorrow, a weekday (the coming one, today included; "last Monday" or a past-tense question: the one just
    gone; "next Monday" from Monday: a week on) and a month-day date."""
    from datetime import timedelta
    q = question or ""
    out: list[tuple[str, date]] = []
    for m in _TWO_DAYS_RX.finditer(q):
        out.append((m.group(0), today + timedelta(days=2 if "after" in m.group(1).lower() else -2)))
    past = bool(_PAST_RX.search(q))
    for m in _DAY_WORD_RX.finditer(_TWO_DAYS_RX.sub(" ", q)):
        if m.group("rel"):
            d = _resolve(m.group("rel"), today)
        elif m.group("wd"):
            i = _WEEKDAYS.index(m.group("wd").lower())
            before = q[max(0, m.start() - 6):m.start()].lower()
            back = (today.weekday() - i) % 7
            if "last " in before or (past and "next " not in before):
                d = today - timedelta(days=back or (7 if "last " in before else 0))
            else:
                ahead = (i - today.weekday()) % 7
                d = today + timedelta(days=ahead or (7 if "next " in before else 0))
        else:
            mon = next(n for n, name in enumerate(_MONTHS_FULL, 1) if name.startswith(m.group("mon").lower()[:3]))
            try:
                d = _year_for(mon, int(m.group("d")), today)
            except ValueError:
                continue
        if d is not None and all(d != x for _, x in out):
            out.append((m.group(0).strip(), d))
    return out


_TRIP_WORDS = {"appointment", "appt", "the", "my", "our", "to", "for", "at", "event"}


def event_for_destination(destination: str, events: list[Event], days: list[date], now: datetime) -> Event | None:
    """The timed event a travel destination names ("the dentist" → "Dentist [at: Pearl Dental]"), on a day
    the question names, else the next one from now. None when nothing matches or two match equally."""
    want = {w for w in _words(destination) if w not in _TRIP_WORDS}
    if not want:
        return None
    hits = [e for e in events if e.t0 and not e.all_day and want & _words(e.line)]
    if days:
        hits = [e for e in hits if e.start in days]
    else:
        hits = [e for e in hits if datetime.combine(e.start, e.t0) >= now]
    hits.sort(key=lambda e: (e.start, e.t0))
    if not hits or (len(hits) > 1 and (hits[0].start, hits[0].t0) == (hits[1].start, hits[1].t0)):
        return None
    return hits[0]


def range_for_question(start: str, end: str, named: list[date], today: date) -> tuple[str, str] | None:
    """A list_calendar_events range moved onto the days the question names, when it covers none of them;
    None when it covers one, or when its range can't be read. A single named day keeps the call's
    times of day ("yesterday evening": 17:00-20:00 on the right day)."""
    if not named:
        return None
    a, b = _resolve(start, today), _resolve(end or start, today)
    if a is None or b is None:
        return None
    if any(a <= d <= b for d in named):
        return None
    d0, d1 = min(named), max(named)
    timed = len(start) > 10 and start[10] == "T" and len(end or "") > 10 and end[10] == "T" and d0 == d1
    if timed:
        return d0.isoformat() + start[10:], d1.isoformat() + end[10:]
    return f"{d0.isoformat()}T00:00:00", f"{d1.isoformat()}T23:59:59"


def check_range(reply: str, reads: list[dict[str, Any]], today: date) -> list[str]:
    """A calendar claim ("you have…", "you're free…", "nothing on…") about a day none of this turn's calendar
    reads covered. Only when the calendar was read (no read at all is check_denial's), and only when every
    read's range is known."""
    if not any(r["tool"] in CALENDAR_TOOLS for r in reads):
        return []
    covered = read_days(reads, today)
    if not covered:
        return []
    for sent in _sentences(reply):
        if not _CAL_CLAIM_RX.search(sent) or _NOT_CALENDAR_RX.search(sent):
            continue
        for cands in _claimed_days(sent, today):
            if not any(c in covered for c in cands):
                d = min(cands, key=lambda c: abs((c - today).days))
                return [f"I didn't read your calendar for {d:%A, %B} {d.day}; ask about that day and I'll check it"]
    return []


_CAL_SOURCE_RX = re.compile(r"\baccording to (?:your|the)(?: shared| household| family)*\s*calendars?\b|"
                            r"\b(?:your|the)(?: shared| household| family)* calendars? (?:shows?|says|lists|has)\b", re.I)


def check_source(reply: str, reads: list[dict[str, Any]]) -> list[str]:
    """The reply cites the calendar, but this turn read none: it came from memory or a note."""
    if any(r["tool"] in CALENDAR_TOOLS for r in reads) or not _CAL_SOURCE_RX.search(reply or ""):
        return []
    return ["I didn't read the calendar this turn; that came from what I remember and may be out of date"]


# A reply that cites a source no tool read this turn (#953, 10-04: "The emails confirm those other bills and orders
# were indeed old notices" after only find_records). Calendar has its own check above. Each kind: the words that cite
# it, and the tools that read it.
SOURCE_READERS: dict[str, tuple[str, frozenset[str]]] = {
    "email": ("your email", frozenset({"open_item", "kb_retrieve", "mail_cleanup_preview", "mail_archive", "item_body",
                                       "search_mail", "mail_awaiting_reply", "mail_move_preview",
                                       "mail_unsubscribe_preview"})),   # the server mail reads (#1642, #1644)
    "messages": ("your messages", frozenset({"search_messages", "open_item", "item_body"})),
    "notes": ("your notes", frozenset({"search_notes", "kb_retrieve", "kb_list"})),
    # `files` is what the model is declared (#1251 C); what runs, and counts as read, is its mode's own tool
    "documents": ("that document", frozenset({"read_file", "file_read", "search_file_contents", "files", "kb_retrieve",
                                               "open_item", "item_body"})),
    # a bill's statement: its record was filed from the mail, so the bill tools read it too
    "statement": ("that statement", frozenset({"read_file", "file_read", "search_file_contents", "files", "kb_retrieve",
                                                "open_item", "item_body", "find_records", "open_obligations"})),
}
_CITE = r"(?:confirms?|shows?|says?|said|indicates?|mentions?|lists?|states?|notes?|reads?|has|had)"
_SOURCE_CLAIM_RX = {
    "email": re.compile(rf"\b(?:the|your|those|these|that|an?|their)\s+(?:e-?mails?|mails?|inbox|messages? in your inbox)\s+{_CITE}\b|"
                        rf"\baccording to (?:the|your|those|these|that) (?:e-?mails?|mails?|inbox)\b|"
                        rf"\b(?:in|from) (?:the|your|those|these) (?:e-?mails?|inbox)\b,? (?:it|they|you) (?:can see|see|says?)\b", re.I),
    "messages": re.compile(rf"\b(?:the|your|those|these|that|their)\s+(?:text messages?|texts?|messages?|imessages?|chats?)\s+{_CITE}\b|"
                           rf"\baccording to (?:the|your|those|these|that) (?:text messages?|texts?|messages?)\b", re.I),
    "notes": re.compile(rf"\b(?:the|your|those|these|that)\s+notes?\s+{_CITE}\b|\baccording to (?:the|your|those|these|that) notes?\b", re.I),
    "documents": re.compile(rf"\b(?:the|your|that|this)\s+(?:document|pdf|file|letter|contract)\s+{_CITE}\b|"
                            rf"\baccording to (?:the|your|that|this) (?:document|pdf|file|letter|contract)\b", re.I),
    "statement": re.compile(rf"\b(?:the|your|that|this)\s+statements?\s+{_CITE}\b|"
                            rf"\baccording to (?:the|your|that|this) statements?\b", re.I),
}


def unread_sources(reply: str, reads: list[dict[str, Any]]) -> list[tuple[str, str, frozenset[str]]]:
    """The sources the reply cites that no read this turn covers: [(kind, words, the tools that would read it)]."""
    used = {r["tool"] for r in reads}
    out = []
    for kind, rx in _SOURCE_CLAIM_RX.items():
        label, readers = SOURCE_READERS[kind]
        if rx.search(reply or "") and not (used & readers):
            out.append((kind, label, readers))
    return out


# A source the person NAMED that the turn never read (#953 reopened, 10-05 trade-in baseline: "check my email and my
# texts" read texts only and the reply didn't say email was skipped). The reply must say it couldn't check it.
_ASKED_RX = {
    "email": re.compile(r"\b(?:my|our|the|your)\s+(?:e-?mails?|inbox|mail(?:box)?)\b|\bcheck\s+(?:e-?mail|mail)\b|"
                        r"\be-?mails?\b(?=[^.?!]*\b(?:from|about|for)\b)", re.I),
    # "the text 'canary bird'" / "summarize the text I pasted" is a piece of writing, not the Messages app: a bare singular
    # "the text" doesn't name a source (10-10: it fired a source retry on every canary turn since 10-05, +2 model calls)
    "messages": re.compile(r"(?:\b(?:my|our|your)\s+(?:texts?|text messages?|imessages?|messages)\b|"
                           r"\bthe\s+(?:texts|text messages?|imessages?|messages)\b)(?!\s+in\s+(?:my|the)\s+inbox)",
                           re.I),
    "notes": re.compile(r"\b(?:my|our|the|your)\s+notes\b", re.I),
}
# "send it to my email", "save this into my notes": a place to put something, not a source to check
_DESTINATION_RX = re.compile(r"\b(?:to|into|in)\s+(?:my|our|your)\s+(?:e-?mails?|inbox|mail|texts?|messages|notes)\b"
                             , re.I)
_PUT_RX = re.compile(r"\b(?:send|forward|save|add|put|copy|share|email|text|mail|write|note|jot)\b", re.I)
_UNCHECKED_RX = re.compile(r"\b(?:couldn(?:'|’)?t|could not|can(?:'|’)?t|cannot|didn(?:'|’)?t|did not|wasn(?:'|’)?t able to|"
                           r"unable to|not able to|no access to|don(?:'|’)?t have access to|haven(?:'|’)?t)\b[^.!?\n]{0,80}", re.I)
_KIND_WORDS = {"email": r"e-?mails?|inbox|mail", "messages": r"texts?|messages?|imessages?", "notes": r"notes?"}
# "what can you do with my mail?": about titan's abilities, not a request to read the mail (10-16 live: the reply
# listed what it can do and got "Correction: I couldn't check your email this turn"). "can you check my mail" still asks.
_ABILITY_Q_RX = re.compile(r"\bwhat\s+(?:else\s+)?(?:can|could)\s+you\s+(?:do|help)\b|\bwhat\s+are\s+you\s+able\s+to\s+do\b|"
                           r"\bwhat\s+(?:are\s+)?your\s+(?:abilities|capabilities)\b", re.I)


def asked_unread(question: str, reply: str, reads: list[dict[str, Any]]) -> list[tuple[str, str, frozenset[str]]]:
    """The sources the person named that no read this turn covers and the reply doesn't say it skipped:
    [(kind, words, the tools that would read it)]. A source the reply also cites is check_unread_source's."""
    used = {r["tool"] for r in reads}
    cited = {k for k, _, _ in unread_sources(reply, reads)}
    out = []
    for kind, rx in _ASKED_RX.items():
        label, readers = SOURCE_READERS[kind]
        if not rx.search(question or "") or used & readers or kind in cited or _ABILITY_Q_RX.search(question or ""):
            continue
        if _PUT_RX.search(question or "") and _DESTINATION_RX.search(question or "") and not re.search(
                r"\b(?:check|look|search|find|see|read|anything|any)\b", question or "", re.I):
            continue
        said = any(re.search(rf"\b(?:{_KIND_WORDS[kind]})\b", m.group(0), re.I) for m in _UNCHECKED_RX.finditer(reply or ""))
        if not said:
            out.append((kind, label, readers))
    return out


def check_asked_unread(reply: str, question: str, reads: list[dict[str, Any]]) -> list[str]:
    """The person named email, messages or notes, nothing read it, and the reply is silent about it."""
    return [f"I couldn't check {label} this turn" for _, label, _ in asked_unread(question, reply, reads)]


def check_unread_source(reply: str, reads: list[dict[str, Any]]) -> list[str]:
    """The reply cites email, messages, notes or a document, but nothing read it this turn: say what was checked."""
    return [f"I didn't read {label} this turn, so that wasn't from {label}" for _, label, _ in unread_sources(reply, reads)]


# A question that says not to list the schedule (the Mac's daily brief: "My Mac already shows me … my calendar … so
# don't list those") isn't asking for a day's listing (titan-node#1038).
_NOT_A_LISTING_Q_RX = re.compile(r"\b(?:don'?t|do not|no need to)\s+(?:list|repeat|go through)\b", re.I)
_LISTING_Q_RX = re.compile(r"\bwhat(?:'|’)?s on\b|\bwhat(?:'|’)?s (?:happening|going on)\b|\bwhat do (?:i|we) have\b|"
                           r"\b(?:schedule|calendar|agenda|plans)\b", re.I)


def _when(e: Event) -> str:
    if e.all_day or not e.t0:
        return "all day"
    return e.t0.strftime("%-I:%M %p").replace(":00 ", " ")


def check_omitted(reply: str, question: str, events: list[Event], today: date,
                  shown: set[date] | None = None) -> list[str]:
    """A day's listing (the question asks what's on; the reply names that day and two or more of its
    events) that leaves one of that day's events out. Not for a question about one time; a weekly event
    named for Friday doesn't make Monday a listing. Not for a day the app already shows in full (`shown`: the
    agenda card a calendar read draws on the Mac and iPhone, so the text is a summary), nor when the question
    says not to list it (#1038)."""
    if not _LISTING_Q_RX.search(question or "") or _TIME_IN_TEXT.search(question or ""):
        return []
    if _NOT_A_LISTING_Q_RX.search(question or ""):
        return []
    days_named = {d for cands in _claimed_days(reply, today) for d in cands}
    said = _words(reply)
    by_day: dict[date, list[Event]] = {}
    for e in events:
        by_day.setdefault(e.start, []).append(e)
    for day, evs in sorted(by_day.items()):
        if day not in days_named or day in (shown or set()):
            continue
        named = [e for e in evs if _words(e.title) and len(_words(e.title) & said) >= min(2, len(_words(e.title)))]
        left = [e for e in evs if e not in named and _words(e.title)]
        if len(named) >= 2 and left:
            e = left[0]
            return [f"the calendar also has “{e.title}” ({_when(e)}) on {day:%A}"]
    return []


_DAYS_UNTIL_RX = re.compile(r"\b(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)\s+days?\s+"
                            r"(?:until|till|to go|away|from (?:now|today)|left|before)\b", re.I)
_NUM_WORDS = {w: i for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                                          "ten"))}
_MONTH_DAY_RX = re.compile(r"\b(?P<mon>" + "|".join(m[:3] for m in _MONTHS_FULL) + r")[a-z]*\.? (?P<d>\d{1,2})(?:st|nd|rd|th)?\b",
                           re.I)


def _span(e: Event) -> str:
    return f"{e.start:%a %b} {e.start.day}" + (f" to {e.end:%a %b} {e.end.day}" if e.end != e.start else "")


def check_event_when(reply: str, events: list[Event], today: date) -> list[str]:
    """A sentence naming one event (all its distinctive words, or two of them) with a month-day date outside
    the event's days, or "N days until" that isn't the count from today to its start. Events that repeat
    under one title are skipped (which one is meant isn't clear)."""
    for sent in _sentences(reply):
        said = _words(sent)
        hit = [e for e in events if _words(e.title) and len(_words(e.title) & said) >= min(2, len(_words(e.title)))]
        if len({e.title for e in hit}) != 1 or len(hit) != 1:
            continue
        e = hit[0]
        for m in _MONTH_DAY_RX.finditer(sent):
            mon = next(n for n, name in enumerate(_MONTHS_FULL, 1) if name.startswith(m.group("mon").lower()[:3]))
            try:
                d = _year_for(mon, int(m.group("d")), today)
            except ValueError:
                continue
            if not e.start <= d <= e.end:
                return [f"“{e.title}” is {_span(e)}"]
        m = _DAYS_UNTIL_RX.search(sent)
        if m:
            n = int(m.group(1)) if m.group(1).isdigit() else _NUM_WORDS[m.group(1).lower()]
            real = (e.start - today).days
            if real >= 0 and n != real:
                return [f"“{e.title}” starts {e.start:%a %b} {e.start.day}, {real} day{'s' if real != 1 else ''} from today"]
    return []


# #1610 (live 10-16): asked why something failed, a reply guessed a cause ("a timeout would stem from local network
# routing ... or container response lag") with nothing read that shows it. A hedged cause is backed only when the
# cause's own word is in what a tool returned this turn; an honest "I don't know" or "couldn't find" needs no backing.
_WHY_FAILED_RX = re.compile(r"\bwhy\b[^?.!]{0,80}\b(?:fail\w*|time[ds]? ?out|timed out|crash\w*|brok\w*|stopp?\w*|"
                            r"go(?:ne|es)? down|went down|slow\w*|error\w*|hang\w*|hung)\b|\bwhat (?:caused|happened)\b|"
                            r"\bare you sure\b", re.I)
_CAUSE_WORDS = r"(?:routing|network|latency|lag|container|server|dns|wi-?fi|load|cloud|mail server|internet|isp|memory|disk|cpu)"
_HEDGED_CAUSE_RX = re.compile(r"\b(?:would|could|might|may)\s+(?:stem|come|result)\s+from\s+(?:the\s+|a\s+|an\s+|your\s+|its\s+)?"
                              r"(?:[\w-]+\s+){0,2}(" + _CAUSE_WORDS + r")|\b(?:most likely|probably|likely)\s+(?:due to|caused by|"
                              r"because of|from)\s+(?:the\s+|a\s+|an\s+|your\s+)?(?:[\w-]+\s+){0,2}(" + _CAUSE_WORDS + r")", re.I)
_SAYS_UNKNOWN_RX = re.compile(r"\b(?:don't|do not|can't|cannot|couldn't|could not)\s+(?:know|tell|find|see|confirm|determine)\b|"
                              r"\bnot sure\b|\bno (?:record|log|sign)\b|\bnothing (?:i|that i) (?:checked|found|read)\b", re.I)


def check_unsupported_cause(reply: str, question: str, reads: list[dict[str, Any]]) -> list[str]:
    """A "why did it fail" answered with a hedged cause nothing read this turn shows, and no "I don't know"."""
    if not _WHY_FAILED_RX.search(question or "") or _SAYS_UNKNOWN_RX.search(reply or ""):
        return []
    seen = "\n".join(str(r.get("text") or "") for r in reads).lower()
    for m in _HEDGED_CAUSE_RX.finditer(reply or ""):
        words = {w.lower() for w in re.findall(_CAUSE_WORDS, m.group(0), re.I)}
        if words and not any(w in seen for w in words):   # any of the named cause's words in what was read backs it
            return ["nothing I checked shows what caused it; that was a guess"]
    return []


def notes(reply: str, question: str, reads: list[dict[str, Any]], now: datetime) -> list[str]:
    """reads: [{tool, text}] of this turn's successful read-tool outputs."""
    today = now.date()
    cal = "\n".join(r["text"] for r in reads if r["tool"] in CALENDAR_TOOLS)
    rem = "\n".join(r["text"] for r in reads if r["tool"] in REMINDER_TOOLS)
    events, reminders = parse_events(cal, today), parse_reminders(rem, today)
    tools = {r["tool"] for r in reads if r["tool"] in READ_TOOLS}
    out = check_denial(reply, question, events, reminders, tools)
    out += check_all_day(reply, events)
    out += check_overdue(reply, reminders, now, read=bool(tools))   # "didn't check" only when nothing at all was read
    out += check_next_reminder(reply, reminders, now)
    out += check_latest(reply, [r["text"] for r in reads if r["tool"] in TIMELINE_TOOLS])
    out += check_first(reply, question, [r["text"] for r in reads if r["tool"] in TIMELINE_TOOLS or r["tool"] == "context_timeline"])
    out += check_range(reply, reads, today)
    out += check_source(reply, reads)
    out += check_unread_source(reply, reads)
    out += check_asked_unread(reply, question, reads)
    out += check_unsupported_cause(reply, question, reads)
    shown = read_days([r for r in reads if r.get("shown")], today) or set()   # drawn as a card by the app
    out += check_omitted(reply, question, events, today, shown)
    out += check_event_when(reply, events, today)
    return out


# A false "no access" (#1203, 10-07 box turn_eval): "I don't have access to Robin's text messages", "I don't have
# access to locate devices", with search_messages / find_my_locate declared that very turn. #953 rightly accepts
# "couldn't check X" as honest when X can't be read; when its reader is right there, the denial is the miss.
NO_ACCESS_READERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (kind, words that name it, its readers in order of preference)
    ("messages", r"texts?|text messages?|imessages?|messages", ("search_messages",)),
    ("find my", r"find my|locat\w*|where (?:it|they|she|he) (?:is|are)|devices?'? locations?|(?:its|their) location",
     ("find_my_locate",)),
    ("threads", r"threads?|conversations? (?:about|with)", ("threads",)),
    ("notes", r"notes", ("search_notes",)),
    ("email", r"e-?mails?|inbox|mail(?:box)?", ("search_mail",)),
    ("calendar", r"calendars?|schedule", ("list_calendar_events",)),
    ("reminders", r"reminders", ("list_reminders",)),
    ("the house", r"home assistant|thermostat|lights?|the house|devices in the house", ("home_status",)),
    ("photos", r"photos?|pictures?", ("photos_find", "view_image")),
    # #1251 C: `files` is declared; find_files / search_file_contents are what a `files` call runs as
    ("files", r"files?|documents?", ("files", "find_files", "search_file_contents")),
)
_DENY_RX = re.compile(r"\b(?:I\s+)?(?:don(?:'|’)?t|do not|can(?:'|’)?t|cannot|am unable to|(?:'|’)m unable to|am not able to|"
                      r"(?:'|’)m not able to|have no)\s+(?:have\s+)?(?:any\s+)?(?:access(?:\s+to)?|see|read|check|look at|"
                      r"view|locate|get to)\b([^.!?\n]{0,80})", re.I)


def false_no_access(reply: str, declared: set[str], used: set[str]) -> list[tuple[str, str]]:
    """[(kind, reader)] for each source the reply says it can't access while a reader for it is declared this turn and
    none ran. [] when the reply denies nothing, or denies something it truly can't reach."""
    out: list[tuple[str, str]] = []
    for m in _DENY_RX.finditer(reply or ""):
        span = m.group(0)
        for kind, words, readers in NO_ACCESS_READERS:
            if not re.search(rf"\b(?:{words})\b", span, re.I):
                continue
            have = [r for r in readers if r in declared]
            if have and not used & set(readers) and kind not in {k for k, _ in out}:
                out.append((kind, have[0]))
    return out
