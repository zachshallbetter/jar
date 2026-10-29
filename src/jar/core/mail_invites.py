#!/usr/bin/env python3
"""Meeting invites, reschedules and cancels in synced mail, read by rule, never by a model.

mail_suggestions asks this first. A mail is an invite when it has one of these forms:
  - a calendar part (BEGIN:VEVENT), when the connector carries one (the Mac's MailCollector sends the plain
    text only, so today this is server mail or an invite pasted into a forward);
  - a calendar system's subject: "Invitation:", "Updated invitation:", "Canceled event:", "Canceled:" /
    "Cancelled:" (FW:/Fwd: in front is fine);
  - an Outlook "When:" line with a date in it (an invite, or one forwarded with its "When:" line kept);
  - "Meeting with <Name> <a day> <a time>" in the subject or body.
A response ("Accepted:", "Declined:", "Tentative:"), a list or marketing mail, and a newsletter or webinar are
never invites, whatever they say.

read() gives the fields mail_suggestions.check() validates: kind (add, update or cancel), title, date, start, end
(in the zone the mail names), tz, the invite's UID when the calendar part has one, and all_day. A day given as
"tomorrow" or "Thursday" is resolved against the mail's own Date header, never the wall clock. When anything is
unclear (no time, a day that could be this week or next, a weekday that disagrees with the date, an unknown
zone), the field is left out, and check() gives no card: an invite never gets a made-up date.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any

KINDS = ("add", "update", "cancel")

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
           "nov": 11, "dec": 12}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MON_RX = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_WD_RX = (r"(?:(?P<wd>mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|"
          r"sat(?:urday)?|sun(?:day)?)\.?,?\s+)?")
_DATE_MDY = re.compile(rf"(?i)\b{_WD_RX}{_MON_RX}\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(?P<year>\d{{4}})\b)?")
_DATE_DMY = re.compile(rf"(?i)\b{_WD_RX}(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON_RX}\b(?:,?\s+(?P<year>\d{{4}})\b)?")
_DATE_ISO = re.compile(r"\b(?P<year>\d{4})-(?P<m>\d{2})-(?P<day>\d{2})\b")
_DATE_US = re.compile(rf"(?i)(?:\b{_WD_RX})?(?<![\d/])(?P<m>\d{{1,2}})/(?P<day>\d{{1,2}})(?:/(?P<year>\d{{4}}|\d{{2}}))?(?![\d/])")
_RELATIVE = re.compile(r"(?i)\b(?:(?P<pre>this|next|on)\s+)?(?P<word>today|tonight|tomorrow|" + "|".join(_WEEKDAYS) + r")\b")

# A time: "11:00 AM", "11am", "3 p.m.", "15:30", "noon".
_TIME = re.compile(r"(?i)\b(?P<noon>noon|midday)\b|(?<![\d:/])(?P<h>\d{1,2})(?::(?P<m>[0-5]\d))?\s*"
                   r"(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?)?(?![\d:/])")
_RANGE_SEP = re.compile(r"^\s*(?:-|–|—|to|until|till|through)\s*", re.I)

# The calendar system's own subjects. Responses to the reader's invites are not invites.
_PREFIX = re.compile(r"(?i)^\s*(?P<pfx>updated invitation(?: with note)?|invitation(?: with note)?|"
                     r"cancell?ed(?: event)?(?: with note)?|updated(?: event)?|new event)\s*:\s*(?P<rest>.+)$")
_FORWARD = re.compile(r"(?i)^\s*(?:fwd?|fw|re)\s*:\s*")
_RESPONSE = re.compile(r"(?i)^\s*(?:(?:fwd?|fw|re)\s*:\s*)*(?:accepted|declined|tentative(?:ly accepted)?|"
                       r"new time proposed)\b")
_WHEN = re.compile(r"(?i)\bwhen:\s*")
_FIELD_END = re.compile(r"(?i)\n|\b(?:where|location|organizer|attendees|who|join|joining|description|calendar|"
                        r"video call|meeting link|meeting id|passcode|guests|view all guest info)\s*:")
_MEETING_WITH = re.compile(r"(?i)\bmeeting with\s+")
_NAME_TOKEN = re.compile(r"[A-Z][\w'’.-]*")
_NOT_NAME = re.compile(r"(?i)^(?:on|at|this|next|today|tonight|tomorrow|" + "|".join(_WEEKDAYS) + r"|" +
                       r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|"
                       r"september|october|november|december|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\.?$")
_NOT_INVITE = re.compile(r"(?i)\b(?:newsletter|digest|webinar|weekly (?:update|summary)|unsubscribe from our|"
                         r"meeting (?:notes|minutes|recap|summary|report)s?)\b")
_CANCEL_BODY = re.compile(r"(?i)\b(?:this (?:event|meeting) has been cancell?ed|has been cancell?ed|"
                          r"cancell?ed this (?:event|meeting))\b")

# Zone names first (Outlook writes "(UTC-07:00) Mountain Time (US & Canada)" all year, and means Denver), then
# abbreviations, then a bare offset.
_TZ_NAMES = [(re.compile(r"(?i)\barizona\b"), "America/Phoenix"),
             (re.compile(r"(?i)\bpacific\b"), "America/Los_Angeles"),
             (re.compile(r"(?i)\bmountain\b"), "America/Denver"),
             (re.compile(r"(?i)\bcentral\b"), "America/Chicago"),
             (re.compile(r"(?i)\beastern\b"), "America/New_York"),
             (re.compile(r"(?i)\balaska\b"), "America/Anchorage"),
             (re.compile(r"(?i)\bhawaii\b"), "Pacific/Honolulu")]
_TZ_ABBR = {"PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
            "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
            "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
            "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
            "AKST": "America/Anchorage", "AKDT": "America/Anchorage", "HST": "Pacific/Honolulu",
            "UTC": "UTC", "GMT": "UTC", "Z": "UTC"}
_TZ_ABBR_RX = re.compile(r"\b(" + "|".join(sorted((k for k in _TZ_ABBR if k != "Z"), key=len, reverse=True)) + r")\b")
_TZ_IANA_RX = re.compile(r"\b((?:America|Europe|Asia|Pacific|Australia|Africa)/[A-Za-z_]+)\b")
_TZ_OFFSET_RX = re.compile(r"(?i)\b(?:utc|gmt)\s*(?P<sign>[+\-−])\s*(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?")


# ── the mail's own parts ─────────────────────────────────────────────────────────────────────────

def body_of(text: str) -> str:
    """The text after the header block (the Mac's excerpt: header lines, a blank line, then the body)."""
    t = str(text or "")
    head, sep, rest = t.partition("\n\n")
    if sep and re.match(r"(?i)^(?:from|to|subject|date):", head):
        return rest
    return t


def subject_of(title: str, text: str) -> str:
    m = re.search(r"(?im)^subject:\s*(.+)$", re.split(r"\n\s*\n", str(text or ""), 1)[0])
    return " ".join((m.group(1) if m else str(title or "")).split())


def sent_at(text: str, tz: tzinfo | None = None) -> datetime | None:
    """The mail's own Date header, in the household's zone (naive): the Mac writes "Thu, Oct 8, 2026, 9:14 AM" in
    its own zone; server mail "Thu, 8 Oct 2026 09:14:00 -0700". None when there's none."""
    head = re.split(r"\n\s*\n", str(text or ""), 1)[0]
    m = re.search(r"(?im)^date:\s*(.+)$", head)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(raw)
        if d is not None and d.tzinfo is not None:
            return d.astimezone(tz).replace(tzinfo=None) if tz else d.replace(tzinfo=None)
        if d is not None:
            return d
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%a, %b %d, %Y, %I:%M %p", "%a, %b %d, %Y", "%A, %B %d, %Y %I:%M %p", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


# ── detection ────────────────────────────────────────────────────────────────────────────────────

def _list_mail(text: str) -> bool:
    import promo_mail
    w = promo_mail.why(text)
    return w in ("list-unsubscribe", "precedence", "sender") or promo_mail.is_marketing(text)


def _clean_subject(subject: str) -> str:
    s = subject
    while _FORWARD.match(s):
        s = _FORWARD.sub("", s, count=1)
    return s.strip()


def form_of(title: str, text: str) -> str | None:
    """Which invite form this mail has ("ics", "subject", "when", "meeting-with"), or None."""
    subject = subject_of(title, text)
    if _RESPONSE.match(subject) or _NOT_INVITE.search(subject) or _list_mail(text):
        return None
    body = body_of(text)
    if "BEGIN:VEVENT" in body:
        return "ics"
    if _PREFIX.match(_clean_subject(subject)):
        return "subject"
    for m in _WHEN.finditer(body):
        if _has_day(_field(body, m.end())):
            return "when"
    import promo_mail
    if promo_mail.why(text) is not None:
        return None                               # "meeting with" in a mail with a newsletter's footer: not an invite
    for src in (subject, body):
        for m in _MEETING_WITH.finditer(src):
            if _name_after(src, m.end()) and _first_time(src[m.end():m.end() + 140])[0] is not None:
                return "meeting-with"
    return None


def _has_day(s: str) -> bool:
    return any(rx.search(s) for rx in (_DATE_MDY, _DATE_DMY, _DATE_ISO, _DATE_US, _RELATIVE))


def detect(title: str, text: str) -> bool:
    return form_of(title, text) is not None


# ── reading the fields ───────────────────────────────────────────────────────────────────────────

def _field(body: str, at: int) -> str:
    rest = body[at:at + 220]
    m = _FIELD_END.search(rest)
    return rest[:m.start()] if m else rest


def _name_after(src: str, at: int) -> str:
    out = []
    for tok in src[at:at + 60].split():
        tok = tok.strip(",.;:!?()")
        if not tok or not _NAME_TOKEN.fullmatch(tok) or _NOT_NAME.match(tok) or len(out) == 3:
            break
        out.append(tok)
    return " ".join(out)


def _year_for(m: int, d: int, ref: date) -> int | None:
    try:
        cand = date(ref.year, m, d)
    except ValueError:
        return None
    return ref.year + 1 if cand < ref - timedelta(days=60) else ref.year


def _find_date(s: str, ref: date | None) -> tuple[date | None, tuple[int, int] | None, bool]:
    """(the date, its span, unsure). An explicit date first; a weekday that disagrees with it is unsure."""
    for rx in (_DATE_MDY, _DATE_DMY, _DATE_ISO, _DATE_US):
        m = rx.search(s)
        if not m:
            continue
        g = m.groupdict()
        mon = _MONTHS[g["mon"].lower()[:3]] if g.get("mon") else (int(g["m"]) if g.get("m") else None)
        try:
            day = int(g["day"])
        except (TypeError, ValueError):
            continue
        if not mon:
            continue
        y = g.get("year")
        if y:
            year = int(y) + (2000 if len(y) == 2 else 0)
        elif ref is not None:
            year = _year_for(mon, day, ref)
            if year is None:
                continue
        else:
            return None, m.span(), True       # a date with no year and no Date header to place it
        try:
            d = date(year, mon, day)
        except ValueError:
            continue
        wd = g.get("wd")
        if wd and [w[:3] for w in _WEEKDAYS].index(wd.lower()[:3]) != d.weekday():
            return None, m.span(), True
        return d, m.span(), False
    return None, None, False


def resolve_relative(word: str, pre: str | None, ref: date | None) -> date | None:
    """"tomorrow", "Thursday", "this Friday" against the mail's own date. Unsure → None: no Date header; "next
    Thursday" (this week's or the one after?); a bare weekday that is the mail's own day (today or a week on?)."""
    if ref is None:
        return None
    w = word.lower()
    if w in ("today", "tonight"):
        return ref
    if w == "tomorrow":
        return ref + timedelta(days=1)
    if (pre or "").lower() == "next":
        return None
    delta = (_WEEKDAYS.index(w) - ref.weekday()) % 7
    return ref + timedelta(days=delta) if delta else None


def _relative_date(s: str, ref: date | None) -> tuple[date | None, tuple[int, int] | None, bool]:
    m = _RELATIVE.search(s)
    if not m:
        return None, None, False
    d = resolve_relative(m.group("word"), m.group("pre"), ref)
    return d, m.span(), d is None


def _hm(m: re.Match) -> tuple[int, int, str | None] | None:
    if m.group("noon"):
        return 12, 0, "p"
    h, mi = int(m.group("h")), int(m.group("m") or 0)
    ap = (m.group("ap") or "").lower()[:1] or None
    if h > 23 or (ap and not 1 <= h <= 12):
        return None
    return h, mi, ap


def _to24(h: int, ap: str | None) -> int:
    if ap == "p":
        return h % 12 + 12
    if ap == "a":
        return h % 12
    return h


def _first_time(s: str, allow_24h: bool = False) -> tuple[tuple[str, str | None] | None, int]:
    """((start "HH:MM", end "HH:MM" or None), where it ended) of the first time or time range in s."""
    for m in _TIME.finditer(s):
        t = _hm(m)
        if t is None:
            continue
        h, mi, ap = t
        rest = s[m.end():]
        sep = _RANGE_SEP.match(rest)
        end = None
        stop = m.end()
        if sep:
            m2 = _TIME.match(rest, sep.end())
            t2 = _hm(m2) if m2 else None
            if t2 is not None:
                end, stop = t2, m.end() + m2.end()
        if ap is None and end is not None and end[2] is not None:
            ap = end[2]
            if _to24(h, ap) > _to24(end[0], end[2]) and ap == "p":
                ap = "a"                          # "11-12pm": 11 AM
        if ap is None and not m.group("noon"):
            if not (m.group("m") and allow_24h) and not (m.group("m") and h >= 13):
                continue                          # "at 3", "15" (a day): not a time
        start = f"{_to24(h, ap):02d}:{mi:02d}"
        e = None
        if end is not None:
            eh, em, eap = end
            if eap is None and ap is not None and end[0] <= 12:
                eap = ap if _to24(end[0], ap) > _to24(h, ap) else ("p" if ap == "a" else "a")
            if eap is not None or allow_24h or eh >= 13:
                e = f"{_to24(eh, eap):02d}:{em:02d}"
        return (start, e), stop
    return None, 0


def find_tz(s: str) -> str | None:
    """The zone a time is given in, as an IANA name or "UTC±HH:MM", or None (the household's)."""
    m = _TZ_IANA_RX.search(s)
    if m and zone_of(m.group(1)) is not None:
        return m.group(1)
    for rx, name in _TZ_NAMES:
        if rx.search(s):
            return name
    m = _TZ_OFFSET_RX.search(s)
    if m:
        sign = "-" if m.group("sign") in "-−" else "+"
        return f"UTC{sign}{int(m.group('h')):02d}:{m.group('m') or '00'}"
    m = _TZ_ABBR_RX.search(s)
    if m:
        return _TZ_ABBR[m.group(1)]
    return None


def zone_of(label: str | None) -> tzinfo | None:
    """A find_tz label as a zone; None when it isn't one."""
    if not label:
        return None
    m = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", label)
    if m:
        off = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        if off > timedelta(hours=14) or int(m.group(3)) >= 60:
            return None                           # no zone is that far from UTC
        return timezone(off if m.group(1) == "+" else -off)
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(label)
    except Exception:  # noqa: BLE001 - an unknown name is no zone
        return None


def _when_fields(s: str, ref: date | None, allow_24h: bool) -> dict[str, Any] | None:
    """date/start/end/tz from one "when" string. None when the day is unsure."""
    d, span, unsure = _find_date(s, ref)
    if unsure:
        return None
    if d is None:
        d, span, unsure = _relative_date(s, ref)
        if d is None:
            return None
    rest = s[:span[0]] + " " * (span[1] - span[0]) + s[span[1]:]
    out: dict[str, Any] = {"date": d.isoformat()}
    if re.search(r"(?i)\ball[- ]day\b", s):
        out["all_day"] = True
        return out
    t, stop = _first_time(rest, allow_24h=allow_24h)
    if t is not None:
        out["start"] = t[0]
        if t[1]:
            out["end"] = t[1]
        tz = find_tz(rest[stop:stop + 90]) or find_tz(rest[max(0, stop - 90):stop + 90])
        if tz:
            out["tz"] = tz
    return out


def _ics(body: str) -> dict[str, Any] | None:
    text = re.sub(r"\r?\n[ \t]", "", body)
    m = re.search(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.S)
    if not m:
        return None
    props: dict[str, tuple[str, str]] = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if not sep:
            continue
        name, _, params = k.partition(";")
        props.setdefault(name.upper().strip(), (params, v.strip()))
    method = re.search(r"(?m)^METHOD:(\w+)", text)
    out: dict[str, Any] = {"title": props.get("SUMMARY", ("", ""))[1].replace("\\,", ",").replace("\\;", ";")}
    if props.get("UID"):
        out["uid"] = props["UID"][1]
    cancel = (method and method.group(1).upper() == "CANCEL") or props.get("STATUS", ("", ""))[1].upper() == "CANCELLED"
    seq = props.get("SEQUENCE", ("", "0"))[1]
    out["kind"] = "cancel" if cancel else ("update" if seq.isdigit() and int(seq) > 0 else "add")
    for key, field in (("DTSTART", "start"), ("DTEND", "end")):
        if key not in props:
            continue
        params, v = props[key]
        mm = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})\d{2}(Z)?)?", v)
        if not mm:
            continue
        if field == "start":
            out["date"] = f"{mm.group(1)}-{mm.group(2)}-{mm.group(3)}"
        if mm.group(4) is None:
            if field == "start":
                out["all_day"] = True
            continue
        if field == "end" and f"{mm.group(1)}-{mm.group(2)}-{mm.group(3)}" != out.get("date"):
            continue
        out[field] = f"{mm.group(4)}:{mm.group(5)}"
        tzid = re.search(r"(?i)TZID=\"?([^;:\"]+)", params)
        if field == "start":
            if mm.group(6):
                out["tz"] = "UTC"
            elif tzid:
                label = tzid.group(1).strip()
                out["tz"] = label if zone_of(label) else (find_tz(label) or label)
    return out


def read(title: str, text: str, sent: datetime | None = None) -> dict[str, Any] | None:
    """The invite's fields, unchecked (mail_suggestions.check() keeps only what the mail shows). None: not an
    invite. A dict without "date" is an invite whose day or time couldn't be read: no card."""
    form = form_of(title, text)
    if form is None:
        return None
    ref = sent.date() if sent else None
    subject = subject_of(title, text)
    clean = _clean_subject(subject)
    body = body_of(text)
    out: dict[str, Any] = {"form": form, "kind": "add"}
    pm = _PREFIX.match(clean)
    if pm:
        p = pm.group("pfx").lower()
        out["kind"] = "cancel" if p.startswith("cancel") else ("update" if p.startswith("updated") else "add")
        clean = pm.group("rest").strip()
    elif _CANCEL_BODY.search(body):
        out["kind"] = "cancel"
    if form == "ics":
        got = _ics(body) or {}
        if pm and got.get("kind") == "add":
            got["kind"] = out["kind"]
        out.update({k: v for k, v in got.items() if v not in ("", None)})
        if not out.get("title"):
            out["title"] = clean.split(" @ ", 1)[0]
        return out
    when = None
    if " @ " in clean:                       # Google: "Title @ Thu Oct 15, 2026 11am - 12pm (MDT) (who@…)"
        clean, when = clean.split(" @ ", 1)
        when = re.sub(r"\([^)]*@[^)]*\)\s*$", "", when)
    title_out = re.sub(r"\s*\([^)]*@[^)]*\)\s*$", "", clean).strip()
    allow_24h = form in ("when", "subject")
    if when is None:
        m = _WHEN.search(body)
        if m:
            when = _field(body, m.end())
    if form == "meeting-with" or (when is None and _MEETING_WITH.search(f"{subject}\n{body}")):
        for src in (subject, body):
            m = _MEETING_WITH.search(src)
            if m and _name_after(src, m.end()):
                name = _name_after(src, m.end())
                if form == "meeting-with":
                    title_out = f"Meeting with {name}"
                if when is None:
                    when = src[m.end() + len(name):m.end() + len(name) + 140]
                    allow_24h = False
                break
    out["title"] = title_out
    if when is None:
        return out
    got = _when_fields(when, ref, allow_24h)
    if got:
        out.update(got)
    return out
