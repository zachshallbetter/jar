"""Calendar and reminder listings, put in order and marked against now, in code, before the model
reads them (10-01 goal: time, day and order of operations right across the calendar, schedule and
reminders). The devices send lines like

  • Thu Oct 1, 9:00 AM–10:00 AM — Dentist [at: Pearl Dental]
  • Thu Oct 1, 9:00 AM — Dentist                      (older apps: no end)
  • Fri Oct 2, all day — Ava's field trip
  • Thu Oct 1, 10:00 PM – Fri Oct 2, 1:00 AM — Movie
  • Mon Oct 5, all day – Wed Oct 7 — Spokane trip
  • Pay the water bill (due Thu Oct 1, 9:00 AM)  /  (due Thu Oct 1)

and the model had to work out which is now, which is next, how long until, what overlaps and what
is overdue (the truth eval's misses: "what's next?" named tomorrow's dentist, "until when?" had no
end to read). Here the lines come back sorted by time with a note on each:

  • Thu Oct 1, 9:00 AM–10:00 AM — Dentist [now · until 10:00 AM]
  • Thu Oct 1, 3:00 PM–4:00 PM — Piano lesson [next · in 5 h 10 min · overlaps Leo soccer]
  • Pay the water bill (due Thu Oct 1, 9:00 AM) [overdue by 1 h 30 min]

Lines it can't read are kept as they were, in place. Pure: `now` is passed in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_MON = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
_DAY = r"(?P<{p}wd>Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\.?,?\s+(?P<{p}mon>[A-Z][a-z]{{2}})\w*\.?\s+(?P<{p}day>\d{{1,2}})(?:,?\s+(?P<{p}year>\d{{4}}))?"
_TIME = r"(?P<{p}h>\d{{1,2}}):(?P<{p}m>\d{{2}})\s*(?P<{p}ap>[AaPp])\.?\s*[Mm]\.?"
_SP = r"[\s  ]*"

# • <day>, <time>[–<time> | – <day>, <time>] — title     |  • <day>, all day[ – <day>] — title
_EVENT = re.compile(
    r"^(?P<lead>\s*[•\-*]\s*)" + _DAY.format(p="s") + r",?" + _SP
    + r"(?:(?P<allday>all day)|" + _TIME.format(p="s") + r")"
    + r"(?:" + _SP + r"[–-]" + _SP + r"(?:" + _DAY.format(p="e") + r",?" + _SP + r")?(?:" + _TIME.format(p="e") + r")?)?"
    + _SP + r"[—–-]{1,2}" + _SP + r"(?P<rest>.+)$")
_REMINDER = re.compile(r"^(?P<lead>\s*[•\-*]\s*)(?P<title>.+?)\s*\(due\s+" + _DAY.format(p="s")
                       + r"(?:,?" + _SP + _TIME.format(p="s") + r")?\)(?P<tail>.*)$")


# The apps' own notes after a title ("[at: …]", "[calendar: …]", "[on calendars: …]", "[2 copies on …]"),
# so a title's own brackets ("Standup [remote]") stay part of it.
_NOTES = re.compile(r"(?:\s+\[(?:at|calendar|on calendars|repeats): [^\]]*\]|\s+\[\d+ copies on [^\]]*\]|\s+—\s.*'s\b)+\s*$")


def _secs(a: datetime, b: datetime) -> float:
    """Real seconds from a to b (same-zone datetimes subtract as wall time, wrong across DST)."""
    return b.timestamp() - a.timestamp()


@dataclass
class _Item:
    index: int
    line: str
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    title: str = ""


_WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _year_for(mon: int, day: int, now: datetime, weekday: str | None = None, after: datetime | None = None) -> int:
    """A date shown without a year. The weekday decides when it can ("Thu Jan 15" is 2026 when
    seen in October 2026: an old overdue reminder, not one 106 days out); otherwise the year that
    puts it nearest now. `after`: an end is never before its start."""
    cands = []
    for y in (now.year - 1, now.year, now.year + 1, now.year + 2):
        try:
            cands.append(datetime(y, mon, day, tzinfo=now.tzinfo))
        except ValueError:
            continue
    if after is not None:
        cands = [d for d in cands if d.date() >= after.date()] or cands
    if weekday and weekday[:3] in _WD:
        match = [d for d in cands if d.weekday() == _WD.index(weekday[:3])]
        if match:
            cands = match
    if not cands:
        return now.year
    return min(cands, key=lambda d: abs((d - (after or now)).total_seconds())).year


def _date(m: re.Match[str], p: str, now: datetime, after: datetime | None = None) -> datetime | None:
    mon = _MON.get((m.group(f"{p}mon") or "")[:3])
    if not mon or not m.group(f"{p}day"):
        return None
    day = int(m.group(f"{p}day"))
    year = (int(m.group(f"{p}year")) if m.group(f"{p}year")
            else _year_for(mon, day, now, m.group(f"{p}wd"), after))
    try:
        return datetime(year, mon, day, tzinfo=now.tzinfo)
    except ValueError:
        return None


def _at(d: datetime, m: re.Match[str], p: str) -> datetime | None:
    if not m.group(f"{p}h"):
        return None
    h, mi, ap = int(m.group(f"{p}h")), int(m.group(f"{p}m")), m.group(f"{p}ap").lower()
    if not (1 <= h <= 12 and 0 <= mi <= 59):
        return None
    h = (h % 12) + (12 if ap == "p" else 0)
    return d.replace(hour=h, minute=mi)


def _clock(d: datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0")


def span(seconds: float) -> str:
    """"5 min", "2 h", "3 h 10 min", "2 days"."""
    s = int(abs(seconds))
    if s < 60:
        return "under a minute"
    days, rem = divmod(s, 86400)
    if days >= 2:
        return f"{days} days"
    h, rem = divmod(s, 3600)
    mi = rem // 60
    if h and mi:
        return f"{h} h {mi} min"
    return f"{h} h" if h else f"{mi} min"


def _day_word(d: datetime, now: datetime) -> str | None:
    delta = (d.date() - now.date()).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(delta)


def _parse_event(i: int, line: str, now: datetime) -> _Item:
    m = _EVENT.match(line)
    if not m:
        return _Item(i, line)
    sday = _date(m, "s", now)
    if sday is None:
        return _Item(i, line)
    title = _NOTES.sub("", m.group("rest")).strip()
    if m.group("allday"):
        eday = _date(m, "e", now, after=sday) if m.group("emon") else sday
        return _Item(i, line, sday, (eday or sday) + timedelta(days=1), True, title)
    start = _at(sday, m, "s")
    if start is None:
        return _Item(i, line)
    end = None
    if m.group("eh"):
        eday = _date(m, "e", now, after=sday) if m.group("emon") else sday
        end = _at(eday or sday, m, "e")
        if end is not None and end <= start and not m.group("emon"):
            end += timedelta(days=1)   # 11:00 PM–1:00 AM
    return _Item(i, line, start, end, False, title)


def _event_note(it: _Item, now: datetime, is_next: bool, overlaps: list[str]) -> str:
    notes: list[str] = []
    if it.all_day:
        if it.start <= now < it.end:
            notes.append("today, all day" if (it.end - it.start).days <= 1 else f"under way, until {(it.end - timedelta(days=1)):%a %b} {(it.end - timedelta(days=1)).day}")
        elif it.start > now:
            w = _day_word(it.start, now)
            notes.append(w if w else f"in {span((it.start.date() - now.date()).days * 86400)}")
        else:
            notes.append("over")
    else:
        if it.end is not None and it.start <= now < it.end:
            until = _clock(it.end) if it.end.date() == now.date() else f"{it.end:%a %b} {it.end.day}, {_clock(it.end)}"
            notes.append(f"now · until {until}")
        elif it.start > now:
            if is_next:
                notes.append("next")
            # Today: how long until; later days: which day ("in 29 h 30 min" reads worse than "tomorrow").
            if it.start.date() == now.date():
                notes.append(f"in {span(_secs(now, it.start))}")
            else:
                notes.append(_day_word(it.start, now) or f"in {span((it.start.date() - now.date()).days * 86400)}")
        elif it.end is None and it.start.date() == now.date():
            # An older app sent no end: it started, and whether it's over can't be told.
            notes.append(f"started at {_clock(it.start)}")
        else:
            notes.append("ended" if _day_word(it.start, now) == "today" else "past")
    if overlaps:
        notes.append("overlaps " + ", ".join(overlaps))
    return " [" + " · ".join(notes) + "]"


def order_events(text: str, now: datetime) -> str:
    """A list_calendar_events result, sorted by start and marked (now / next · in … / ended /
    overlaps …). Unreadable or non-event lines stay where they were, before the events."""
    lines = (text or "").split("\n")
    items = [_parse_event(i, l, now) for i, l in enumerate(lines)]
    events = [it for it in items if it.start is not None]
    if len(events) == 0:
        return text
    others = [it for it in items if it.start is None and it.line.strip()]
    events.sort(key=lambda it: (it.start, not it.all_day, it.index))
    upcoming = [it for it in events if not it.all_day and it.start > now]
    nxt = upcoming[0].index if upcoming else None
    out = [it.line for it in others]
    for it in events:
        short = lambda e: not e.all_day and e.end is not None and _secs(e.start, e.end) < 86400   # noqa: E731
        over = [o.title for o in events if o is not it and short(o) and short(it)
                and o.start < it.end and it.start < o.end]
        out.append(it.line + _event_note(it, now, it.index == nxt, over))
    return "\n".join(out)


def order_reminders(text: str, now: datetime) -> str:
    """A list_reminders result: soonest due first (no due date last), each marked overdue by … /
    due today · in … / due tomorrow / in N days."""
    lines = (text or "").split("\n")
    dated: list[tuple[datetime, bool, int, str]] = []
    rest: list[str] = []
    for i, line in enumerate(lines):
        m = _REMINDER.match(line)
        day = _date(m, "s", now) if m else None
        if not m or day is None:
            if line.strip():
                rest.append(line)
            continue
        at = _at(day, m, "s")
        if at is not None and at.hour == 0 and at.minute == 0:
            at = None   # older apps printed a day-only due date as 12:00 AM
        dated.append((at or day, at is not None, i, line))
    if not dated:
        return text
    dated.sort(key=lambda x: (x[0], x[2]))
    out = []
    for due, timed, _, line in dated:
        if timed:
            if due < now and (now.date() - due.date()).days >= 2:
                note = f"overdue since {due:%a %b} {due.day}"
            elif due < now:
                note = f"overdue by {span(_secs(due, now))}"
            elif due.date() == now.date():
                note = f"due today · in {span(_secs(now, due))}"
            else:
                note = _day_word(due, now) or f"in {span((due.date() - now.date()).days * 86400)}"
        else:
            if due.date() < now.date():
                note = f"overdue since {due:%a %b} {due.day}"
            else:
                note = "due today" if due.date() == now.date() else (_day_word(due, now) or f"in {span((due.date() - now.date()).days * 86400)}")
        out.append(f"{line} [{note}]")
    no_due = [l for l in rest if l.lstrip().startswith(("•", "-", "*"))]
    head = [l for l in rest if l not in no_due]
    return "\n".join(head + out + no_due)
