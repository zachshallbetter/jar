#!/usr/bin/env python3
"""The local model's schedule, read for it and laid out in code (titan-node#347).

The truth eval (round 5, 10-01) had the local Qwen3-8B at 17/32 against Gemini's 30/32. Five of its
calendar misses never called the calendar ("I don't have any information about your dentist
appointment"), others put an all-day event at a time or listed one day's events under another.
When a schedule question goes to the local model (the Gemini-failed fallback, local-pinned turns),
titan reads the calendar itself, for yesterday through a week ahead, and the model gets a digest:
  - each day labelled the way people ask ("Tomorrow (Fri Oct 2)"), empty days said as "nothing";
  - what's under way now and what's next, with minutes to go;
  - all-day events marked as not taking up any time;
  - reminders, when asked about: overdue, due today, next.
Facts in code; the model only phrases them.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import claim_checks as cc

SCHEDULE_Q = re.compile(
    r"\b(?:calendar|schedule[ds]?|appointments?|events?|meetings?|free|busy|available|today|tonight|tomorrow|"
    r"yesterday|this (?:week|weekend|morning|afternoon|evening)|weekend|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"what's next|whats next|next (?:thing|event|meeting|appointment)|right now|am i in|when is|when's|what time|"
    r"how many days|days until|trip|lesson|practice|class|leave for)\b", re.I)
# #1230: a repeating item's cadence or next time ("How often does the compost go out?", "When does it go out next?")
# matched none of the words above, so the local model got no digest and answered from an old saved note. These broader
# words are for the LOCAL prefetch only: the digest stays on titan there. The Gemini prefetch keeps SCHEDULE_Q alone,
# so "When does the store close?" never sends the asker's schedule to Gemini (JEV's review of #1237).
LOCAL_SCHEDULE_Q = re.compile(r"\b(?:how often|when (?:does|do|will)|next time|goes? out|every (?:week|other week|month))\b",
                              re.I)
# The apps list at most 40 events (Mac DeviceExecutor and iPhone PhoneDevice: `.prefix(40)`), silently.
APP_EVENT_CAP = 40
REMINDER_Q = re.compile(r"\b(?:reminders?|overdue|due|to-?dos?|tasks?)\b", re.I)


def wants_schedule(question: str, local: bool = False) -> bool:
    """A schedule question. local=True (the local brain's prefetch, nothing leaves titan) also takes the broader cadence
    and next-time words; the Gemini prefetch never does."""
    q = question or ""
    return bool(SCHEDULE_Q.search(q) or (local and LOCAL_SCHEDULE_Q.search(q)))


def wants_reminders(question: str) -> bool:
    return bool(REMINDER_Q.search(question or ""))


def window(now: datetime) -> dict[str, str]:
    """Yesterday through a week ahead, the args list_calendar_events takes."""
    d0 = (now - timedelta(days=1)).date()
    d1 = (now + timedelta(days=7)).date()
    return {"start": f"{d0.isoformat()}T00:00:00", "end": f"{d1.isoformat()}T23:59:59"}


def _label(d: date, today: date) -> str:
    rel = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get((d - today).days)
    plain = f"{d:%a %b} {d.day}"
    return f"{rel} ({plain})" if rel else plain


def _clock(t) -> str:
    return t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


# The device's notes on whose an event is (jellybean's EventText.distinctLines): "[calendar: X]" for one copy,
# "[N copies on X]" for several on one calendar, "[on calendars: A, B]" for copies on several. The device's note comes
# after the title, so it's the LAST one on the line; after it, only device markers ([at: …], [repeats: …]).
_ANY_NOTE = re.compile(r"\[(?:calendar: |\d+ copies on |on calendars: )")
_TAIL = r"(?P<after>(?: \[(?!calendar: |\d+ copies on |on calendars: )[^\]]*\])*)\s*$"
# jellybean#1037: a single-calendar note may carry the device's own calendar id after its flags ("[calendar: Work, shared,
# id: 6A3F-…]", "[2 copies on Work, id: …]"); "[on calendars: …]" never does.
_ID = r"(?:, id: (?P<id>[A-Za-z0-9._:-]{1,128}))?"
_NOTE_KINDS = (re.compile(r"\[calendar: (?P<cal>.+?)(?:, shared)?" + _ID + r"(?: —[^\]]*)?\]" + _TAIL),
               re.compile(r"\[\d+ copies on (?P<cal>.+?)" + _ID + r"(?: —[^\]]*)?\]" + _TAIL),
               re.compile(r"\[on calendars: (?P<cals>.+?)(?: —[^\]]*)?\]" + _TAIL))


def _calendars_of(line: str) -> tuple[str, list[str] | None]:
    kind, cals, _cid = _calendar_note(line)
    return kind, cals


_ID_STRIP_RX = re.compile(r"(\[(?:calendar: |\d+ copies on )[^\]\n]*?), id: [A-Za-z0-9._:-]{1,128}(?=[\] ]|$)")


def without_ids(text: str) -> str:
    """The read without the devices' calendar ids (jellybean#1037): they're for matching a device's own claims and
    never reach a model."""
    return _ID_STRIP_RX.sub(r"\1", text or "")


def _calendar_note(line: str) -> tuple[str, list[str] | None, str | None]:
    """("none", None) with no note; ("one", [name]) for a "[calendar: X]" or "[N copies on X]" note; ("several",
    [pieces]) for "[on calendars: A, B]"; ("bad", None) when there's more than one note of any kind (an event title
    can carry a fake one: anyone who sends an invite writes its title) or the note won't parse. JEV's reviews of #1231."""
    found = list(_ANY_NOTE.finditer(line))
    if not found:
        return "none", None, None
    if len(found) > 1:
        return "bad", None, None
    tail = line[found[0].start():]
    for rx in _NOTE_KINDS:
        m = rx.match(tail)
        if m:
            if "cals" in m.groupdict():
                return "several", [x.strip() for x in m.group("cals").split(",") if x.strip()], None
            cal = m.group("cal").strip()
            return ("one", [cal], m.group("id")) if cal else ("bad", None, None)
    return "bad", None, None


def _may_see(owner: dict | None, viewer_id: str | None, viewer_role: str | None, parents_of: Any) -> bool:
    if not owner:
        return True   # unclaimed: a household calendar
    oid = str(owner.get("id") or "")
    mine = bool(viewer_id) and oid == viewer_id and viewer_role != "household"
    parent = (viewer_role in ("admin", "adult") and owner.get("role") == "kid" and bool(viewer_id)
              and viewer_id in (parents_of(oid) or []))
    return mine or parent


def visible_to(text: str, viewer_id: str | None, viewer_role: str | None, claim: Any, parents_of: Any,
               id_claim: Any = None) -> str:
    """The calendar read cut to what this viewer may see (#1230), before anything is derived from it: the digest's day
    list and every fact (now, next, overlaps, firsts, next occurrence) come only from what's left. A device lists what
    its account can see, which can include someone else's calendar shared to it (a TV signed in as a parent, a spouse's
    calendar on a phone); a sentence like "Therapy at 3 overlaps soccer" must never draw on another person's.
      claim(title) → {id, role} of the person Settings › Household gives that calendar to, or None (a household one)
      parents_of(id) → that person's parents' ids
    Kept: the viewer's own calendars; for an adult, their own kids'; unclaimed (household) calendars, but on a shared
    screen only those. "[on calendars: A, B]" is dropped if any calendar in it (or any run of comma pieces, since a
    name may hold a comma) is claimed by someone the viewer may not see. More than one note, or one that won't parse:
    dropped (fail closed). No note (an older app): kept.
      id_claim(cal_id) → {owner: {id, role}, widens: bool} for the claim the device that ran the read made for that
      calendar (jellybean#1037), or None. A claim always NARROWS: an owner this viewer may not see drops the event. It
      WIDENS (keeps what the title rule hides) only when `widens` (an adult or the admin claiming for themselves or
      their own kids, judged now); otherwise the title rule must allow it too (#1268: a kid's device can list a
      parent's calendar and call it hers). No claim: the title rule, #1259's two-claimant fail-closed included. Ids
      stay in the text: without_ids strips them after. A line that isn't a bullet (a title carrying a newline)
    belongs to the bullet above it: the entry is judged whole and goes as one."""
    # One entry is a bullet line plus the lines after it that aren't bullets (a title carrying a newline puts the
    # device's note on the last of them), judged whole and kept or dropped together. Lines before the first bullet
    # (a header such as "No events between…") stay.
    entries: list[list[str]] = [[]]
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("•"):
            entries.append([line])
        else:
            entries[-1].append(line)
    out = list(entries[0])
    for entry in entries[1:]:
        kind, cals, cid = _calendar_note(" ".join(x.strip() for x in entry))
        if kind == "bad":
            keep = False
        elif kind == "one":
            by_id = id_claim(cid) if (cid and id_claim) else None
            by_title = lambda: _may_see(claim(cals[0]), viewer_id, viewer_role, parents_of)  # noqa: E731
            if by_id:
                keep = _may_see(by_id["owner"], viewer_id, viewer_role, parents_of) and (bool(by_id.get("widens"))
                                                                                         or by_title())
            else:
                keep = by_title()
        elif kind == "several":
            runs = {", ".join(cals[i:j]) for i in range(len(cals)) for j in range(i + 1, len(cals) + 1)}
            keep = all(_may_see(claim(r), viewer_id, viewer_role, parents_of) for r in runs)
        else:
            keep = True
        if keep:
            out.extend(entry)
    return "\n".join(out)


def calendar_digest(text: str, now: datetime, days: int = 8, facts: bool = True) -> str:
    """The digest, or the tool's own lines when they can't be read (a format change must never turn into a
    false "nothing")."""
    today = now.date()
    events = cc.parse_events(text, today)
    if not events:
        if re.search(r"^•", text or "", re.M):
            return "[SCHEDULE, read from the calendar just now]\n" + text.strip()
        if not re.match(r"\s*No events between", text or ""):
            return ""   # an error or a refusal ("Calendar access not granted."): never a false "nothing"
    capped = len(re.findall(r"^•", text or "", re.M)) >= APP_EVENT_CAP
    last_read = max(e.start for e in events) if capped and events else None
    lines = []
    for k in range(-1, days):
        d = today + timedelta(days=k)
        if last_read and d > last_read:
            lines.append(f"- {_label(d, today)}: (not read: the calendar listing stopped at {APP_EVENT_CAP} events)")
            continue
        todays = [e for e in events if e.start <= d <= e.end]
        allday = [f"{e.title} (all day" + (f", {_label(e.start, today)} – {_label(e.end, today)}, "
                                           f"{(e.end - e.start).days + 1} days" if e.end != e.start else "")
                  + "; doesn't take up any time)" for e in todays if e.all_day]
        timed = sorted((e for e in todays if not e.all_day and e.t0), key=lambda e: e.t0)
        items = allday + [f"{_clock(e.t0)}{('–' + _clock(e.t1)) if e.t1 else ''} {e.title}" for e in timed]
        lines.append(f"- {_label(d, today)}: " + ("; ".join(items) if items else "nothing"))
    head, facts_ = _head_lines(events, now, today, days)
    title = ("[SCHEDULE, read from the calendar just now; answer from this. The listing stopped at "
             f"{APP_EVENT_CAP} events: days marked (not read) weren't covered, so read the calendar for those]"
             if capped else "[SCHEDULE, read from the calendar just now; answer from this, it is complete for these days]")
    computed = facts_ if facts else []
    return (title + "\n" + " · ".join(head) + "\n" + (("Facts: " + " · ".join(computed) + "\n") if computed else "")
            + "\n".join(lines))


_REPEATS_RX = re.compile(r"\[repeats:\s*([^\]]+)\]", re.I)


def _span(m: int) -> str:
    """"2 h 10 min", "50 min", "3 h"."""
    h, mm = divmod(max(0, m), 60)
    return f"{h} h {mm} min" if h and mm else (f"{h} h" if h else f"{mm} min")


def _clean_title(e) -> str:
    return _REPEATS_RX.sub("", e.title).strip()


def _times(e) -> str:
    return f"{_clock(e.t0)}{('–' + _clock(e.t1)) if e.t1 else ''}"


def _head_lines(events, now: datetime, today: date, days: int) -> tuple[list[str], list[str]]:
    """The facts the model shouldn't work out itself (#1064, Box's #402 gap): what's on now, the next event and how
    long until it, the first event of today and tomorrow, which events overlap, and when each repeating event comes
    next. → (the header line's parts, the fact lines under it)."""
    t = now.time()
    on_now = [e for e in events if not e.all_day and e.t0 and e.start == today and e.t0 <= t < (e.t1 or e.t0)]
    later = sorted((e for e in events if not e.all_day and e.t0
                    and (e.start > today or (e.start == today and e.t0 > t))), key=lambda e: (e.start, e.t0))
    head = []
    if on_now:
        e = on_now[0]
        head.append(f"Now: {_clean_title(e)}" + (f" (until {_clock(e.t1)})" if e.t1 else ""))
    else:
        head.append("Now: nothing scheduled")
    if later:
        e = later[0]
        mins = int((datetime.combine(e.start, e.t0) - now).total_seconds() // 60)
        when = f"in {_span(mins)}" if e.start == today else _label(e.start, today)
        head.append(f"Next timed event: {_clean_title(e)} at {_clock(e.t0)} ({when})")
    facts = []
    for k, word in ((0, "today"), (1, "tomorrow")):
        d = today + timedelta(days=k)
        timed = sorted((e for e in events if not e.all_day and e.t0 and e.start <= d <= e.end and e.start == d),
                       key=lambda e: e.t0)
        allday = [_clean_title(e) for e in events if e.all_day and e.start <= d <= e.end]
        if timed:
            first = timed[0]
            facts.append(f"first {word}: {_clean_title(first)} {_clock(first.t0)}"
                         + (f" (after the all-day {', '.join(allday)})" if allday else ""))
            pairs = []
            for i, a in enumerate(timed):
                for b in timed[i + 1:]:
                    a_end = a.t1 or a.t0
                    if b.t0 < a_end:
                        pairs.append(f"{_clean_title(a)} {_times(a)} and {_clean_title(b)} {_times(b)}")
            # "conflicts" is the person's word (Box, 10-06: the 8B read "overlaps" past a "conflicts" question)
            facts.append(f"conflicts/overlaps {word}: " + ("; ".join(pairs) if pairs else "none"))
        elif allday:
            facts.append(f"{word}: all-day only ({', '.join(allday)})")
    seen = set()
    for e in sorted((e for e in events if _REPEATS_RX.search(e.line) and e.t0), key=lambda e: (e.start, e.t0)):
        if e.start < today or (e.start == today and e.t0 <= t) or _clean_title(e) in seen:
            continue
        seen.add(_clean_title(e))
        rule = _REPEATS_RX.search(e.line).group(1).strip()
        facts.append(f"next {_clean_title(e)}: {_label(e.start, today)} {_clock(e.t0)} (repeats {rule})")
    return head, facts


_CONFLICT_Q = re.compile(r"\b(conflict|overlap|clash|double[- ]?book|same time|at once)", re.I)
_FIRST_Q = re.compile(r"\b(first|earliest|start (?:my|the) day)\b", re.I)
_NEXT_Q = re.compile(r"\b(next|how often|repeat|every|how long until|coming up)\b", re.I)
_DAY_Q = (("tomorrow", re.compile(r"\btomorrow\b", re.I)), ("today", re.compile(r"\b(today|tonight|this (?:morning|afternoon|evening))\b", re.I)))


_DAYLOOK_Q = re.compile(r"\b(anything|what'?s on|what is on|busy|free|plans|on my calendar|have on)\b", re.I)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _day_line(question: str, lines: list[str]) -> str:
    """"Do I have anything on Sunday?" → that day's own line from the digest ("Sun Oct 4: nothing"): the nearest such
    day from today on, never yesterday. Box's B2 arm (10-06): the 8B answered an empty day with other days' events."""
    q = question.lower()
    want = next((w[:3].capitalize() for w in _WEEKDAYS if re.search(rf"\b{w}\b", q)), None)
    rel = "Tomorrow" if re.search(r"\btomorrow\b", q) else ("Today" if re.search(r"\b(today|tonight)\b", q) else None)
    if not (want or rel):
        return ""
    for l in lines:
        if not l.startswith("- ") or l.startswith("- Yesterday") or ": " not in l:
            continue
        label = l[2:].split(": ", 1)[0]
        if (rel and label.startswith(rel)) or (want and not rel and re.search(rf"\b{want}\b", label)):
            return l[2:]
    return ""


_FREE_Q = re.compile(r"\b(free|busy|available|booked)\b", re.I)
_AT_Q = re.compile(r"\bat (\d{1,2})(?::(\d\d))?\s*([ap])\.?m\b", re.I)
_SPAN = re.compile(r"(\d{1,2})(?::(\d\d))? ([AP])M(?:–(\d{1,2})(?::(\d\d))? ([AP])M)? (.+)")


def _mins(h: str, m: str | None, ap: str) -> int:
    hh = int(h) % 12 + (12 if ap.upper() == "P" else 0)
    return hh * 60 + int(m or 0)


def _free_at(question: str, lines: list[str]) -> str:
    """"Am I free tomorrow at 3:30 PM?" → the verdict in code, from that day's own line: busy with what overlaps that
    minute, or free. #1064 (Box's option 3): the 8B said "free at 3:30" with Piano 3–4 and soccer 3:30–4:30 in its
    digest, three nights running. All-day items take no time, as the digest says."""
    q = question or ""
    m = _AT_Q.search(q)
    if not (m and _FREE_Q.search(q)):
        return ""
    day = _day_line(q, lines)
    if not day:
        return ""
    label, _, rest = day.partition(": ")
    t = _mins(m.group(1), m.group(2), m.group(3))
    hits = []
    for part in rest.split("; "):
        if "doesn't take up any time" in part:
            continue
        e = _SPAN.match(part.strip())
        if not e:
            continue
        a = _mins(e.group(1), e.group(2), e.group(3))
        b = _mins(e.group(4), e.group(5), e.group(6)) if e.group(4) else a + 1
        if a <= t < b:
            hits.append(part.strip())
    when = f"{label} at {m.group(1)}{':' + m.group(2) if m.group(2) else ''} {m.group(3).upper()}M"
    return f"{when}: busy ({'; '.join(hits)})" if hits else f"{when}: free (nothing timed then)"


def answer_line(question: str, digest: str) -> str:
    """#1064 (Box's option 2): the facts this question asks for, as one line to go right after it. A local model
    follows the last lines best, and the 8B answered "no conflicts" with the overlap in its digest. Only facts the
    digest already holds (code-made), picked by the question's own words; "" when none fits."""
    lines = (digest or "").splitlines()
    facts = next((l[len("Facts: "):].split(" · ") for l in lines if l.startswith("Facts: ")), [])
    head = lines[1].split(" · ") if len(lines) > 1 and lines[1].startswith("Now:") else []
    q = question or ""
    days = [w for w, rx in _DAY_Q if rx.search(q)]

    def on_day(f: str) -> bool:
        return not days or any(f.split(":", 1)[0].endswith(" " + d) for d in days)
    picked: list[str] = []
    if _CONFLICT_Q.search(q):
        picked += [f for f in facts if f.startswith("conflicts/overlaps ") and on_day(f)]
    if _FIRST_Q.search(q):
        picked += [f for f in facts if f.startswith("first ") and on_day(f)]
    if _NEXT_Q.search(q):
        nexts = [f for f in facts if f.startswith("next ")]
        words = {w for w in re.findall(r"[a-z]{3,}", q.lower())}
        named = [f for f in nexts if words & set(re.findall(r"[a-z]{3,}", f.split(":", 1)[0].lower()[5:]))]
        picked += named or [h for h in head if h.startswith("Next timed event")]
    if not picked:
        fa = _free_at(q, lines)
        if fa:
            picked.append(fa)
    if not picked and _DAYLOOK_Q.search(q):
        d = _day_line(q, lines)
        if d:
            picked.append(d)
    picked = list(dict.fromkeys(picked))
    # read just now, it outranks a saved note (#1230: the 8B gave an older note's "every other week, Oct 28" over the
    # calendar's weekly Monday)
    return ("[From the calendar above, read just now (it outranks any saved note), for this question: "
            + "; ".join(picked) + "]") if picked else ""


TIMELINE_Q = re.compile(r"\b(?:timeline|most recent (?:thing|entry|event)|latest (?:thing|entry|event)|"
                        r"when did (?:this|that|it) happen)\b", re.I)
_QUOTED = re.compile(r"[\u201c\"]([^\u201d\"]{6,})[\u201d\"]")


def wants_timeline(question: str) -> bool:
    return bool(TIMELINE_Q.search(question or ""))


def timeline_about(question: str) -> str:
    """The entry a question quotes ("When did this happen: \u201c…\u201d?"), or ""."""
    m = _QUOTED.search(question or "")
    return m.group(1).strip() if m else ""


def timeline_answer_line(question: str, listing: str) -> str:
    """#1230: the entry the question asks for, from titan's own timeline read (newest first): the quoted one when the
    question quotes one (the listing was read about it), else the newest. Only rows the listing holds."""
    rows = [l[2:] for l in (listing or "").splitlines() if l.startswith("- ")]
    if not rows:
        return ""
    return "[From the household timeline above, for this question: " + rows[0] + "]"


def reminders_answer_line(question: str, digest: str) -> str:
    """#1064: a reminders question that names today or tomorrow gets that day's due line once more, right after it."""
    q = question or ""
    days = [w for w, rx in _DAY_Q if rx.search(q)]
    picked = [l[2:] for l in (digest or "").splitlines() if any(l.startswith(f"- Due {d} (") for d in days)]
    return ("[From the reminders above, for this question: " + "; ".join(picked) + "]") if picked else ""


def reminders_digest(text: str, now: datetime) -> str:
    rs = cc.parse_reminders(text, now.date())
    if not rs:
        if re.search(r"^•", text or "", re.M):
            return "[REMINDERS, read just now]\n" + text.strip()   # lines it can't read, handed over as they are
        if not re.match(r"\s*No (?:incomplete )?reminders", text or ""):
            return ""   # an error or a refusal, never a false "none"
    late = [r for r in rs if r.due and (r.due < now if r.timed else r.due.date() < now.date())]
    soon = sorted((r for r in rs if r.due and r not in late), key=lambda r: (r.due.date(), r.due.time() if r.timed else datetime.max.time()))
    undated = [r for r in rs if not r.due]

    def show(r):
        return r.line.lstrip("• ")
    # #1064 (Box's B2 arm): which are due today and tomorrow, said outright; the model read "Next" as tomorrow
    today, tomorrow = now.date(), now.date() + timedelta(days=1)
    days_ = "".join(f"- Due {w} ({_label(d, today).split('(')[-1].rstrip(')')}): "
                    f"{'; '.join(show(r) for r in rs if r.due and r.due.date() == d) or 'none'}\n"
                    for w, d in (("today", today), ("tomorrow", tomorrow)))
    return ("[REMINDERS, read just now]\n" + days_
            + f"- Overdue: {'; '.join(show(r) for r in late) or 'none'}\n"
            + f"- Next: {show(soon[0]) if soon else 'none'}\n"
            + (f"- Later: {'; '.join(show(r) for r in soon[1:4])}\n" if len(soon) > 1 else "")
            + (f"- No due date: {'; '.join(show(r) for r in undated[:4])}" if undated else "")).rstrip()
