#!/usr/bin/env python3
"""Calendar and reminder suggestions from synced mail, as cards, never silent writes (titan-node#501,
zachshallbetter/jellybean#225; design: docs/extraction-suggestions.md).

Each mail the Mac syncs (connector:mail, the Mac's excerpt; full bodies stay off until #390) that names an
appointment, a booking, a deadline or a pickup is read once by titan's LOCAL model, and code checks what it
says against the mail:
  - the date is a real date, today or later, and the mail's body (not only its header) shows it;
  - a time, when given, appears in the body;
  - the title's words appear in the mail; a place, when given, too;
  - titles and places are redacted (no codes, account or card numbers, #268).
A suggestion that passes becomes a card to the mail's owner only (Zach, 10-02: a mail shared with the
household is suggested to its owner): "Add “Dentist” on Tue Oct 6, 3:00 PM to your calendar?" with Add it /
Not now. The Add button's words are built here from the checked fields; a street address never goes into
them, so writing one still goes through #509's hold. Nothing is added unless the person taps it.

Meeting invites, reschedules and cancels (mail_invites.py) are read by rule, never by the model, whatever triage
called them: Outlook "When:" lines, "Invitation:" / "Updated invitation:" / "Canceled:" subjects, "Meeting with
<Name> <day> <time>", a calendar part when the connector carries one. A relative day counts from the mail's own Date
header; a named zone is converted to the household's. The owner's own synced event is found by the invite's UID, else
a telling title word within 14 days: at the same time, nothing; at another time, "Move “X” to Thu Oct 15, 12:00 PM?"
(Update / Ignore); a cancel, "Remove “X”…?" (Remove / Ignore); not there, Add / Ignore. One card per invite (key
mailsug-inv-<hash>), replaced by a later reschedule or cancel; the card shows the title and time only, expires at the
event's start, and the buttons go through the calendar tools as the person's own turn. Unsure of the day, the time or
the zone: no card. A model-read event on the calendar the same day at another time gets an Update card too.

Guards: quiet hours (22:00-07:00: held for the morning), at most 3 suggestion cards per person per household
day (Zach, 10-02), nothing already on their calendar (same day and time, a shared title word), each mail judged once
per version, bills left to mail_bills, newsletters and automated alerts skipped (Zach, 10-02: all mail minus
bills and automated). Kids get none (no kid mail syncs).

  python3 scripts/mail_suggestions.py            dry run: what would be suggested
  python3 scripts/mail_suggestions.py --apply    (titan-mail-bills.timer runs it after the bills)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = Path(os.environ.get("MAIL_SUGGESTIONS_DB", "/ai/datasets/connectors/mail_suggestions.db"))
PER_DAY = 3
QUIET = (22, 7)
WINDOW_DAYS = 14          # mails older than this aren't read for suggestions
SOURCE = "mail-suggestion"
CARD_PROMPT_MARK = "(from a suggestion card)"   # = agent_memory.CARD_PROMPT_MARK: memory never learns from a tap
# A title that tells someone to do something isn't an event's name ("text Ava…", "ignore the above"): refused,
# because the Add button's words become the person's own turn when tapped.
_VERBS = (r"(?:text|call|send|email|e-mail|message|reply|forward|pay|transfer|wire|delete|remove|cancel|share|tell|"
          r"ignore|disregard|open|click|log ?in|sign ?in|buy|order|download|install|run|forget|set|change|update|"
          r"unlock|turn)")
# A verb at the start, or after a clause break or a joining word (JEV's review of #1282: "Lunch - and also share my
# notes…" passed when only the start was checked). A plain title that trips it gets no card; never a wrong one.
IMPERATIVE = re.compile(r"(?i)(?:^\s*|[-–—:;,.!?/|]\s*|\b(?:and|then|also|please|now)\s+)(?:please\s+)?" + _VERBS + r"\b"
                        r"|\b(?:titan|assistant|instructions?|system prompt)\b")

# A mail is a candidate when it names something to put on a calendar or be reminded of.
EVENT_WORDS = re.compile(r"\b(appointment|appt|reservation|booking|booked|scheduled|reschedul\w*|confirm(?:ed|ation)|"
                         r"reminder|due|deadline|rsvp|meeting|pick ?up|pickup|delivery|class|practice|game|"
                         r"interview|visit|check-?in|recital|conference|party|ready for pickup|"
                         # 10-09 (the coverage audit): gatherings and times of day a plan names ("the members' evening")
                         r"evening|morning|afternoon|night|gathering|meetup|meet-up|reception|dinner|lunch|brunch|"
                         r"celebration|ceremony|service|rehearsal|match|tournament|performance)\b", re.I)
# Newsletters and automated alerts (not booking confirmations, which often come from no-reply addresses).
AUTOMATED = re.compile(r"\b(newsletter|digest|weekly (?:update|summary)|security alert|sign-?in|new login|verify your|"
                       r"password|verification code|your code|deals?|% off|sale|webinar|unsubscribe from our)\b", re.I)
_HEADER = re.compile(r"(?m)^(From|To|Cc|Subject|Date):\s.*$")
INVITE_KINDS = ("add", "update", "cancel")
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


def split_mail(text: str) -> tuple[dict[str, str], str]:
    """(headers, body) of the Mac's excerpt: header lines first, then the body."""
    heads = {m.group(1).lower(): m.group(0).split(":", 1)[1].strip() for m in _HEADER.finditer(text[:1500])}
    body = _HEADER.sub("", text).strip()
    return heads, body


def is_candidate(title: str, text: str) -> bool:
    import mail_bills
    import mail_invites
    if mail_invites.detect(title, text):
        return True                       # an invite, a reschedule or a cancel, by rule: whatever triage said
    heads, body = split_mail(text)
    if mail_bills.is_candidate(title, text):
        return False                      # money: mail_bills files it
    if AUTOMATED.search(title) or AUTOMATED.search(heads.get("from", "")):
        return False
    import promo_mail   # #650: a VALORANT crypto promo became a suggestion
    if promo_mail.is_marketing(text):
        return False
    return bool(EVENT_WORDS.search(f"{title}\n{body}"))


PROMPT = """{now}
Below is one email sent to the reader on {sent}. If it tells the reader about something to put on their calendar
(an appointment, a reservation, a pickup, a class, a deadline) or to be reminded of, extract it. Return JSON only:
  "kind": "event" (happens at a time), "reminder" (something to do by a day), or "none"
  "title": a short title (3-6 words), no codes or numbers
  "date": "YYYY-MM-DD"
  "start": "HH:MM" 24-hour, when a time is given
  "end": "HH:MM", when given
  "place": where, when given (a business name or an address)

SUBJECT: {title}
EMAIL:
{body}"""


def _words(t: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(t).lower()) if len(w) >= 4}


def _date_shown(d: date, body: str, sent: date | None = None) -> bool:
    """The body (not the header) shows this date: "Oct 6", "October 6th", "10/6", "6 October", "Tuesday, Oct 6", a
    calendar part's 20261006, or a relative day ("tomorrow", "Thursday") that is this date counted from the mail's own
    Date header (`sent`), never from today."""
    b = body.lower()
    mon = _MONTHS[d.month - 1]
    day = d.day
    if (re.search(rf"\b{mon[:3]}[a-z]*\.?\s+{day}(?:st|nd|rd|th)?\b", b)
            or re.search(rf"\b{day}(?:st|nd|rd|th)?\s+(?:of\s+)?{mon[:3]}", b)
            or re.search(rf"(?<!\d){d.month}/{day}(?!\d)", b)
            or re.search(rf"\b{d.isoformat()}\b", b)
            or re.search(rf"\b{d:%Y%m%d}(?:t\d|\b)", b)):
        return True
    if sent is None:
        return False
    import mail_invites
    return any(mail_invites.resolve_relative(m.group("word"), m.group("pre"), sent) == d
               for m in mail_invites._RELATIVE.finditer(body))


def _time_shown(hhmm: str, body: str) -> bool:
    h, m = (int(x) for x in hhmm.split(":"))
    h12 = (h % 12) or 12
    b = body.lower()
    if re.search(rf"t{h:02d}{m:02d}\d\d", b):                       # a calendar part's DTSTART
        return True
    if h == 12 and not m and re.search(r"\b(?:noon|midday)\b", b):
        return True
    if m:
        return bool(re.search(rf"(?<!\d){h12}:{m:02d}(?!\d)|(?<!\d){h}:{m:02d}(?!\d)", b))
    return bool(re.search(rf"(?<!\d){h12}(?::00)?\s*(?:[ap]\.?\s?m)|(?<!\d){h}:00(?!\d)|"
                          rf"(?<![\d:]){h12}\s*(?:-|–|—|to)\s*\d{{1,2}}(?::\d\d)?\s*[ap]\.?\s?m", b))


def _clean_title(raw: Any, title: str, body: str) -> str | None:
    import redact
    t = " ".join(redact.redact_personal(str(raw or "")).split())[:60].strip(" .\"'")
    t = re.sub(r"[\"“”\n]", "", t)
    if not t or not (_words(t) & _words(f"{title} {body}")) or IMPERATIVE.search(t):
        return None
    return t


def _check_invite(f: dict[str, Any], title: str, body: str, today: date, sent: date | None) -> dict[str, Any] | None:
    """An invite's fields (mail_invites.read): the day and time must be in the mail (its subject counts: a calendar
    system puts them there), a relative day counted from the mail's Date header, a zone it names converted to the
    household's. Anything unsure: None, no card."""
    import local_time
    import mail_invites
    kind = str(f.get("kind") or "")
    shown = f"{title}\n{body}"
    try:
        d = date.fromisoformat(str(f.get("date") or "")[:10])
    except ValueError:
        return None
    if not _date_shown(d, shown, sent):
        return None
    start, end = (str(f.get(k) or "") for k in ("start", "end"))
    ok = lambda v: re.fullmatch(r"\d{2}:\d{2}", v) and int(v[:2]) < 24 and int(v[3:]) < 60   # noqa: E731
    if start and not (ok(start) and _time_shown(start, shown)):
        return None
    if not start and not (f.get("all_day") or kind == "cancel"):
        return None                       # an invite with no time we can read: no card
    end = end if end and start and ok(end) and _time_shown(end, shown) else ""
    home = local_time.zone()
    label = str(f.get("tz") or "")
    src = mail_invites.zone_of(label) if label else home
    if label and src is None:
        return None                       # a zone we don't know: the time could be anything
    out: dict[str, Any] = {"kind": kind, "date": d.isoformat()}
    if start:
        at = datetime.combine(d, datetime.strptime(start, "%H:%M").time())
        if src is not None and home is not None:
            at = at.replace(tzinfo=src).astimezone(home).replace(tzinfo=None)
        out.update(date=at.date().isoformat(), start=f"{at:%H:%M}")
        if end:
            e = datetime.combine(d, datetime.strptime(end, "%H:%M").time())
            if src is not None and home is not None:
                e = e.replace(tzinfo=src).astimezone(home).replace(tzinfo=None)
            if e > at and e.date() == at.date():
                out["end"] = f"{e:%H:%M}"
        if label:
            out["tz"] = label
    local = date.fromisoformat(out["date"])
    if local < today or local > today + timedelta(days=366):
        return None
    t = _clean_title(f.get("title"), title, body)
    if not t:
        return None
    out["title"] = t
    uid = re.sub(r"[^\w@.\-]", "", str(f.get("uid") or ""))[:200]
    if uid:
        out["uid"] = uid
    return out


def check(f: dict[str, Any], title: str, body: str, today: date, sent: date | None = None) -> dict[str, Any] | None:
    """The model's (or, for an invite, mail_invites.read's) fields, kept only when the mail shows them. None: nothing
    to suggest. `sent`: the mail's own date, for a relative day ("tomorrow", "Thursday")."""
    import redact
    kind = str(f.get("kind") or "").lower()
    if kind in INVITE_KINDS:
        return _check_invite({**f, "kind": kind}, title, body, today, sent)
    if kind not in ("event", "reminder"):
        return None
    try:
        d = date.fromisoformat(str(f.get("date") or "")[:10])
    except ValueError:
        return None
    if d < today or d > today + timedelta(days=366) or not _date_shown(d, body, sent):
        return None
    t = _clean_title(f.get("title"), title, body)
    if not t:
        return None
    out: dict[str, Any] = {"kind": kind, "title": t, "date": d.isoformat()}
    for k in ("start", "end"):
        v = str(f.get(k) or "")
        if re.fullmatch(r"\d{1,2}:\d{2}", v) and int(v.split(":")[0]) < 24 and _time_shown(v, body):
            out[k] = f"{int(v.split(':')[0]):02d}:{v.split(':')[1]}"
    place = " ".join(redact.redact_personal(str(f.get("place") or "")).split())[:80].strip(" .")
    if place and _words(place) and _words(place) <= _words(f"{title} {body}") | {"street", "avenue", "road"}:
        out["place"] = re.sub(r"[\"“”\n]", "", place)
    if "end" in out and "start" not in out:
        out.pop("end")
    return out


def _clock(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{(h % 12) or 12}:{m:02d} {'PM' if h >= 12 else 'AM'}"


def card_for(s: dict[str, Any], subject: str) -> tuple[str, str, list[dict[str, str]]]:
    """(title, detail, buttons), built here from checked fields. The button's words never carry a street
    address: a location with a street number is the person's to add (#509)."""
    d = date.fromisoformat(s["date"])
    when = d.strftime("%a %b ") + str(d.day) + (f", {_clock(s['start'])}" if s.get("start") else "")
    place = s.get("place") or ""
    safe_place = place if place and not re.search(r"\d", place) else ""
    if s["kind"] == "event":
        title = f"Add “{s['title']}” on {when} to your calendar?"
        ask = (f'{CARD_PROMPT_MARK} Create a calendar event {title_span(s["title"])} on {s["date"]}'
               + (f" from {s['start']}" + (f" to {s['end']}" if s.get("end") else "") if s.get("start") else " (all day)")
               + (f" at {safe_place}" if safe_place else "") + ".")
        label = "Add it"
    else:
        title = f"Remind you about “{s['title']}” on {when}?"
        ask = f'{CARD_PROMPT_MARK} Remind me about {title_span(s["title"])} on {s["date"]} at {s.get("start") or "09:00"}.'
        label = "Remind me"
    detail = f"From an email: {subject[:80]}." + (f" Where: {place}." if place else "")
    return title, detail, [{"label": label, "kind": "prompt", "value": ask[:300]},
                                 {"label": "Not now", "kind": "dismiss"}]


def on_calendar(s: dict[str, Any], events: list[dict[str, Any]], owner: str) -> bool:
    """Already there: an event of the owner's on that day sharing a title word."""
    for n in events:
        d = n.get("data") or {}
        prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
        if str(prov.get("owner") or "zach") != owner:
            continue
        if str(d.get("start") or "")[:10] == s["date"] and _words(d.get("title") or "") & _words(s["title"]):
            return True
    return False


MATCH_DAYS = 14   # an invite's event is looked for this many days either side of its new day
# Words every meeting shares: a title match needs one of its other words ("Meeting with Robin" ≠ "Meeting with Leo").
GENERIC = frozenset({"meeting", "meetings", "call", "sync", "appointment", "appt", "event", "invitation", "invite",
                     "updated", "canceled", "cancelled", "session", "catch", "team", "weekly", "daily", "monthly",
                     "planning", "review", "check", "update", "standup", "huddle", "chat", "time", "with", "from"})


def _distinct(t: str) -> set[str]:
    """A title's telling words: 4+ letters, or a short token with a digit ("q4"), minus GENERIC."""
    toks = re.findall(r"[a-z0-9]+", str(t).lower())
    return {w for w in toks if len(w) >= 4 or (len(w) >= 2 and re.search(r"\d", w))} - GENERIC


def _event_owner(d: dict[str, Any]) -> str:
    """The event's owner from its provenance. None recorded: "" (matches no one), never a guess: a card's title must
    come from the owner's own calendar, so an event of unknown ownership is never used."""
    prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
    return str(prov.get("owner") or "")


def _event_uid(d: dict[str, Any]) -> str:
    """A synced event's iCalendar UID: the Mac's source_id is "<uid>@<start>" (SyncModels CalendarMapping)."""
    prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
    sid = str(prov.get("source_id") or "")
    return str(d.get("uid") or d.get("external_id") or (sid.rsplit("@", 1)[0] if "@" in sid else "")).strip().lower()


def _event_local(d: dict[str, Any]) -> datetime | None:
    """The event's start in the household's zone (naive); a start with no zone is the household's already."""
    import local_time
    at = local_time.parse_iso(d.get("start"))
    if at is None:
        return None
    if at.tzinfo is not None:
        z = local_time.zone()
        at = (at.astimezone(z) if z is not None else at.astimezone()).replace(tzinfo=None)
    return at


def _target(s: dict[str, Any]) -> datetime:
    d = date.fromisoformat(s["date"])
    return datetime.combine(d, datetime.strptime(s.get("start") or "00:00", "%H:%M").time())


def find_event(s: dict[str, Any], events: list[dict[str, Any]], owner: str, same_day: bool = False
               ) -> tuple[dict[str, Any], datetime, bool] | None:
    """The owner's own synced event this suggestion is about: → (its data, its local start, matched by UID). By the
    invite's UID first; else a telling title word in common within MATCH_DAYS (same_day: on that day only); the
    nearest wins. Only the owner's events are looked at: nothing on the card comes from anyone else's calendar."""
    target = _target(s)
    want = _distinct(s["title"])
    uid = str(s.get("uid") or "").lower()
    best = None
    for n in events:
        d = n.get("data") or {}
        if _event_owner(d) != owner:
            continue
        at = _event_local(d)
        if at is None:
            continue
        by_uid = bool(uid) and _event_uid(d) == uid
        if not by_uid:
            if not want or not (want & _distinct(d.get("title") or "")):
                continue
            if (at.date() != target.date()) if same_day else abs((at.date() - target.date()).days) > MATCH_DAYS:
                continue
        rank = (0 if by_uid else 1, abs((at - target).total_seconds()))
        if best is None or rank < best[0]:
            best = (rank, d, at, by_uid)
    return (best[1], best[2], best[3]) if best else None


def same_time(s: dict[str, Any], at: datetime) -> bool:
    return at.date().isoformat() == s["date"] and (not s.get("start") or f"{at:%H:%M}" == s["start"])


def _when(day: str, start: str | None = None) -> str:
    d = date.fromisoformat(day)
    return d.strftime("%a %b ") + str(d.day) + (f", {_clock(start)}" if start else "")


# A title in a button's words sits between these (#1282 review): when tapped, the turn wraps the span as untrusted
# data with its own nonce (agent_runtime_lib._card_prompt_as_data), so a sender's words in it are never instructions.
TITLE_OPEN, TITLE_CLOSE = "⟪", "⟫"


def title_span(t: Any) -> str:
    """A title for a button's words: one line, no control characters, the span's own brackets removed."""
    import untrusted
    clean = untrusted.clean_name(str(t or ""), 120).replace(TITLE_OPEN, "").replace(TITLE_CLOSE, "")
    return f"{TITLE_OPEN}{clean}{TITLE_CLOSE}"


def _safe_event_title(t: Any) -> str | None:
    """An event title from the owner's own calendar, for a card and its button (a tap makes it their own turn)."""
    t = re.sub(r"[\"“”\n]", "", " ".join(str(t or "").split()))[:60].strip(" .'")
    return t if t and not IMPERATIVE.search(t) else None


def invite_key(owner: str, s: dict[str, Any]) -> str:
    """One card per invite and person, so a reschedule or a cancel replaces it: by the UID, else by the title's
    words. Hashed: the key never carries the title."""
    ident = ("uid:" + str(s["uid"]).lower()) if s.get("uid") else ("title:" + " ".join(sorted(_words(s["title"]) or {s["title"].lower()})))
    return "mailsug-inv-" + hashlib.sha1((owner + "\x00" + ident).encode()).hexdigest()[:16]


def change_card(action: str, s: dict[str, Any], ev_title: str = "", at: datetime | None = None
                ) -> tuple[str, str, list[dict[str, str]]]:
    """(title, detail, buttons) for an invite: add, update (move), remove, or withdraw (a pending add was canceled).
    Only the event's title and times go on it. The buttons' words are built here from checked fields and the owner's
    own event; each becomes the person's turn, which writes through the calendar tools (the device asks first)."""
    new = f"{s['date']}T{s['start']}" if s.get("start") else s["date"]
    ignore = {"label": "Ignore", "kind": "dismiss"}
    if action == "add":
        title = f"Add “{s['title']}” on {_when(s['date'], s.get('start'))} to your calendar?"
        ask = (f'{CARD_PROMPT_MARK} Create a calendar event {title_span(s["title"])} on {s["date"]}'
               + (f" from {s['start']}" + (f" to {s['end']}" if s.get("end") else "") if s.get("start") else " (all day)")
               + ".")
        return title, "From a meeting invite in your mail.", [{"label": "Add", "kind": "prompt", "value": ask[:300]},
                                                                    ignore]
    old = f"{at:%Y-%m-%dT%H:%M}" if at else ""
    old_when = _when(at.date().isoformat(), f"{at:%H:%M}") if at else ""
    if action == "update":
        title = f"Move “{ev_title}” to {_when(s['date'], s.get('start'))}?"
        ask = (f'{CARD_PROMPT_MARK} Move my calendar event {title_span(ev_title)} on {old} to start {new}'
               + (f" and end {s['date']}T{s['end']}" if s.get("end") else "") + ".")
        return title, f"An updated invite moves it. On your calendar now: {old_when}.", [
            {"label": "Update", "kind": "prompt", "value": ask[:300]}, ignore]
    if action == "remove":
        title = f"Remove “{ev_title}” on {old_when}?"
        ask = f'{CARD_PROMPT_MARK} Delete my calendar event {title_span(ev_title)} on {old}.'
        return title, "The invite was canceled.", [{"label": "Remove", "kind": "prompt", "value": ask[:300]}, ignore]
    return (f"“{s['title']}” was canceled", "Nothing to add to your calendar.", [ignore])


# Which triaged mail the suggestions pass reads (Coordination, 10-09, from the 10-08/09 coverage audit): everything a
# person reads plus notifications (bookings and appointment confirmations live there); newsletters and marketing only
# with an invite (a calendar part), from a sender the owner has replied to in that thread, or naming a title already
# on the owner's calendar, never a plain promotion.
SUGGEST_ALWAYS = frozenset({"personal", "action", "record", "notification"})
SUGGEST_IF_KNOWN = frozenset({"newsletter", "marketing"})


def worth_suggesting(category: str | None, title: str, *, has_ics: bool = False, sender_known: bool = False,
                     cal_titles: Any = ()) -> bool:
    """Whether a mail of this triage category goes to the suggestions pass (see SUGGEST_ALWAYS / SUGGEST_IF_KNOWN)."""
    if category is None or category in SUGGEST_ALWAYS:   # untriaged mail (triage off) goes as it always did
        return True
    if category in SUGGEST_IF_KNOWN:
        words = _distinct(title or "")
        return has_ics or sender_known or any(words & _distinct(t) for t in cal_titles if t)
    return False


def dropped_worth(sources: list[dict[str, Any]], events: list[dict[str, Any]],
                  known: Callable[[str], set[str]] | None = None, text_of: Callable[[str], str | None] | None = None
                  ) -> list[dict[str, Any]]:
    """The Mac path's dropped mail the suggestions pass should read (worth_suggesting): every notification; a newsletter
    or a promotion only with an invite in it, from a sender the owner has replied to, or naming one of their calendar
    titles."""
    import mail_server
    import mail_triage
    known = known or mail_server.correspondents_of
    text_of = text_of or mail_triage.dropped_text
    seen_known: dict[str, set[str]] = {}
    out = []
    for s in sources:
        owner = str((s.get("metadata") or {}).get("owner") or "")
        cat, title = s.get("triaged"), str(s.get("title") or "")
        if cat in SUGGEST_IF_KNOWN:
            if owner not in seen_known:
                seen_known[owner] = known(owner)
            titles = [str((n.get("data") or {}).get("title") or "") for n in events if _event_owner(n.get("data") or {}) == owner]
            if not worth_suggesting(cat, title, has_ics=mail_triage.is_invite(title, text_of(s["source_id"]) or ""),
                                    sender_known=mail_server.address_of(s.get("sender")) in seen_known[owner],
                                    cal_titles=titles):
                continue
        elif not worth_suggesting(cat, title):
            continue
        out.append(s)
    return out


def _db(path: Path | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(path or DB)
    con.execute("CREATE TABLE IF NOT EXISTS seen (mail_id TEXT PRIMARY KEY, updated_at TEXT, outcome TEXT, ts REAL)")
    con.execute("CREATE TABLE IF NOT EXISTS cards (key TEXT PRIMARY KEY, mail_id TEXT, owner TEXT, title TEXT, "
                "detail TEXT, actions TEXT, state TEXT NOT NULL, day TEXT, ts REAL)")
    have = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
    # expires: household-local ISO, the event's start. event, target: the calendar event a change card is about (its
    # titan id) and the change ("YYYY-MM-DDTHH:MM", or "cancel"), so a text's signal and a mail invite proposing the
    # same change make one card (event_signals.py).
    for col in ("expires", "event", "target"):
        if col not in have:
            try:
                con.execute(f"ALTER TABLE cards ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass   # another process added it first
    return con


def _store(con: sqlite3.Connection, key: str, sid: str, owner: str, card: tuple[str, str, list[dict[str, str]]],
           expires: str | None, event: str | None = None, target: str | None = None) -> bool:
    """Queue a card under its key; a later one with the same key replaces it (a reschedule updates the card). The
    same card again is a no-op. → whether anything changed."""
    title, detail, actions = card
    row = con.execute("SELECT title, actions, state FROM cards WHERE key=?", (key,)).fetchone()
    if row and row[0] == title and row[1] == json.dumps(actions) and row[2] in ("held", "sent"):
        return False
    con.execute("INSERT OR REPLACE INTO cards (key, mail_id, owner, title, detail, actions, state, day, ts, expires, "
                "event, target) VALUES (?,?,?,?,?,?,'held',NULL,?,?,?,?)",
                (key[:80], sid, owner, title, detail, json.dumps(actions), time.time(), expires, event, target))
    return True


def _expires(s: dict[str, Any], at: datetime | None = None) -> str:
    """When a card about this stops mattering: the event's start (household time), or the end of an all-day one."""
    if at is not None:
        return f"{at:%Y-%m-%dT%H:%M}"
    return f"{s['date']}T{s['start']}" if s.get("start") else f"{s['date']}T23:59"


def _sent(text: str, meta: dict[str, Any]) -> datetime | None:
    """The mail's own date (its Date header, else when it arrived), household time, naive: relative days count from
    it, never from the wall clock."""
    import local_time
    import mail_invites
    z = local_time.zone()
    at = mail_invites.sent_at(text, z)
    if at is not None:
        return at
    obs = local_time.parse_iso(meta.get("observed_at"))
    if obs is None:
        return None
    if obs.tzinfo is not None:
        obs = (obs.astimezone(z) if z is not None else obs.astimezone()).replace(tzinfo=None)
    return obs


def _invite(s: dict[str, Any], sid: str, owner: str, events: list[dict[str, Any]], con: sqlite3.Connection,
            apply: bool, now: datetime | None = None) -> tuple[str, str | None]:
    """What an invite does: → (outcome, the card's action or None). Matched at the same time: silent. Matched at
    another time: Update. Not found: Add. A cancel of a matched event: Remove; of one never added: the pending Add
    card (if any) says it was canceled."""
    key = invite_key(owner, s)
    found = find_event(s, events, owner)
    have = con.execute("SELECT state FROM cards WHERE key=?", (key,)).fetchone()
    action: str | None
    if s["kind"] == "cancel":
        if found and (found[2] or same_time(s, found[1])):
            action = "remove"
        elif have and have[0] in ("held", "sent"):
            action = "withdraw"
        else:
            return "invite-gone", None
    elif found and same_time(s, found[1]):
        if apply and have and have[0] == "held":
            con.execute("UPDATE cards SET state='withdrawn' WHERE key=?", (key,))   # it got there on its own
        return "invite-same", None
    else:
        action = "update" if found else "add"
    ev_title, at, eid, target = "", None, None, None
    if action in ("update", "remove"):
        ev_title, at = _safe_event_title(found[0].get("title")) or "", found[1]
        if not ev_title:
            return "invite-unsafe", None
        import event_signals
        eid = event_signals.event_id(found[0]) or None
        if now is None:
            import local_time
            now = local_time.now().replace(tzinfo=None)
        target = "cancel" if action == "remove" else (f"{s['date']}T{s['start']}" if s.get("start") else s["date"])
        if eid and con.execute("SELECT 1 FROM cards WHERE key LIKE ? AND owner=? AND event=? AND target=? AND state "
                               "IN ('held','sent') AND (expires IS NULL OR expires > ?)",
                               (event_signals.KEY_PREFIX + "%", owner, eid, target,
                                f"{now:%Y-%m-%dT%H:%M}")).fetchone():
            return "invite-dup-text", None   # a text already proposed this same change to this event: one card
    if apply:
        if action == "withdraw" and have and have[0] == "held":
            con.execute("UPDATE cards SET state='withdrawn' WHERE key=?", (key,))
        else:
            exp = _expires(s, at if action == "remove" else None)
            _store(con, key, sid, owner, change_card(action, s, ev_title, at), exp, event=eid, target=target)
    return "invite-" + action, action


def run(apply: bool, sources: list[dict], text_of: Callable[[str], str], llm: Callable[[str], str],
        events: list[dict[str, Any]], con: sqlite3.Connection, now: datetime, roles: dict[str, str],
        send: Callable[..., Any] | None = None, now_line: str = "", limit: int = 40,
        is_envelope: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """sources: the KB's sources with metadata (mail_bills.with_meta: owner, observed_at). Suggestions are
    queued per mail version; held ones are sent outside quiet hours within the day's budget."""
    today = now.date()
    seen = {r[0]: r[1] for r in con.execute("SELECT mail_id, updated_at FROM seen")}
    stats: dict[str, Any] = {"mails": 0, "candidates": 0, "suggested": [], "sent": 0,
                             "invites": {k: 0 for k in INVITE_KINDS}, "outcomes": {}}
    for src in sources:
        if src.get("source_type") != "connector:mail":
            continue
        stats["mails"] += 1
        sid, stamp = src["source_id"], str(src.get("updated_at") or "")
        meta = src.get("metadata") or {}
        owner = str(meta.get("owner") or "zach")
        sent = str(meta.get("observed_at") or "")[:10]
        if seen.get(sid) == stamp or roles.get(owner) not in ("admin", "adult"):
            continue
        if is_envelope is not None and is_envelope(sid):   # a summary is never read for a suggestion (#390, 10-05)
            if apply:
                import mail_bills
                mail_bills.keep_first_reading(con, 4, sid, stamp)
            continue
        try:
            if sent and date.fromisoformat(sent) < today - timedelta(days=WINDOW_DAYS):
                continue
        except ValueError:
            pass
        title, text = str(src.get("title") or ""), text_of(sid)
        if not is_candidate(title, text):
            if apply:
                con.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?)", (sid, stamp, "not-candidate", time.time()))
            continue
        stats["candidates"] += 1
        if stats["candidates"] > limit:
            break
        heads, body = split_mail(text)
        sent_at = _sent(text, meta)
        sent_day = sent_at.date() if sent_at else None
        import mail_invites
        inv = mail_invites.read(title, text, sent_at)
        outcome = "none"
        if inv is not None:
            # an invite, a reschedule or a cancel: read by rule, no model; unsure → no card
            stats["invites"][inv.get("kind") or "add"] += 1
            s = check(inv, title, mail_invites.body_of(text), today, sent_day)
            if s is None:
                outcome = "invite-unsure"
            else:
                outcome, action = _invite(s, sid, owner, events, con, apply, now)
                if action:
                    stats["suggested"].append({"owner": owner, **s, "kind": action})
        else:
            raw = llm(PROMPT.format(now=now_line, sent=sent or "an unknown day", title=title[:200], body=body[:3000]))
            m = re.search(r"\{.*\}", raw or "", re.S)
            try:
                f = json.loads(m.group(0)) if m else {}
            except ValueError:
                f = {}
            s = check(f, title, body, today, sent_day)
            found = find_event(s, events, owner, same_day=True) if s and s["kind"] == "event" else None
            if s and s["kind"] == "event" and found and same_time(s, found[1]):
                outcome = "on-calendar"
            elif s and s["kind"] == "event" and found and _safe_event_title(found[0].get("title")):
                # the same day, another time: the mail moved it (a same-day change was swallowed before)
                outcome = "suggested-update"
                stats["suggested"].append({"owner": owner, **s, "kind": "update"})
                if apply:
                    _store(con, f"mailsug-{sid}"[:80], sid, owner,
                           change_card("update", s, _safe_event_title(found[0].get("title")) or "", found[1]),
                           _expires(s))
            elif s and s["kind"] == "reminder" and on_calendar(s, events, owner):
                outcome = "on-calendar"
            elif s:
                outcome = "suggested"
                ctitle, detail, actions = card_for(s, title)
                stats["suggested"].append({"owner": owner, **s})
                if apply:
                    _store(con, f"mailsug-{sid}"[:80], sid, owner, (ctitle, detail, actions),
                           _expires(s) if s["kind"] == "event" else None)
        stats["outcomes"][outcome] = stats["outcomes"].get(outcome, 0) + 1
        if apply:
            con.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?)", (sid, stamp, outcome, time.time()))
            con.commit()
    if apply:
        stats["sent"] = send_held(con, now, send)
    return stats


def quiet(now: datetime) -> bool:
    return now.hour >= QUIET[0] or now.hour < QUIET[1]


def send_held(con: sqlite3.Connection, now: datetime, send: Callable[..., Any] | None) -> int:
    """Held suggestions become cards: outside quiet hours, at most PER_DAY per person per household day,
    oldest first. send=None: nothing sent. A card about an event expires at its start (at most a week); one whose
    event has started by now is dropped unsent."""
    if send is None or quiet(now):
        return 0
    day = now.date().isoformat()
    n = 0
    for key, owner, title, detail, actions, expires in con.execute(
            "SELECT key, owner, title, detail, actions, expires FROM cards WHERE state='held' ORDER BY ts").fetchall():
        expires_in = 7 * 86400.0
        if expires:
            try:
                left = (datetime.fromisoformat(expires) - now).total_seconds()
            except ValueError:
                left = expires_in
            if left <= 0:
                con.execute("UPDATE cards SET state='expired' WHERE key=?", (key,))
                continue
            expires_in = max(3600.0, min(expires_in, left))
        used = con.execute("SELECT COUNT(*) FROM cards WHERE owner=? AND day=?", (owner, day)).fetchone()[0]
        if used >= PER_DAY:
            continue
        import event_proposals
        import event_signals
        import meeting_notes
        src = (event_signals.SOURCE if key.startswith(event_signals.KEY_PREFIX) else
               event_proposals.SOURCE if key.startswith(event_proposals.KEY_PREFIX) else
               meeting_notes.SOURCE if key.startswith(meeting_notes.KEY_PREFIX) else SOURCE)
        send(title, detail, kind="suggestion", urgency=1, source=src, to=[owner], key=key,
             actions=json.loads(actions), expires_in=expires_in)
        con.execute("UPDATE cards SET state='sent', day=? WHERE key=?", (day, key))
        n += 1
    con.commit()
    return n


def main_run(apply: bool, limit: int = 40) -> dict[str, Any]:
    import agent_runtime_lib as lib
    import household_identity as hh
    import kb_hygiene
    import mail_bills
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = _db()
    events = lib.record_nodes("appointment")[0]   # every one, paged (#345)
    roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()}
    send = None
    if apply:
        import proactive
        send = proactive.add_card
    now = lib._local_now().replace(tzinfo=None)
    import event_signals
    import hooks
    try:   # texts' reschedules and cancels (event_signals.py): queued here, sent with the rest by run()'s send_held
        sig = event_signals.process(hooks.connect(), events, con, now, roles, apply)
    except Exception as exc:  # noqa: BLE001 - a failed signal pass costs those cards, never the mail's
        sig = {"signals": 0, "outcomes": {"error": 1}}
        print(f"[event_signals] not run ({type(exc).__name__})", file=sys.stderr)
    import event_proposals
    import world
    try:   # plans agreed over texts (event_proposals.py, #1363): an owner-only card each, sent with the rest
        names = {str(p["id"]): str(p.get("name") or "") for p in hh.get_store().people()}
        hcal = next(iter(sorted(world.household_calendars())), None)
        props = event_proposals.process(hooks.connect(), events, con, now, roles, names, hcal, apply)
    except Exception as exc:  # noqa: BLE001 - a failed proposal pass costs those cards, never the mail's
        props = {"proposals": 0, "outcomes": {"error": 1}}
        print(f"[event_proposals] not run ({type(exc).__name__})", file=sys.stderr)
    import item_refs
    import mail_triage
    pending = item_refs.pending_sources("mail")   # #446: new mail kept as references, its text until this reads it
    records = mail_triage.record_sources()        # receipts, orders, statements kept out of the KB (mail_triage.py)
    records += dropped_worth(mail_triage.dropped_sources(), events)   # notifications, and the rest by the rule
    out = run(apply, mail_bills.with_meta(kb_hygiene.list_sources() + pending + records),
              (lambda base: lambda sid: mail_triage.dropped_text(sid) or base(sid))(   # dropped mail the rule let in
                  mail_triage.text_of(item_refs.text_of(kb_hygiene.source_text))),
              mail_bills.local_llm, events, con, now, roles, send=send, now_line=lib._lt.now_line(lib._local_now()),
              limit=limit, is_envelope=mail_bills.enveloped)
    out["text_signals"] = sig
    out["text_proposals"] = props
    if apply and (pending or records):
        done = {r[0] for r in con.execute("SELECT mail_id FROM seen")}
        item_refs.mark("suggestions", [p["source_id"] for p in pending if p["source_id"] in done])
        mail_triage.mark("suggestions", [r["source_id"] for r in records if r["source_id"] in done])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    st = main_run(a.apply, a.limit)
    for s in st["suggested"]:   # the kind, the day and whose: never the title (it's someone's mail)
        print(("  queued  " if a.apply else "  would suggest  ") + f"{s['kind']} on {s['date']} for {s['owner']}")
    print(f"mail suggestions: {st['mails']} mails, {st['candidates']} candidates, {len(st['suggested'])} "
          f"{'queued' if a.apply else 'to suggest'}, {st['sent']} sent{'' if a.apply else ' (dry run)'}")
    print("  invites " + json.dumps(st["invites"]) + "  outcomes " + json.dumps(st["outcomes"], sort_keys=True))
    ts = st.get("text_signals") or {}   # counts only: never a title, a time or whose
    print(f"  text signals {ts.get('signals', 0)}  outcomes " + json.dumps(ts.get("outcomes") or {}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
