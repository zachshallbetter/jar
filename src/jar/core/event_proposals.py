#!/usr/bin/env python3
"""A plan agreed over texts as an owner-only "Add to calendar?" card (#1363, the titan half; the Mac reads its own
texts, jellybean#1156). Found by the 10-09 coverage audit: a one-to-one thread settled a new plan over 21 texts and
titan got nothing, since text hooks need a number and event_signal only moves or cancels an existing event.

The Mac posts one derived, body-free fact per agreed plan (POST /v1/hooks, from the poster's paired device):

  {"kind": "event_proposal", "title": "<≤ 60>", "start": "<ISO-8601 with an offset>" | "<YYYY-MM-DD>",
   "end": "<the same kind>" | null, "place": "<≤ 60>" | null, "participants": ["<household person id>", …], "others": <n>, "confidence": 0.0-1.0,
   "thread_ref": "<16 hex>", "sig_id": "<16 hex>"}

  participants   household members the plan includes (ids, ≤ 10); others: how many people outside the household
  thread_ref     the device's own opaque thread id (sha1, first 16), so a later plan in the same thread is the same one
  sig_id         the proposal's opaque id: the same sig_id again updates it in place (and its card)
  with_name      optional: the other person's first name as the owner's Contacts show it (Coordination, 10-09: the
                 card should say who, so it arrives ready to approve). One word, ≤ 24 characters, letters only: never
                 a handle, number, address or last name (400 for a digit, "@" or "+"; only the first word is kept)

Nothing else is accepted: no text, no sender, no handle (400). Title and place are one
line, without markers or control characters, capped. A shared household screen has no personal items (403).

The suggestions pass (mail_suggestions.main_run, every 30 minutes) turns new proposals into cards:
  - to the poster only, whoever the plan names (a kid's plan naming adults stays the kid's); the people named see
    nothing until the poster adds it, and then only as the calendar they chose shows it;
  - nothing below confidence 0.6, nothing starting in the past or more than 60 days ahead, and no card when the
    poster's calendar already has it (that day, a shared title word);
  - a plan with no time yet (jellybean#1158 sends a bare day, "this weekend" as Sat to Sun) is an all-day card and
    event, "Sat Oct 10" or "Sat Oct 10 – Sun Oct 11", and no Leave by (next_stop skips all-day events). When the same
    sig_id later arrives with a time, the card updates in place: Add with the time, or, if the owner already added
    the all-day event, "Update" moves that event to the time (never a second event);
  - "Alex and you agreed on “<title>”, Sat Oct 10, 3:00 PM. Add to calendar?" ("You agreed…" without with_name),
    with who and where in the detail; buttons Add
    (the poster's own calendar), Add to the household calendar (an adult's, when the household has one), Not now.
    A tap is the poster's own turn through the calendar tools (the device asks first); nothing is written without it.
    Add creates the event WITH the place as its location, so Leave-by picks it up with nothing more (next_stop.py,
    the trip Live Activity): the card shows the place before the tap, so the place in Add's words is the owner's own
    approval (#509 holds a number from text the person never saw). A place that doesn't read as a place (an
    instruction, markers) stays inside a data span instead.
    Key textprop-<sig_id>: an update replaces the card, expiring at the plan's start.
Kept (jellybean#1189): the poster tapping Add, Add to the household calendar or Update on the card marks the proposal
kept (proactive.card_action → mark_kept; only the owner's own row). GET /v1/event_proposals/kept gives the caller's
own kept ones as ids only, {sig_id, thread_ref}, so their Mac can post thread:device hooks for the texts the plan came
from (hooks.put_device). Not now and an expired card mark nothing.
Logs and stats carry counts only.
"""
from __future__ import annotations

import math
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

KIND = "event_proposal"
FIELDS = frozenset({"kind", "title", "start", "end", "place", "participants", "others", "confidence", "thread_ref",
                    "sig_id"})
OPTIONAL = frozenset({"with_name"})
NAME_MAX = 24
NAME_RX = re.compile(r"[^\W\d_][^\W\d_'’-]*(?:['’-][^\W\d_]+)*")
# a place as plain words in a button: letters, digits and an address's punctuation, and nothing that reads as an order
PLACE_RX = re.compile(r"[\w .,#'’&/()-]{1,80}")
HEX16 = re.compile(r"[0-9a-f]{16}")
START_RX = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})")
PID_RX = re.compile(r"[A-Za-z0-9_-]{1,40}")
TITLE_MAX, PLACE_MAX, MAX_PARTICIPANTS, MAX_OTHERS = 60, 60, 10, 50   # jellybean#1158 sends ≤ 60
DAY_RX = re.compile(r"\d{4}-\d{2}-\d{2}")
AHEAD_DAYS = 60
MIN_CONFIDENCE = 0.6
KEY_PREFIX = "textprop-"
SOURCE = "text-proposal"
ROLES = ("admin", "adult", "kid")

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_proposals (owner TEXT NOT NULL, sig_id TEXT NOT NULL, thread_ref TEXT NOT NULL,
    title TEXT NOT NULL, start TEXT NOT NULL, end_at TEXT, place TEXT, participants TEXT NOT NULL, others INTEGER NOT NULL,
    with_name TEXT,
    confidence REAL NOT NULL, made_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'new', outcome TEXT,
    PRIMARY KEY (owner, sig_id));
CREATE INDEX IF NOT EXISTS idx_event_proposals_state ON event_proposals(state);
CREATE TABLE IF NOT EXISTS event_proposals_kept (owner TEXT NOT NULL, sig_id TEXT NOT NULL, thread_ref TEXT NOT NULL,
    kept_at REAL NOT NULL, PRIMARY KEY (owner, sig_id));
"""


class ProposalError(ValueError):
    pass


def _now() -> datetime:
    import local_time
    return local_time.now()


def _line(v: Any, cap: int) -> str:
    """One line of someone else's words: no markers, no control characters, capped (untrusted.clean_name)."""
    import untrusted
    import mail_suggestions as M
    return untrusted.clean_name(str(v), cap).replace(M.TITLE_OPEN, "").replace(M.TITLE_CLOSE, "").strip()


def _when(v: Any, field: str, now: datetime) -> tuple[Any, bool]:
    """→ (an aware datetime, False) for a time, or (a date, True) for a bare day ("2026-10-10": no time was named)."""
    import local_time
    from datetime import date as _date
    if isinstance(v, str) and DAY_RX.fullmatch(v):
        try:
            d = _date.fromisoformat(v)
        except ValueError:
            raise ProposalError(f"{field} must be a real day") from None
        if not now.date() <= d <= (now + timedelta(days=AHEAD_DAYS)).date():
            raise ProposalError(f"{field} must be within the next {AHEAD_DAYS} days")
        return d, True
    at = local_time.parse_iso(v) if isinstance(v, str) and START_RX.fullmatch(v) else None
    if at is None or at.tzinfo is None:
        raise ProposalError(f"{field} must be an ISO-8601 time with an offset, or a day (YYYY-MM-DD)")
    if not now - timedelta(hours=1) <= at <= now + timedelta(days=AHEAD_DAYS):
        raise ProposalError(f"{field} must be within the next {AHEAD_DAYS} days")
    return at, False


def validate(body: Any, now: datetime | None = None) -> dict[str, Any]:
    """The proposal, checked field by field; ProposalError (its message names the field, never echoes a value)."""
    if not isinstance(body, dict):
        raise ProposalError("an event proposal is a JSON object")
    extra = set(body) - FIELDS - OPTIONAL
    if extra:
        raise ProposalError("unknown fields: event proposals carry only " + ", ".join(sorted(FIELDS)))
    missing = FIELDS - set(body)
    if missing:
        raise ProposalError("missing fields: " + ", ".join(sorted(missing)))
    if body["kind"] != KIND:
        raise ProposalError("kind must be event_proposal")
    for f in ("sig_id", "thread_ref"):
        if not isinstance(body[f], str) or not HEX16.fullmatch(body[f]):
            raise ProposalError(f"{f} must be 16 lowercase hex characters")
    if not isinstance(body["title"], str):
        raise ProposalError("title must be a string")
    title = _line(body["title"], TITLE_MAX)
    if not title:
        raise ProposalError("title must have words")
    place = None
    if body["place"] is not None:
        if not isinstance(body["place"], str):
            raise ProposalError("place must be a string or null")
        place = _line(body["place"], PLACE_MAX) or None
    now = now or _now()
    start, day = _when(body["start"], "start", now)
    end = None
    if body["end"] is not None:
        end, end_day = _when(body["end"], "end", now)
        if end_day != day:
            raise ProposalError("start and end must both be days or both be times")
        if end < start or (not day and end == start):
            raise ProposalError("end must be after start")
    ps = body["participants"]
    if not isinstance(ps, list) or len(ps) > MAX_PARTICIPANTS or not all(isinstance(p, str) and PID_RX.fullmatch(p) for p in ps):
        raise ProposalError(f"participants must be a list of at most {MAX_PARTICIPANTS} person ids")
    others = body["others"]
    if isinstance(others, bool) or not isinstance(others, int) or not 0 <= others <= MAX_OTHERS:
        raise ProposalError(f"others must be a whole number from 0 to {MAX_OTHERS}")
    conf = body["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
        raise ProposalError("confidence must be a number from 0 to 1")
    with_name = None
    if body.get("with_name") is not None:
        raw = body["with_name"]
        if not isinstance(raw, str) or re.search(r"[\d@+]", raw):
            raise ProposalError("with_name must be a first name: no digits, handles or addresses")
        first = (_line(raw, 80).split() or [""])[0][:NAME_MAX]
        if not NAME_RX.fullmatch(first):
            raise ProposalError("with_name must be a first name")
        with_name = first
    return {"with_name": with_name, "day": day, "sig_id": body["sig_id"], "thread_ref": body["thread_ref"], "title": title, "start": start.isoformat(),
            "end": end.isoformat() if end else None, "place": place, "participants": sorted(set(ps)),
            "others": others, "confidence": float(conf)}


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def put(person: str, body: Any, con: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Keep one proposal, owned by the poster. The same sig_id again replaces it, and it's judged again."""
    import json
    p = validate(body, now)
    ensure(con)
    con.execute("INSERT OR REPLACE INTO event_proposals (owner, sig_id, thread_ref, title, start, end_at, place, "
                "participants, others, with_name, confidence, made_at, state, outcome) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'new',NULL)",
                (person, p["sig_id"], p["thread_ref"], p["title"], p["start"], p["end"], p["place"],
                 json.dumps(p["participants"]), p["others"], p["with_name"], p["confidence"], time.time()))
    con.commit()
    return {"kind": KIND, "sig_id": p["sig_id"], "accepted": True}


def mark_kept(con: sqlite3.Connection, owner: str, sig_id: str, at: float | None = None) -> bool:
    """The owner tapped Add (or Update) on their own proposal's card. Kept apart from event_proposals, so a re-post of
    the same sig_id (INSERT OR REPLACE) never forgets it. Another person's sig_id marks nothing. → marked."""
    ensure(con)
    row = con.execute("SELECT thread_ref FROM event_proposals WHERE owner=? AND sig_id=?", (owner, sig_id)).fetchone()
    if not row:
        return False
    con.execute("INSERT OR IGNORE INTO event_proposals_kept (owner, sig_id, thread_ref, kept_at) VALUES (?,?,?,?)",
                (owner, sig_id, row[0], time.time() if at is None else at))
    con.commit()
    return True


def kept(con: sqlite3.Connection, owner: str, since: str | None = None) -> list[dict[str, str]]:
    """The owner's own kept proposals since a day (household-zone local, inclusive), as ids only, oldest first."""
    ensure(con)
    t0 = 0.0
    if since:
        try:
            d = datetime.fromisoformat(since[:10])
        except ValueError:
            raise ProposalError("since is an ISO day") from None
        import local_time
        zone = local_time.zone()   # the household's day, never titan's UTC
        t0 = d.replace(tzinfo=zone).timestamp()
    return [{"sig_id": s, "thread_ref": t} for s, t in con.execute(
        "SELECT sig_id, thread_ref FROM event_proposals_kept WHERE owner=? AND kept_at>=? ORDER BY kept_at, sig_id",
        (owner, t0))]


# ── the pass ──────────────────────────────────────────────────────────────────────────────────────

def _local(iso: str | None) -> datetime | None:
    from event_signals import _local as local
    return local(iso)


def _names(ids: list[str], names: dict[str, str], others: int) -> str:
    """"Robin and Ava", "Robin, Ava and 2 others": household first names, then a count of anyone else."""
    parts = [names[i].split(" ")[0] for i in ids if names.get(i)]
    if others:
        parts.append(f"{others} other{'s' if others != 1 else ''}")
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


def card(p: dict[str, Any], at: Any, end: Any, names: dict[str, str], household_cal: str | None,
         move_from_day: bool = False) -> tuple[str, str, list[dict[str, str]]]:
    """(title, detail, buttons) from checked fields only. `at`/`end`: datetimes, or dates for a plan with no time yet
    (an all-day card and event: "Sat Oct 10", or "Sat Oct 10 – Sun Oct 11"). move_from_day: the same plan was added as
    an all-day event and now has a time: the card moves that event to the time instead of adding a second one."""
    import mail_suggestions as M
    day = not isinstance(at, datetime)
    place = p.get("place") or ""
    # the place is the event's location (Leave-by reads it): plain words when it reads as a place, else a data span
    loc = ""
    if place:
        loc = place if PLACE_RX.fullmatch(place) and not M.IMPERATIVE.search(place) else M.title_span(place)
    who = _names(p["participants"], names, p["others"])
    lead = f"{p['with_name']} and you agreed" if p.get("with_name") else "You agreed"
    detail = "From your texts." + (f" With {who}." if who else "") + (f" Where: {place}." if place else "")
    tail = f", location: {loc}" if loc else ""
    span = M.title_span(p["title"])
    if day:
        first, last = at.isoformat(), (end.isoformat() if end and end != at else None)
        when = M._when(first) + (f" – {M._when(last)}" if last else "")
        base = (f"{M.CARD_PROMPT_MARK} Create an all-day calendar event {span} "
                + (f"from {first} to {last}" if last else f"on {first}") + tail)
    else:
        s = {"date": at.date().isoformat(), "start": f"{at:%H:%M}"}
        if end is not None and end.date() == at.date():
            s["end"] = f"{end:%H:%M}"
        when = M._when(s["date"], s["start"])
        if move_from_day:
            title = f"{lead} on a time for “{p['title']}”: {when}. Update your calendar?"
            ask = (f"{M.CARD_PROMPT_MARK} Change my all-day calendar event {span} on {s['date']} to {s['date']} from "
                   f"{s['start']}" + (f" to {s['end']}" if s.get("end") else "") + tail + ".")
            return title, detail, [{"label": "Update", "kind": "prompt", "value": ask[:300]},
                                   {"label": "Not now", "kind": "dismiss"}]
        base = (f"{M.CARD_PROMPT_MARK} Create a calendar event {span} on {s['date']} from {s['start']}"
                + (f" to {s['end']}" if s.get("end") else "") + tail)
    title = f"{lead} on “{p['title']}”, {when}. Add to calendar?"
    buttons = [{"label": "Add", "kind": "prompt", "value": (base + " on my calendar.")[:300]}]
    if household_cal:
        buttons.append({"label": "Add to the household calendar", "kind": "prompt",
                        "value": (base + f" on the household calendar {M.title_span(household_cal)}.")[:300]})
    buttons.append({"label": "Not now", "kind": "dismiss"})
    return title, detail, buttons


def _match(events: list[dict[str, Any]], owner: str, title: str, day: str) -> dict[str, Any] | None:
    """The owner's event that day sharing a title word (mail_suggestions.on_calendar's rule), or None."""
    import mail_suggestions as M
    for n in events:
        d = n.get("data") or {}
        prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
        if str(prov.get("owner") or "zach") == owner and str(d.get("start") or "")[:10] == day and \
                M._words(d.get("title") or "") & M._words(title):
            return d
    return None


def judge(row: dict[str, Any], events: list[dict[str, Any]], cards: sqlite3.Connection, now: datetime,
          names: dict[str, str], roles: dict[str, str], household_cal: str | None, apply: bool) -> str:
    """One proposal → its outcome (a word). now: household time, naive."""
    import json
    import mail_suggestions as M
    owner = row["owner"]
    if roles.get(owner) not in ROLES:
        return "no-person"
    if row["confidence"] < MIN_CONFIDENCE:
        return "low-confidence"
    from datetime import date as _date
    day = bool(DAY_RX.fullmatch(str(row["start"] or "")))
    if day:
        at: Any = _date.fromisoformat(row["start"])
        end: Any = _date.fromisoformat(row["end_at"]) if row.get("end_at") else None
        if (end or at) < now.date():
            return "past"
    else:
        at, end = _local(row["start"]), _local(row["end_at"])
        if at is None or at <= now:
            return "past"
    title = M._safe_event_title(row["title"])
    if not title:
        return "unsafe-title"
    p = {"title": title, "place": row["place"], "others": int(row["others"]), "with_name": row.get("with_name"),
         "participants": [i for i in json.loads(row["participants"] or "[]") if i in roles and i != owner]}
    first = at.isoformat() if day else at.date().isoformat()
    found = _match(events, owner, title, first)
    move = False
    if found is not None:
        if day or not found.get("all_day"):
            return "on-calendar"
        move = True   # added as a whole day before the time was settled: move that event, never add a second
    if apply:
        # the household calendar is an adult's to write to; a kid's plan goes on the kid's own calendar
        hcal = household_cal if roles.get(owner) in ("admin", "adult") else None
        expires = f"{(end or at).isoformat()}T23:59" if day else f"{at:%Y-%m-%dT%H:%M}"
        M._store(cards, (KEY_PREFIX + row["sig_id"])[:80], "", owner, card(p, at, end, names, hcal, move),
                 expires, event=None, target=first if day else f"{at:%Y-%m-%dT%H:%M}")
    if move:
        return "move"
    return "card"


def process(con: sqlite3.Connection, events: list[dict[str, Any]], cards: sqlite3.Connection, now: datetime,
            roles: dict[str, str], names: dict[str, str], household_cal: str | None, apply: bool) -> dict[str, Any]:
    """New proposals → cards held in mail_suggestions' queue (sent by its send_held). → counts by outcome."""
    ensure(con)
    con.row_factory = sqlite3.Row
    out: dict[str, int] = {}
    rows = [dict(r) for r in con.execute("SELECT * FROM event_proposals WHERE state='new' ORDER BY made_at")]
    for r in rows:
        outcome = judge(r, events, cards, now, names, roles, household_cal, apply)
        out[outcome] = out.get(outcome, 0) + 1
        if apply:
            con.execute("UPDATE event_proposals SET state='done', outcome=? WHERE owner=? AND sig_id=?",
                        (outcome, r["owner"], r["sig_id"]))
    if apply:
        con.commit()
        cards.commit()
    return {"proposals": len(rows), "outcomes": out}


def _same_title(t: Any) -> str:
    """A title as compared for #1418: case, punctuation and spacing folded, so the calendar's own normalising
    (a trailing period, doubled spaces, an emoji the app strips) doesn't break the match, but different words do."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKC", str(t or "")).casefold()
    return " ".join(re.findall(r"[\w']+", t))


def goers(d: dict[str, Any], people: list[dict[str, Any]], con: sqlite3.Connection | None = None) -> set[str]:
    """Who a household-calendar event is a Leave-by for (Coordination, 10-09): the event matched to a plan agreed
    over texts, then that plan's poster and the adults it names. Kids get none on their own device: the adult who
    added it does. An event no plan matches: nobody (next_stop skips household events as before).
    The match is the plan's own event (#1418): Add makes it with the plan's title, so its cleaned title must EQUAL
    the plan's, and its start fall in the same minute. One shared word isn't enough: another member's household
    event at that minute ("practice", "party") would hand this plan's adults a Leave-by for an event they're not in."""
    import json
    import local_time
    at = local_time.parse_iso(d.get("start"))
    title_of_event = _same_title(d.get("title"))
    if at is None or at.tzinfo is None or not title_of_event:
        return set()
    if con is None:
        import hooks
        con = hooks.connect()
    ensure(con)
    adults = {str(p.get("id")) for p in people if str(p.get("role") or "") in ("admin", "adult")}
    out: set[str] = set()
    for owner, start, title, parts in con.execute(
            "SELECT owner, start, title, participants FROM event_proposals WHERE state='done' AND outcome='card'"):
        st = local_time.parse_iso(start)
        if st is None or abs((st - at).total_seconds()) >= 60 or _same_title(title) != title_of_event:
            continue
        out |= ({owner} | set(json.loads(parts or "[]"))) & adults
    return out
