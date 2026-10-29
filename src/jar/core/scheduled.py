#!/usr/bin/env python3
"""Scheduled actions: titan does one thing at a later time ("turn off the living room lights at 11 PM").

Zach, 10-01 (#263): "You can schedule later actions. They just need to be identified in the UI and
manageable." So every scheduled action is a card on its owner's Jellybean from the moment it's made
("Scheduled: …", with a Cancel button), the same card (key sched-<id>) later says how it went, and
GET /v1/scheduled and DELETE /v1/scheduled/{id} list and cancel them.

What runs: one tool call (`tool` + `args`). It is checked when it's scheduled as if it ran then (the
tool exists, the owner's role may use it, never a lock, alarm, garage door or camera), and again when
it runs: execute_tool as the owner, with the owner's role at that moment. Tools on titan run on titan.
A Mac tool goes to the owner's Mac the way a phone turn's does (cross_device.route), so the Mac's own
trust settings still decide whether it asks first; nobody is holding a phone at run time to answer,
so an action the Mac wants approved fails with that reason (and a "Do it now" button). It never runs
unapproved. iPhone-only tools can't be scheduled: titan can't reach the iPhone on its own.

At most once: a row moves scheduled → running in one UPDATE … WHERE state='scheduled', so two runners
(the agent API's loop and the timer) can't both take it. A row left 'running' by a runner that died is
marked failed and never run again.

A place instead of a time (10-01, #284): "when I get home, turn on the porch light". Such a row has a
trigger {place, on: arrive|leave} and no run_at. When the owner's iPhone reports that arrival or
departure (POST /v1/presence/place → on_place_event), every matching pending action of theirs runs,
once each, through the same run-as-owner path; someone else's arrival never fires it. The place must be
one titan knows for that person (home, or a place their iPhone has reported: places.known). Unfired
after 30 days, it expires as failed ("never arrived" / "never left").

A town or an address (#616): a place titan doesn't know is resolved once, when it's
scheduled, into a region the owner's iPhone watches: a town by Open-Meteo, a circle sized by its population; an
address as said, with no coordinates (the iPhone geocodes it). At most 8 pending per person. The iPhone lists its
own (GET /v1/presence/regions) and reports the region's id with the event; titan never learns where the person is,
only that they arrived.

Who (scripts/audience.py): the owner is who asked; the audience is "private" (the owner only). The
owner or the admin may cancel it.

Row: {id, owner, audience, what, tool, args, run_at (None for a place), trigger: {place, on} | None,
      expires_at, state: scheduled|running|done|failed|cancelled, result, created_at, updated_at, card_key}
Store: SQLite at $SCHEDULED_DB (default /ai/datasets/connectors/scheduled.db).

  python3 scripts/scheduled.py run-due     (titan-scheduled.timer, every minute: a backup for when the
                                            agent API is down; the API runs the same thing every 20 s)
  python3 scripts/scheduled.py list
"""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local_time  # noqa: E402

DB_PATH = Path(os.environ.get("SCHEDULED_DB", "/ai/datasets/connectors/scheduled.db"))
STATES = ("scheduled", "running", "done", "failed", "cancelled")
MAX_AHEAD_S = 30 * 86400        # a month at most: longer is a calendar event or a routine
LATE_S = 30 * 60                # due more than this long ago (titan was down): failed, not run late
STALE_RUNNING_S = 15 * 60       # a Mac action takes ~5 min at most (wake 25 s, run 120 s, confirm 110 s)
TOOLS = ("schedule_action", "list_scheduled", "cancel_scheduled")
# Never scheduled: these three themselves (no schedules of schedules).
NEVER = frozenset(TOOLS)
# home_assistant.HANDS_OFF in words: titan never changes these, now or later.
_HANDS_OFF_RX = re.compile(r"\b(?:lock|locks|locked|unlock\w*|deadbolt|alarm|alarms|garage|camera|cameras|siren|"
                           r"lock\.|alarm_control_panel\.|cover\.|camera\.|siren\.|valve\.)", re.I)
HANDS_OFF = ("Not scheduled: titan never changes locks, alarms, garage doors or cameras, now or later. "
             "Use Home Assistant for those.")
NO_MAC = "titan can't reach your Mac (it has never connected to titan, or it can't be woken)."
NEEDS_OK = ("your Mac asks before doing this, and nobody was there to say yes then (it wasn't done). "
            "Do it now from Jellybean, or mark that kind of action trusted on the Mac.")
_lock = threading.Lock()


# ── the store ───────────────────────────────────────────────────────────────────────────────

def _path(db: Path | None = None) -> Path:
    return Path(db or os.environ.get("SCHEDULED_DB") or DB_PATH)


def _db(db: Path | None = None) -> sqlite3.Connection:
    p = _path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA.format(name="scheduled"))
    if "place" not in [r[1] for r in con.execute("PRAGMA table_info(scheduled)")]:
        _migrate_v1(con)
    if "repeat" not in [r[1] for r in con.execute("PRAGMA table_info(scheduled)")]:
        try:   # #382 item 3: {"days": [0-6], "hm": "HH:MM", "until": epoch} on a repeating action
            con.execute("ALTER TABLE scheduled ADD COLUMN repeat TEXT")
        except sqlite3.OperationalError:
            pass   # another process added it first
    if "region" not in [r[1] for r in con.execute("PRAGMA table_info(scheduled)")]:
        try:   # #616: a town or an address the owner's iPhone watches, as JSON (see resolve_place)
            con.execute("ALTER TABLE scheduled ADD COLUMN region TEXT")
        except sqlite3.OperationalError:
            pass   # another process added it first
    con.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_due ON scheduled(state, run_at)")
    return con


_SCHEMA = """CREATE TABLE IF NOT EXISTS {name} (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, audience TEXT NOT NULL DEFAULT 'private', what TEXT NOT NULL,
    tool TEXT NOT NULL, args TEXT NOT NULL, run_at REAL, state TEXT NOT NULL, result TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL, card_key TEXT, runner TEXT,
    place TEXT, on_event TEXT, expires_at REAL)"""
_V1_COLS = "id, owner, audience, what, tool, args, run_at, state, result, created_at, updated_at, card_key, runner"


def _migrate_v1(con: sqlite3.Connection) -> None:
    """#301's table had run_at NOT NULL and no trigger columns: copy it into the new shape (SQLite
    can't drop a NOT NULL in place). Under BEGIN IMMEDIATE, re-checked, so two processes can't both."""
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        if "place" in [r[1] for r in con.execute("PRAGMA table_info(scheduled)")]:
            con.execute("COMMIT")
            return
        con.execute(_SCHEMA.format(name="scheduled_v2"))
        con.execute(f"INSERT INTO scheduled_v2 ({_V1_COLS}) SELECT {_V1_COLS} FROM scheduled")
        con.execute("DROP TABLE scheduled")
        con.execute("ALTER TABLE scheduled_v2 RENAME TO scheduled")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.isolation_level = ""


def _row(r: sqlite3.Row) -> dict[str, Any]:
    try:
        args = json.loads(r["args"] or "{}")
    except ValueError:
        args = {}
    return {"id": r["id"], "owner": r["owner"], "audience": r["audience"] or "private", "what": r["what"],
            "tool": r["tool"], "args": args, "run_at": r["run_at"], "state": r["state"], "result": r["result"],
            "created_at": r["created_at"], "updated_at": r["updated_at"], "card_key": r["card_key"],
            "trigger": ({"place": r["place"], "on": r["on_event"], **({"region": _region_of(r)} if _region_of(r) else {})}
                        if r["place"] else None),
            "expires_at": r["expires_at"],
            "repeat": _repeat_of(r)}


def _region_of(r: sqlite3.Row) -> dict[str, Any] | None:
    if "region" not in r.keys() or not r["region"]:
        return None
    try:
        reg = json.loads(r["region"])
    except ValueError:
        return None
    return reg if isinstance(reg, dict) else None


def _repeat_of(r: sqlite3.Row) -> dict[str, Any] | None:
    if "repeat" not in r.keys() or not r["repeat"]:
        return None
    try:
        rep = json.loads(r["repeat"])
    except ValueError:
        return None
    return rep if isinstance(rep, dict) else None


def add(owner: str, what: str, tool: str, args: dict[str, Any], run_at: float | None, *, audience: str = "private",
        db: Path | None = None, place: str | None = None, on: str | None = None,
        expires_at: float | None = None, repeat: dict[str, Any] | None = None,
        card_key: str | None = None, region: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store one scheduled action (already validated: see schedule()): a time (run_at) or a place
    (place + on, expires_at). Returns the row."""
    import audience as _aud
    what = " ".join(str(what or "").split())[:200]
    if not what or not tool or not owner:
        raise ValueError("a scheduled action needs an owner, a tool and a description")
    if (run_at is None) == (not place) or (place and on not in ("arrive", "leave")):
        raise ValueError("a scheduled action has a time, or a place with on: arrive or leave")
    sid = "sa-" + uuid.uuid4().hex[:8]
    now = time.time()
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT INTO scheduled (id, owner, audience, what, tool, args, run_at, state, result, "
                        "created_at, updated_at, card_key, place, on_event, expires_at, repeat, region) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sid, str(owner).lower(), _aud.normalize(audience, default="private"), what, tool,
                         json.dumps(args or {}, default=str), None if run_at is None else float(run_at),
                         "scheduled", None, now, now, card_key or f"sched-{sid}", place or None,
                         on if place else None, expires_at if place else None,
                         json.dumps(repeat) if repeat else None,
                         json.dumps(region) if (place and region) else None))
            con.commit()
            return _row(con.execute("SELECT * FROM scheduled WHERE id=?", (sid,)).fetchone())
        finally:
            con.close()


def visible(r: dict[str, Any], person: str | None, role: str | None) -> bool:
    """Whether a person sees a scheduled action (person and role None: titan itself sees all)."""
    import audience as _aud
    return _aud.visible(r.get("audience") or "private", person, role, owner=r.get("owner"))


def can_cancel(r: dict[str, Any], person: str | None, role: str | None) -> bool:
    """Its owner or the admin (None/None: titan itself)."""
    if (person is None and role is None) or role == "admin":
        return True
    return bool(person) and str(r.get("owner") or "").lower() == str(person).lower()


def list_for(person: str | None, role: str | None, *, include_done: bool = False, since: float | None = None,
             db: Path | None = None) -> list[dict[str, Any]]:
    """What this person sees: their pending actions, soonest first; with include_done, also the ones
    that ran, failed or were cancelled (since `since`, default the last day)."""
    if not _path(db).exists():
        return []
    con = _db(db)
    try:
        rows = [_row(r) for r in con.execute("SELECT * FROM scheduled ORDER BY COALESCE(run_at, expires_at)")]
    finally:
        con.close()
    since = time.time() - 86400 if since is None else since
    return [r for r in rows if visible(r, person, role)
            and (r["state"] in ("scheduled", "running") or (include_done and r["updated_at"] >= since))]


def get(sid: str, db: Path | None = None) -> dict[str, Any] | None:
    if not _path(db).exists():
        return None
    con = _db(db)
    try:
        r = con.execute("SELECT * FROM scheduled WHERE id=?", (str(sid or "").strip(),)).fetchone()
        return _row(r) if r else None
    finally:
        con.close()


def _transition(sid: str, frm: tuple[str, ...], to: str, *, result: str | None = None, runner: str | None = None,
                db: Path | None = None) -> bool:
    """Move a row from one of `frm` to `to` in one statement (the at-most-once guard). False: it
    wasn't in `frm` (someone else took it, it was cancelled, or it already ran)."""
    with _lock:
        con = _db(db)
        try:
            cur = con.execute(f"UPDATE scheduled SET state=?, result=COALESCE(?, result), runner=?, updated_at=? "
                              f"WHERE id=? AND state IN ({','.join('?' * len(frm))})",
                              (to, result, runner, time.time(), sid, *frm))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def mark(sid: str, state: str, result: str | None = None, db: Path | None = None) -> bool:
    """Finish a running action: done or failed, with its one-line result."""
    if state not in ("done", "failed"):
        raise ValueError("mark sets done or failed")
    return _transition(sid, ("running",), state, result=(result or "")[:500], db=db)


def cancel(sid: str, person: str | None, role: str | None, db: Path | None = None, *, card=None) -> dict[str, Any]:
    """Cancel a pending action. Raises LookupError (no such action this person sees), PermissionError
    (not theirs) or ValueError (it already ran or is running). Updates its card."""
    r = get(sid, db)
    if not r or not (visible(r, person, role) or role == "admin"):
        raise LookupError("No scheduled action like that.")
    if not can_cancel(r, person, role):
        raise PermissionError(f"“{r['what']}” isn't yours to cancel; ask whoever scheduled it.")
    if not _transition(sid, ("scheduled",), "cancelled", result="Cancelled.", db=db):
        cur = get(sid, db) or r
        raise ValueError({"running": f"“{r['what']}” is running right now.",
                          "cancelled": f"“{r['what']}” was already cancelled."}.get(
            cur["state"], f"“{r['what']}” already ran ({cur['state']})."))
    r = get(sid, db) or r
    (card or _card)(r, f"Cancelled: {r['what']}", f"It was set for {row_when(r)}; it won't run.",
                    expires_in=3600)
    return r


# ── times ───────────────────────────────────────────────────────────────────────────────────

_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
          "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600, "d": 86400, "day": 86400,
          "days": 86400}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_PART_RX = re.compile(r"(\d+(?:\.\d+)?|\bhalf an?\b|\ban?\b)\s*([a-z]+)")
_TIME_RX = re.compile(r"(?:at\s+)?(noon|midnight|(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?)$")
_DAY_RX = re.compile(r"^(?:on\s+)?(today|tonight|this evening|this afternoon|this morning|tomorrow(?: morning| night"
                     r"| evening| afternoon)?|(?:next\s+)?(?:" + "|".join(_WEEKDAYS) + r")|(?:" + "|".join(_MONTHS) +
                     r")[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?)\b")


def parse_when(text: Any, now: datetime | None = None) -> datetime:
    """When to run, in the household's time: an ISO local time ("2026-10-01T23:00"), "in 30 minutes",
    "in 2 hours and 15 minutes", "at 4:30 PM", "11 PM", "tonight at 11", "tomorrow at 7am", "Friday at
    5 PM", "Oct 3 at 9:00". A bare time that has passed today is tomorrow; "at 4" is whichever of 4:00
    and 16:00 comes next. Raises ValueError with a readable reason."""
    now = now or local_time.now()
    tz = now.tzinfo
    s = " ".join(str(text or "").strip().lower().replace("’", "'").split()).rstrip(".")
    if not s:
        raise ValueError("no time given")
    # ISO (the model's usual form): naive means the household's time.
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        if len(s) == 10:
            raise ValueError("that's a date without a time of day")
        try:
            d = datetime.fromisoformat(s.upper().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"can't read the time “{text}”") from exc
        return d.replace(tzinfo=tz) if d.tzinfo is None else d.astimezone(tz)
    # Relative: "in 30 minutes", "in 2 hours and 15 minutes", "in an hour", "90 min from now".
    if s.startswith("in ") or s.endswith(" from now"):
        body = re.sub(r"\band\b|,", " ", re.sub(r"^in\s+|\s+from now$", "", s))
        parts = _PART_RX.findall(body)
        if parts and not _PART_RX.sub("", body).strip() and all(u in _UNITS for _, u in parts):
            secs = sum((0.5 if n.startswith("half") else 1.0 if n in ("a", "an") else float(n)) * _UNITS[u]
                       for n, u in parts)
            if secs > 0:
                return (now + timedelta(seconds=secs)).replace(microsecond=0)
        raise ValueError(f"can't read the time “{text}”")
    # A day word, then a time of day.
    day_word = None
    dm = _DAY_RX.match(s)
    if dm:
        day_word = dm.group(1)
        s = s[dm.end():].strip(" ,")
    elif re.search(r"\b(tomorrow|tonight|today)$", s):   # "11 pm tomorrow"
        day_word = s.rsplit(" ", 1)[1]
        s = s.rsplit(" ", 1)[0].strip(" ,")
    tm = _TIME_RX.match(s) if s else None
    if not tm:
        if day_word and not s:
            raise ValueError(f"what time {day_word}?")
        raise ValueError(f"can't read the time “{text}”")
    if tm.group(1) in ("noon", "midnight"):
        hours, mm = [12 if tm.group(1) == "noon" else 0], 0
    else:
        hh, mm = int(tm.group(2)), int(tm.group(3) or 0)
        ampm = (tm.group(4) or "").replace(".", "")
        if hh > 23 or mm > 59 or (ampm and not 1 <= hh <= 12):
            raise ValueError(f"can't read the time “{text}”")
        evening = bool(day_word) and any(w in day_word for w in ("tonight", "evening", "night", "afternoon"))
        morning = bool(day_word) and "morning" in day_word
        if ampm:
            hours = [hh % 12 + (12 if ampm == "pm" else 0)]
        elif hh == 0 or hh >= 12 or tm.group(2).startswith("0"):
            hours = [hh]                      # 24-hour forms: 16:30, 09:00, 12:15
        elif evening:
            hours = [hh + 12]
        elif morning:
            hours = [hh]
        else:
            hours = [hh, hh + 12]             # "at 4": whichever comes next
    base = now.replace(second=0, microsecond=0)
    bare = len(hours) == 2   # "at 1": no AM/PM, no morning/evening word

    def ok(d: datetime) -> datetime:
        # A bare small hour that lands in the night (#382: "at 1" said at 2 PM became 1 AM tomorrow): ask.
        if bare and d.hour < 6:
            h = hours[0]
            raise ValueError(f"{h} in the morning or the afternoon? Say {h} AM or {h} PM")
        return d

    def at(day: datetime, h: int) -> datetime:
        return day.replace(hour=h, minute=mm)

    if not day_word or day_word in ("today", "tonight", "this evening", "this afternoon", "this morning"):
        today = [at(base, h) for h in hours if at(base, h) > now]
        if today:
            return ok(min(today))
        if day_word:
            raise ValueError(f"{_clock(at(base, hours[-1]))} today has already passed")
        return ok(min(at(base + timedelta(days=1), h) for h in hours))   # a bare time that has passed: tomorrow
    if day_word.startswith("tomorrow"):
        return ok(min(at(base + timedelta(days=1), h) for h in hours))
    wd = next((i for i, w in enumerate(_WEEKDAYS) if w in day_word), None)
    if wd is not None:
        ahead = (wd - base.weekday()) % 7
        if day_word.startswith("next") and ahead == 0:
            ahead = 7
        cands = [c for c in (at(base + timedelta(days=ahead), h) for h in hours) if c > now]
        return ok(min(cands or [at(base + timedelta(days=ahead + 7), h) for h in hours]))
    mon = next(i for i, mo in enumerate(_MONTHS) if day_word.startswith(mo))
    day = int(re.search(r"\d{1,2}", day_word).group(0))
    for year in (base.year, base.year + 1):
        try:
            d = base.replace(year=year, month=mon + 1, day=day)
        except ValueError as exc:
            raise ValueError(f"there's no {day_word}") from exc
        cands = [c for c in (at(d, h) for h in hours) if c > now]
        if cands:
            return ok(min(cands))
    raise ValueError(f"can't read the time “{text}”")


def _clock(d: datetime) -> str:
    return f"{d.hour % 12 or 12}:{d.minute:02d} {'AM' if d.hour < 12 else 'PM'}"


def when_words(ts: float, now: datetime | None = None) -> str:
    """"today at 11:00 PM", "tomorrow at 7:00 AM", "Friday at 5:00 PM (Oct 3)", "Mon Oct 20 at 9:00 AM"."""
    now = now or local_time.now()
    d = datetime.fromtimestamp(float(ts), now.tzinfo)
    days = (d.date() - now.date()).days
    if days == 0:
        return f"today at {_clock(d)}"
    if days == 1:
        return f"tomorrow at {_clock(d)}"
    if 1 < days < 7:
        return f"{d:%A} at {_clock(d)} ({d:%b} {d.day})"
    return f"{d:%a %b} {d.day} at {_clock(d)}"


def place_words(place: str, on: str, past: bool = False) -> str:
    """"when you get home", "when you get to work", "when you leave work" (past: "when you got home")."""
    home = str(place).lower() == "home"
    if on == "arrive":
        return ("when you got " if past else "when you get ") + ("home" if home else f"to {place}")
    return ("when you left " if past else "when you leave ") + ("home" if home else str(place))


def row_when(r: dict[str, Any], now: datetime | None = None, past: bool = False) -> str:
    """When a row runs, in words: its time, or its place trigger."""
    t = r.get("trigger")
    return place_words(t["place"], t["on"], past) if t else when_words(r["run_at"], now)


_ON = {"arrive": "arrive", "arrived": "arrive", "arriving": "arrive", "arrival": "arrive", "get": "arrive",
       "get to": "arrive", "reach": "arrive", "enter": "arrive", "get home": "arrive", "leave": "leave",
       "left": "leave", "leaving": "leave", "depart": "leave", "departure": "leave", "exit": "leave"}


# A town or an address instead of a saved place (#616). Off until the iPhone watches regions (its half of #616):
# before that, a region trigger could only lapse after 30 days.
REGION_BUDGET = 8          # pending region triggers per person: iOS monitors 20 conditions per app; places keep the rest
ADDRESS_RADIUS_M = 150
_RADIUS_TIERS = ((10_000, 3_000), (100_000, 6_000), (500_000, 10_000))   # population under → metres; else 15 km
_HOUSE_NUMBER_RX = re.compile(r"^\s*\d+[a-z]?\s+\S", re.I)
# Words for a kind of place, never a town: "school" must stay "a place you named", not a village called School.
_NOT_TOWNS = frozenset("""work office school gym church park store shop mall practice daycare hospital doctor
    dentist library pool airport station grocery groceries market restaurant cafe coffee bar class college
    university campus field court rink studio clinic pharmacy bank barber salon garage friend friends mom dad
    grandma grandpa mom's dad's grandma's grandpa's""".split())


def radius_for(population: int | None) -> int:
    """A town's circle: under 10k people 3 km, under 100k 6 km, under 500k 10 km, otherwise 15 km (an unknown
    population is a small town's)."""
    for under, m in _RADIUS_TIERS:
        if (population or 0) < under:
            return m
    return 15_000


def region_words(region: dict[str, Any]) -> str:
    """The reply's "(about 6 km around town)" / "(within about 150 m of it; your iPhone finds the address)"."""
    if region.get("kind") == "town":
        return f" (about {round(region['radius_m'] / 1000)} km around town)"
    return f" (within about {region['radius_m']} m of it; your iPhone finds the address)"


def _pending_regions(person: str, now: float, db: Path | None = None) -> int:
    con = _db(db)
    try:
        return con.execute("SELECT COUNT(*) FROM scheduled WHERE owner=? AND state='scheduled' AND run_at IS NULL "
                           "AND region IS NOT NULL AND (expires_at IS NULL OR expires_at>?)",
                           (str(person).lower(), now)).fetchone()[0]
    finally:
        con.close()


def _region(text: str, geocode: Callable[..., Any] | None = None) -> dict[str, Any]:
    """A town (Open-Meteo, its circle by population) or an address (no coordinates: the iPhone geocodes it).
    ValueError to ask instead: two towns tie, or the words are a kind of place, not a town."""
    label = text[:40].rstrip(" ,")
    if _HOUSE_NUMBER_RX.match(text):
        return {"kind": "address", "label": label, "query": text[:200], "radius_m": ADDRESS_RADIUS_M}
    bare = re.sub(r"^(?:my|the|our|a|an)\s+", "", text.lower()).strip()
    if bare in _NOT_TOWNS or bare.rstrip("s") in _NOT_TOWNS:
        raise LookupError(text)
    if geocode is None:
        import weather
        geocode = weather.geocode
    try:
        hits = geocode(text, every=True) or []
    except Exception as exc:  # noqa: BLE001 - the geocoder is down: ask, never guess
        raise ValueError(f"titan couldn't look up “{text}” just now; try again in a minute") from exc
    if not hits:
        return {"kind": "address", "label": label, "query": text[:200], "radius_m": ADDRESS_RADIUS_M}
    top = [h for h in hits if h.get("score") == hits[0].get("score")]
    by_label: dict[str, dict[str, Any]] = {}
    for h in top:   # the same town listed twice (a city and its admin area) is one answer: the bigger
        cur = by_label.get(h["name"])
        if cur is None or (h.get("population") or 0) > (cur.get("population") or 0):
            by_label[h["name"]] = h
    towns = sorted(by_label.values(), key=lambda h: -(h.get("population") or 0))
    if len(towns) > 1:
        a, b = towns[0].get("population") or 0, towns[1].get("population") or 0
        # One town at least 20 times the next is the one meant (Spokane, WA over Spokane, MO); Portland, Oregon
        # and Portland, Maine are both real answers, so ask.
        if not a or b * 20 > a:
            names = " or ".join(f"{h['name']}" for h in towns[:3])
            raise ValueError(f"which one: {names}? Say it with the state or country")
    h = towns[0]
    return {"kind": "town", "label": h["name"][:40], "lat": round(float(h["latitude"]), 5),
            "lon": round(float(h["longitude"]), 5), "radius_m": radius_for(h.get("population"))}


def resolve_place(person: str, raw: Any, *, check: bool = True, now: float | None = None,
                  db: Path | None = None, geocode: Callable[..., Any] | None = None) -> tuple[str, dict[str, Any] | None]:
    """(the place's name, its region or None). A place titan knows for this person ("home", or a name their
    iPhone has reported) first; otherwise, with regions on (#616), a town or an address their iPhone will
    watch. ValueError saying what to do instead. check=False (eval turns): any well-formed name, no region."""
    import places
    text = " ".join(str(raw or "").split())
    bare = re.sub(r"^(?:my|the|our)\s+", "", text.lower())
    if bare in ("house", "home", "place", "apartment"):
        text = bare = "home"
    if not text:
        raise ValueError("say which place (home, or one you named on your iPhone)")
    if not check:
        try:
            return places.clean_place(text), None
        except ValueError as exc:
            raise ValueError("say which place (home, or one you named on your iPhone)") from exc
    if not places.recent(person, limit=1):
        raise ValueError("your iPhone hasn't told titan about arriving or leaving yet, so titan wouldn't know "
                         "when it happens. Turn on \"Tell titan when I arrive or leave\" in Jellybean's settings "
                         "on the iPhone")
    known = places.known(person)
    hit = next((k for k in known if k.lower() in (text.lower(), bare)), None)
    if hit:
        return hit, None
    unknown = (f"titan doesn't know a place called “{raw}” for you. It knows: {', '.join(known)} "
               "(the places you named in the iPhone's Settings › Places)")
    try:
        region = _region(text, geocode)
    except LookupError:
        raise ValueError(unknown) from None
    if _pending_regions(person, now or time.time(), db) >= REGION_BUDGET:
        raise ValueError(f"you have {REGION_BUDGET} place reminders for towns or addresses waiting. Cancel one first")
    return region["label"], region


# ── scheduling (the tools) ──────────────────────────────────────────────────────────────────

def _lib():
    import agent_runtime_lib as lib
    return lib


def _asker() -> tuple[str, str, str | None]:
    """(person id, role, name) of the turn asking."""
    import household_identity as hh
    lib = _lib()
    person = getattr(lib._TURN_IO, "person", None)
    pid = str(person.get("id")) if isinstance(person, dict) and person.get("id") else hh.ADMIN_PERSON_ID
    return pid.lower(), lib.turn_role(), (person or {}).get("name") if isinstance(person, dict) else None


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        v = json.loads(str(raw))
    except ValueError as exc:
        raise ValueError("tool_args must be a JSON object like {\"device\": \"Living Room Lamp\", \"action\": \"off\"}") from exc
    if not isinstance(v, dict):
        raise ValueError("tool_args must be a JSON object")
    return v


def _default_what(tool: str, args: dict[str, Any]) -> str:
    if tool == "home_control" and args.get("device"):
        return f"{str(args.get('action') or 'change').replace('_', ' ')} {args['device']}".strip()
    for k in ("title", "message", "text", "name", "query"):
        if args.get(k):
            return f"{tool.replace('_', ' ')}: {str(args[k])[:80]}"
    return tool.replace("_", " ")


def _is_device(tool: str) -> bool:
    return tool in _lib()._DEVICE_TOOLS


def refusal(tool: str, args: dict[str, Any], owner: str, role: str, *, check_mac: bool = True) -> str | None:
    """Why this action can't be scheduled for this person (None: it can). The same checks as
    running it now, plus what can't happen later (the iPhone, locks)."""
    lib = _lib()
    if not tool:
        return "Not scheduled: say which action to run (tool_name)."
    if tool in NEVER:
        return "Not scheduled: a scheduled action can't schedule or cancel others."
    if tool not in lib._TOOL_REGISTRY and tool not in lib._DEVICE_TOOLS:
        return f"Not scheduled: there's no action called {tool}."
    import household_identity as hh
    if not hh.role_allows_tool(role, tool):
        return "Not scheduled: " + lib._ROLE_REFUSAL
    if tool in ("home_control", "run_shortcut", "run_applescript", "run_shell") and \
            _HANDS_OFF_RX.search(json.dumps(args, default=str)):
        return HANDS_OFF
    if tool == "home_control":
        dom = _ha_domain(args)
        if dom:
            import home_assistant as ha
            if dom in ha.HANDS_OFF:
                return HANDS_OFF
    if _is_device(tool):
        spec = lib._DEVICE_TOOLS[tool]
        if spec.get("internal"):
            return f"Not scheduled: {tool} isn't an action on its own."
        if tool in lib.PHONE_ONLY_TOOLS:
            return ("Not scheduled: that runs on the iPhone itself, and titan can't reach the iPhone on its own at a "
                    "set time. A reminder at that time works (the user does it then).")
        if check_mac:
            try:
                macs = hh.get_store().macs_for(owner)
            except Exception:  # noqa: BLE001
                macs = []
            if not macs:
                return ("Not scheduled: that runs on the Mac, and titan has never connected to this person's Mac, so "
                        "it couldn't do it then. A reminder at that time works (the user does it then).")
    return None


def _ha_domain(args: dict[str, Any]) -> str | None:
    """The Home Assistant domain of home_control's device, when titan can tell (an entity id, or
    Home Assistant answering now); None otherwise (run time checks again)."""
    dev = str(args.get("device") or args.get("entity") or "").strip()
    if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", dev):
        return dev.split(".", 1)[0]
    if not dev or os.environ.get("AGENT_TURN_SOURCE") == "test":
        return None
    try:
        import home_assistant as ha
        hits = ha._find(ha._http("GET", "/api/states", None), dev)
        doms = {h.get("entity_id", "").split(".", 1)[0] for h in hits}
        hit = doms & ha.HANDS_OFF
        return next(iter(hit)) if hit else (next(iter(doms)) if len(doms) == 1 else None)
    except Exception:  # noqa: BLE001 - Home Assistant down: the run checks again
        return None


# ── repeating (#382 item 3) ─────────────────────────────────────────────────────────────────────

REPEAT_DAYS = 90   # a repeating action runs for this long, then lapses with a card (re-schedule to keep it)
_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_repeat(text: Any) -> list[int]:
    """The weekdays (0 = Monday) a repeat says: "daily", "every day", "weekdays", "every weekday",
    "weekends", "Mondays and Thursdays", "every Mon, Wed, Fri". Raises ValueError otherwise."""
    s = " ".join(str(text or "").lower().replace(",", " ").replace("/", " ").split())
    s = re.sub(r"\b(every|each|on|and|the)\b", " ", s).strip()
    s = " ".join(s.split())
    if s in ("daily", "day", "days", "everyday", "every day"):
        return list(range(7))
    if s in ("weekday", "weekdays", "work day", "work days", "workday", "workdays"):
        return [0, 1, 2, 3, 4]
    if s in ("weekend", "weekends", "weekend day", "weekend days"):
        return [5, 6]
    days = set()
    for w in s.split():
        key = w[:3]
        if key in _DAY_NAMES and re.fullmatch(r"(mon|tue|tues|wed|wednes|thu|thur|thurs|fri|sat|satur|sun)(day)?s?", w):
            days.add(_DAY_NAMES[key])
        else:
            raise ValueError(f"can't read how it repeats (“{text}”); say “every weekday”, “daily” or the days")
    if not days:
        raise ValueError("say how it repeats: “every weekday”, “daily” or the days")
    return sorted(days)


def next_on(days: list[int], hm: str, after: datetime) -> datetime:
    """The first of `days` at `hm` (HH:MM, household time) strictly after `after`."""
    h, m = (int(x) for x in hm.split(":"))
    for add_days in range(0, 8):
        d = (after + timedelta(days=add_days)).replace(hour=h, minute=m, second=0, microsecond=0)
        if d.weekday() in days and d > after:
            return d
    raise ValueError("no next day")


def repeat_words(rep: dict[str, Any]) -> str:
    days = rep.get("days") or []
    h, m = (int(x) for x in str(rep.get("hm") or "00:00").split(":"))
    clock = f"{(h % 12) or 12}:{m:02d} {'PM' if h >= 12 else 'AM'}".replace(":00 ", " ")
    if days == list(range(7)):
        said = "every day"
    elif days == [0, 1, 2, 3, 4]:
        said = "every weekday"
    elif days == [5, 6]:
        said = "every weekend day"
    else:
        names = [_WEEKDAYS[d].capitalize() + "s" for d in days]
        said = "on " + (" and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1])
    return f"{said} at {clock}"


def _schedule_next(r: dict[str, Any], *, db: Path | None = None) -> dict[str, Any] | None:
    """After a repeating action ran (or failed): its next one, in the same series and on the same card,
    unless the series reached its end. None when it ended."""
    rep = r.get("repeat")
    if isinstance(rep, str):
        try:
            rep = json.loads(rep)
        except ValueError:
            return None
    if not isinstance(rep, dict) or not rep.get("days"):
        return None
    tz = local_time.zone()
    after = datetime.fromtimestamp(float(r["run_at"] or time.time()), tz)
    nxt = next_on(list(rep["days"]), str(rep["hm"]), max(after, datetime.fromtimestamp(time.time(), tz)))
    if nxt.timestamp() > float(rep.get("until") or 0):
        return None
    return add(r["owner"], r["what"], r["tool"], r.get("args") or {}, nxt.timestamp(), audience=r.get("audience") or "private",
               db=db, repeat=rep, card_key=r.get("card_key"))


def schedule(tool: str, args: dict[str, Any], at: Any, what: str | None, *, owner: str, role: str,
             now: datetime | None = None, db: Path | None = None, card=None, check_mac: bool = True,
             store: bool = True, place: Any = None, on: Any = None, repeat: Any = None,
             before_store: Callable[[], str | None] | None = None) -> tuple[dict[str, Any] | None, str]:
    """Validate, store and card one action: at a time (`at`), or on arriving at or leaving a place
    (`place` + `on`). Returns (row or None, the reply sentence). check_mac=False (eval turns) skips the
    checks against the person's own devices (their Mac, their reported places). before_store: a last check once
    the action is valid and about to be stored (the tool's duplicate guard); its sentence refuses it."""
    why = refusal(tool, args, owner, role, check_mac=check_mac)
    if why:
        return None, why
    now = now or local_time.now()
    if place not in (None, "") or on not in (None, ""):
        if at not in (None, ""):
            return None, "Not scheduled: give a time or a place, not both."
        trig = _ON.get(" ".join(str(on or "").lower().split()))
        if not trig:
            return None, "Not scheduled: say whether it's on arriving at the place or on leaving it (on: arrive or leave)."
        try:
            name, region = resolve_place(owner, place, check=check_mac, now=now.timestamp(), db=db)
        except ValueError as exc:
            return None, f"Not scheduled: {exc}."
        what = " ".join(str(what or "").split())[:200] or _default_what(tool, args)
        words = place_words(name, trig) + (region_words(region) if region else "")
        until = now.timestamp() + MAX_AHEAD_S
        where = (" Your Mac does it then; if it's set to ask first for this, it asks, and nobody may be there to say "
                 "yes." if _is_device(tool) else "")
        if not store:
            return None, f"Test turn: scheduled “{what}” {words} (place {name}, on {trig}).{where}"
        if before_store and (why := before_store()):
            return None, why
        row = add(owner, what, tool, args, None, db=db, place=name, on=trig, expires_at=until, region=region)
        (card or _card)(row, f"Scheduled: {what}, {words}",
                        f"Any time in the next 30 days (until {when_words(until, now).split(' at ')[0]})."
                        + (" On your Mac." if _is_device(tool) else ""),
                        actions=[{"label": "Cancel", "kind": "prompt", "value": f"Cancel scheduled action {row['id']}"}])
        return row, (f"Scheduled “{what}” {words}. It's a card in Jellybean, where you can cancel it, and the same "
                     f"card says how it went; if it hasn't happened in 30 days it lapses.{where}")
    if repeat not in (None, ""):
        if at in (None, "") or str(at).strip().lower().startswith("in "):
            return None, "Not scheduled: a repeating action needs a time of day (at: “4:30 PM”) with how it repeats."
        try:
            days = parse_repeat(repeat)
            first = parse_when(at, now)
        except ValueError as exc:
            return None, f"Not scheduled: {exc}."
        rep = {"days": days, "hm": f"{first.hour:02d}:{first.minute:02d}",
               "until": now.timestamp() + REPEAT_DAYS * 86400}
        start = next_on(days, rep["hm"], now)
        what = " ".join(str(what or "").split())[:200] or _default_what(tool, args)
        words = repeat_words(rep)
        until = when_words(rep["until"], now).split(" at ")[0]
        where = (" Your Mac does it each time; if it's set to ask first for this, it asks each time." if _is_device(tool) else "")
        if not store:
            return None, f"Test turn: scheduled “{what}” {words}, starting {when_words(start.timestamp(), now)}.{where}"
        if before_store and (why := before_store()):
            return None, why
        row = add(owner, what, tool, args, start.timestamp(), db=db, repeat=rep)
        (card or _card)(row, f"Scheduled: {what}, {words}",
                        f"Next {when_words(start.timestamp(), now)}; until {until}." + (" On your Mac." if _is_device(tool) else ""),
                        actions=[{"label": "Cancel", "kind": "prompt", "value": f"Cancel scheduled action {row['id']}"}])
        return row, (f"Scheduled “{what}” {words}, starting {when_words(start.timestamp(), now)}, for {REPEAT_DAYS} "
                     f"days (until {until}). It's one card in Jellybean, where you can cancel it; it says how each one "
                     f"went.{where}")
    try:
        when = parse_when(at, now)
    except ValueError as exc:
        return None, (f"Not scheduled: {exc}. Give a time like “11:00 PM”, “in 30 minutes” or “tomorrow at "
                      "7 AM”.")
    if when.timestamp() <= now.timestamp():
        return None, f"Not scheduled: {when_words(when.timestamp(), now)} has already passed."
    if when.timestamp() - now.timestamp() > MAX_AHEAD_S:
        return None, ("Not scheduled: that's more than 30 days away. A calendar event or a reminder suits "
                      "something that far off.")
    what = " ".join(str(what or "").split())[:200] or _default_what(tool, args)
    words = when_words(when.timestamp(), now)
    where = (" Your Mac does it then; if it's set to ask first for this, it asks, and nobody may be there to say "
             "yes." if _is_device(tool) else "")
    if not store:
        return None, f"Test turn: scheduled “{what}” for {words}.{where}"
    if before_store and (why := before_store()):
        return None, why
    row = add(owner, what, tool, args, when.timestamp(), db=db)
    (card or _card)(row, f"Scheduled: {what}",
                    f"{words[0].upper() + words[1:]}." + (" On your Mac." if _is_device(tool) else ""),
                    actions=[{"label": "Cancel", "kind": "prompt", "value": f"Cancel scheduled action {row['id']}"}])
    return row, (f"Scheduled “{what}” for {words}. It's a card in Jellybean, where you can cancel it, and the "
                 f"same card says how it went.{where}")


CANCEL_ASK_RX = re.compile(r"^\s*cancel scheduled action (sa-[0-9a-f]{8})\s*\.?\s*$", re.I)


def asked_cancel(text: Any) -> str | None:
    """The id when a message is exactly a card's Cancel button ("Cancel scheduled action sa-…"): the turn
    cancels it in code, without the model (#382 item 4)."""
    m = CANCEL_ASK_RX.match(str(text or ""))
    return m.group(1).lower() if m else None


_DAY_WORDS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays")


def _pending_like(ref: str, owner: str | None, role: str | None) -> list[dict[str, Any]]:
    """This person's pending actions an id ("sa-…", or the card's "Cancel scheduled action sa-…") or a few words of
    what it does point at."""
    sid = re.search(r"\bsa-[0-9a-f]{8}\b", ref)
    pending = [r for r in list_for(owner, role) if r["state"] == "scheduled"]
    if sid:
        hit = get(sid.group(0))
        return [hit] if hit and (hit in pending or role == "admin") else []
    return [r for r in pending if ref.lower() in r["what"].lower()]


def tool(name: str, args: dict[str, Any]) -> str:
    """The agent tools: schedule_action, list_scheduled, cancel_scheduled (as the turn's person)."""
    lib = _lib()
    owner, role, _ = _asker()
    test_turn = lib.turn_source() in ("eval", "canary")
    if name == "schedule_action":
        tname = str(args.get("tool_name") or args.get("tool") or args.get("action_tool") or "").strip()
        try:
            targs = _parse_args(args.get("tool_args") if "tool_args" in args else args.get("args"))
        except ValueError as exc:
            return f"Not scheduled: {exc}"
        import file_tool
        if tname == file_tool.MERGED:
            # #1251 C: stored as the mode's own tool, so every check here and at run time is that tool's
            real, mapped, bad = file_tool.resolve(targs if isinstance(targs, dict) else {})
            if bad:
                return bad.replace("Not run:", "Not scheduled:", 1)
            tname, targs = real, mapped
        at = args.get("at") or args.get("when") or args.get("time")
        place, on = args.get("place"), args.get("on") or args.get("event")
        repeat, what = args.get("repeat") or args.get("every"), args.get("what")
        # An edit names the pending action it changes (change), and keeps whatever the call leaves out; a second
        # action that does exactly the same thing is refused unless asked for (10-03 audit, #802's shape: "make it 8
        # instead" scheduled a second one, and both ran).
        change = " ".join(str(args.get("change") or "").split())
        old = None
        if change:
            hits = _pending_like(change, owner, role)
            if not hits:
                return "Not changed: no pending scheduled action like that. List them to see what's scheduled."
            if len(hits) > 1:
                return "Several match; ask which one: " + "; ".join(f"“{r['what']}” {row_when(r)}" for r in hits[:8])
            old = hits[0]
            tname = tname or old["tool"]
            if "tool_args" not in args and "args" not in args:
                targs = old["args"]
            what = what or old["what"]
            if at in (None, "") and place in (None, ""):
                if old["trigger"]:
                    place, on = old["trigger"]["place"], on or old["trigger"]["on"]
                elif old["repeat"]:
                    at = old["repeat"].get("hm")
                    repeat = repeat or " ".join(_DAY_WORDS[d] for d in old["repeat"].get("days") or [])
                elif old["run_at"]:
                    at = datetime.fromtimestamp(old["run_at"], local_time.zone()).replace(tzinfo=None).isoformat(
                        timespec="minutes")

        def duplicate() -> str | None:
            if old is not None or str(args.get("new") or "").strip().lower() in ("yes", "true", "1"):
                return None
            key = json.dumps(targs, sort_keys=True, default=str)
            same = [r for r in list_for(owner, role) if r["state"] == "scheduled" and r["tool"] == tname
                    and json.dumps(r["args"], sort_keys=True, default=str) == key]
            if not same:
                return None
            r = same[0]
            return (f"Not scheduled: “{r['what']}” is already scheduled {row_when(r)} (id {r['id']}). To move or "
                    f"change it, call schedule_action again with change set to its id. If the person wants a second "
                    f"one as well, ask them, then call again with new='yes'.")
        row, reply = schedule(tname, targs, at, what, owner=owner, role=role, check_mac=not test_turn,
                              store=not test_turn, place=place, on=on, repeat=repeat, before_store=duplicate)
        if row and not test_turn:   # a turn asked from a project page files what it made there (#1173)
            try:
                import agent_runtime_lib as _turn_lib   # not `_lib`: that name is this function's already
                _turn_lib._note_created(f"scheduled:{row['id']}")
            except Exception:  # noqa: BLE001 - outside a turn there's nothing to note
                pass
        if old is None or (row is None and not test_turn):
            return reply
        if test_turn:
            return f"{reply} (Test turn: “{old['what']}” would be replaced.)"
        try:
            cancel(old["id"], owner, role)
        except (LookupError, PermissionError, ValueError) as exc:
            return f"{reply} The old one couldn't be cancelled ({exc.args[0] if exc.args else exc}); cancel it from its card."
        return f"Changed: {reply} It replaces the one that was set {row_when(old)}."
    if name == "list_scheduled":
        rows = list_for(owner, role, include_done=True)
        if not rows:
            return "Nothing is scheduled."
        def line(r: dict[str, Any]) -> str:
            tail = {"scheduled": "", "running": " — running now", "cancelled": " — cancelled"}.get(
                r["state"], f" — {r['state']}: {str(r['result'] or '')[:120]}")
            return f"• {row_when(r)} — {r['what']} (id {r['id']}){tail}"
        pending = [r for r in rows if r["state"] in ("scheduled", "running")]
        recent = sorted((r for r in rows if r not in pending), key=lambda r: -r["updated_at"])[:10]
        return "\n".join(line(r) for r in pending[:30] + recent)
    # cancel_scheduled: by id, or by what it says among this person's pending ones
    ref = " ".join(str(args.get("id") or args.get("what") or "").split())
    if not ref:
        return "Not cancelled: say which one (its id or what it does)."
    hits = _pending_like(ref, owner, role)
    if not hits:
        return "Not cancelled: no pending scheduled action like that. List them to see what's scheduled."
    if len(hits) > 1:
        return "Several match; ask which one: " + "; ".join(f"“{r['what']}” {row_when(r)}" for r in hits[:8])
    if test_turn:
        return f"Test turn: cancelled “{hits[0]['what']}”."
    try:
        r = cancel(hits[0]["id"], owner, role)
    except (LookupError, PermissionError, ValueError) as exc:
        return f"Not cancelled: {exc.args[0] if exc.args else exc}"
    return f"Cancelled “{r['what']}” (it was set for {row_when(r)})."


# ── cards ───────────────────────────────────────────────────────────────────────────────────

def _card(r: dict[str, Any], title: str, detail: str, *, urgency: int = 1, actions: Any = None,
          expires_in: float | None = None, kind: str = "reminder") -> None:
    """The action's one card on its owner's Jellybean (key sched-<id>): made when it's scheduled,
    updated in place when it runs, fails or is cancelled."""
    import proactive
    proactive.add_card(title, detail, kind=kind, urgency=urgency, source="scheduled", to=[r["owner"]],
                       key=r.get("card_key") or f"sched-{r['id']}", actions=actions, expires_in=expires_in)


def _result_card(r: dict[str, Any], ok: bool, result: str, card=None, *, title: str | None = None,
                 urgency: int = 2) -> None:
    line = " ".join(str(result or "").split())
    line = (line[:157] + "…") if len(line) > 160 else line
    if ok and r.get("tool") == "run_task" and "task is running" in str(result):
        # One step of a multi-step task (#283): its result, then the next step is one tap, never unattended.
        tid = str((r.get("args") or {}).get("task_id") or "")
        waiting = "— needs you:" in str(result)
        (card or _card)(r, ("Waiting on you: " if waiting else "Step done: ") + r["what"],
                        f"{line} ({row_when(r, past=True)})", kind="update", urgency=2 if waiting else 1,
                        actions=[{"label": "Next step", "kind": "prompt", "value": f"Run the next step of task {tid}"},
                                 {"label": "OK", "kind": "dismiss"}] if not waiting else
                                [{"label": "Open task", "kind": "prompt", "value": f"What's the status of task {tid}?"},
                                 {"label": "OK", "kind": "dismiss"}])
    elif ok:
        (card or _card)(r, f"Done: {r['what']}", f"{line or 'Done.'} ({row_when(r, past=True)})", kind="update",
                        expires_in=12 * 3600)
    else:
        # urgency 2: it reaches the phone (push.py), so a failure at 11 PM isn't found the next day.
        (card or _card)(r, title or f"Couldn't {r['what']}", line or "It failed.", kind="alert", urgency=urgency,
                        actions=[{"label": "Do it now", "kind": "prompt", "value": f"Do this now: {r['what']}"},
                                 {"label": "OK", "kind": "dismiss"}])


# ── running ─────────────────────────────────────────────────────────────────────────────────

def _runner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _runner_dead(runner: str | None) -> bool:
    """Whether the process that claimed a row is gone (same host only; another host's: unknown)."""
    host, _, pid = str(runner or "").rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def reap(db: Path | None = None, *, card=None, now: float | None = None) -> int:
    """Rows left 'running' by a runner that died (or that has run far too long): failed, never re-run."""
    if not _path(db).exists():
        return 0
    now = now or time.time()
    con = _db(db)
    try:
        rows = con.execute("SELECT * FROM scheduled WHERE state='running'").fetchall()
    finally:
        con.close()
    n = 0
    for r in rows:
        if not (_runner_dead(r["runner"]) or now - float(r["updated_at"]) > STALE_RUNNING_S):
            continue
        why = "titan stopped while it was running, so whether it happened is unknown; it wasn't run again."
        if mark(r["id"], "failed", why, db=db):
            _result_card(_row(r), False, why, card)
            n += 1
    return n


def mac_hook(owner: str, sid: str, store: Any = None) -> Callable[[dict], dict]:
    """The device hook for a scheduled Mac action: the owner's Mac through cross_device, as a phone
    turn reaches it. Only meaningful in the agent API's process (the Mac's long-poll is there)."""
    import cross_device as xd
    asked: dict[str, Any] = {}

    def ask_confirm(payload: dict[str, Any]) -> dict[str, Any]:
        asked["confirm"] = payload   # nobody is holding a phone at run time: not approved
        return {"approved": False, "remember_session": False}

    def hook(req: dict[str, Any]) -> dict[str, Any]:
        mac = xd.mac_for(owner, store)
        if not mac:
            return {"error": NO_MAC}
        res = xd.route(req, person_id=owner, mac=mac, turn_id=sid, session=f"scheduled:{sid}",
                       origin={"device": "titan (scheduled)", "platform": "titan"},
                       emit=lambda event, data: None, ask_confirm=ask_confirm)
        if asked:
            return {"result": "Not done: " + NEEDS_OK, "completed": False}
        return res
    return hook


def _grant_for(r: dict[str, Any]) -> str | None:
    """An active grant this scheduled device action holds, checked against titan's copy (tool, exact
    arguments, window, uses). The device verifies its own record too; any doubt and it asks, as before."""
    if not _is_device(r["tool"]):
        return None
    try:
        import grants
        lib = _lib()
        for g in grants.list_for(r["owner"]):
            if g["subject_kind"] == "scheduled" and g["subject_ref"] == r["id"] and \
                    grants.use(g["id"], r["owner"], r["tool"], dict(r["args"]), classify=lib.effect_class) is None:
                return g["id"]
    except Exception:  # noqa: BLE001 - a grant problem never blocks the action: it asks instead
        return None
    return None


def _execute_as(r: dict[str, Any], role: str, name: str | None,
                device: Callable[[str, str], Callable[[dict], dict]] | None) -> tuple[bool, str]:
    """Run one action through execute_tool as its owner. (ok, one-line result)."""
    lib = _lib()
    io = lib._TURN_IO
    keys = ("person", "role", "device", "client", "device_tools", "remote", "device_completed", "outcomes",
            "failed_calls", "source", "eval_mac", "travel_which", "deadline", "status", "scheduled", "grant_id", "later")
    saved = {k: getattr(io, k, None) for k in keys}
    try:
        # A clean slate: this worker thread may have run anything before (the API's to_thread pool).
        for k in ("source", "eval_mac", "travel_which", "deadline", "status", "later"):
            setattr(io, k, None)   # "later": the request that scheduled it said "at 11 PM"; now is that time
        io.person = {"id": r["owner"], "name": name}
        io.role = role
        io.client = "mac"
        io.device_tools = None
        io.remote = None
        io.device_completed = None
        io.outcomes = []
        io.failed_calls = {}
        io.scheduled = r["id"]   # run_task does one step when scheduled (#283)
        io.grant_id = _grant_for(r)   # approved ahead on the device (#395/#382), or None: the device asks
        # a device action's hook; or a task step whose owner granted it device reads (#395: their own Mac only)
        io.device = (device(r["owner"], r["id"]) if (device and (_is_device(r["tool"]) or
                     (r["tool"] == "run_task" and getattr(io, "task_reads", None)))) else None)
        out = str(lib.execute_tool(r["tool"], dict(r["args"])) or "")
        text = " ".join(out.split())
        ok = (not lib._FAIL_RX.search(text[:300]) and text != lib._ROLE_REFUSAL
              and getattr(io, "device_completed", None) is not False
              and not text.startswith(("Not scheduled", "That's not something")))
        return ok, text
    finally:
        for k, v in saved.items():
            setattr(io, k, v)


def run_due(now: float | None = None, *, db: Path | None = None, device=None, execute=None, card=None,
            store: Any = None) -> list[dict[str, Any]]:
    """Run every action whose time has come, once each. device(owner, id) → a device hook (the agent
    API passes mac_hook); without one (the timer's own process can't reach a Mac) Mac actions are left
    for the API, and fail only once they're LATE_S late. execute(row, role, name) → (ok, text) is for
    tests. Returns [{id, state, result}]."""
    if db is None and not os.environ.get("SCHEDULED_DB") and os.environ.get("AGENT_TURN_SOURCE") == "test":
        return []   # a test process (the API's loop starts in every TestClient) never runs the real store
    if not _path(db).exists():
        return []
    reap(db, card=card)
    now = now or time.time()
    expire(now, db=db, card=card)
    con = _db(db)
    try:
        due = [_row(r) for r in con.execute("SELECT * FROM scheduled WHERE state='scheduled' AND run_at IS NOT NULL "
                                            "AND run_at<=? ORDER BY run_at", (now,))]
    finally:
        con.close()
    # titan's own first: a Mac that takes minutes to wake mustn't hold up the lights.
    due.sort(key=lambda r: (_is_device(r["tool"]), r["run_at"]))
    out = []
    store = _store(store)
    for r in due:
        late = now - r["run_at"]
        if _is_device(r["tool"]) and device is None and execute is None and late <= LATE_S:
            continue   # the agent API's runner reaches the Mac; this process can't
        if not _transition(r["id"], ("scheduled",), "running", runner=_runner_id(), db=db):
            continue   # cancelled meanwhile, or another runner took it
        out.append(_run_claimed(r, late=late, db=db, device=device, execute=execute, card=card, store=store))
    return out


def _store(store: Any) -> Any:
    if store is not None:
        return store
    import household_identity as hh
    try:
        return hh.get_store()
    except Exception:  # noqa: BLE001
        return None


def _run_claimed(r: dict[str, Any], *, late: float = 0.0, db: Path | None, device, execute, card,
                 store: Any) -> dict[str, Any]:
    """Run one row this runner has just moved to 'running', as its owner; mark it and update its card."""
    person = None
    try:
        person = store.person(r["owner"]) if store is not None else None
    except Exception:  # noqa: BLE001
        person = None
    if late > LATE_S:
        ok, text = False, (f"titan wasn't running then ({row_when(r)}), so it wasn't done "
                           "(it doesn't run actions that late).")
    elif store is not None and person is None:
        ok, text = False, "whoever scheduled it is no longer in the household."
    else:
        role = (person or {}).get("role") or "admin"
        try:
            ok, text = (execute or (lambda row, ro, nm: _execute_as(row, ro, nm, device)))(
                r, role, (person or {}).get("name"))
        except Exception as exc:  # noqa: BLE001 - one action's crash fails it, never the runner
            ok, text = False, f"it failed ({type(exc).__name__}: {str(exc)[:160]})"
    state = "done" if ok else "failed"
    mark(r["id"], state, text, db=db)
    _result_card(r, ok, text, card)
    if r.get("repeat") and (store is None or person is not None):   # #382: a repeating one goes on (not for a gone owner)
        try:
            nxt = _schedule_next(r, db=db)
        except Exception as exc:  # noqa: BLE001 - the run is recorded either way
            nxt = None
            print(f"[scheduled] next of {r['id']} failed: {exc}", file=sys.stderr)
        line = " ".join(str(text or "").split())[:120]
        if nxt:
            (card or _card)(nxt, f"{'Done' if ok else 'Couldn’t do it'}: {r['what']}",
                            f"{line or ('Done.' if ok else 'It failed.')} Next {when_words(float(nxt['run_at']))}.",
                            kind="update" if ok else "alert", urgency=1 if ok else 2,
                            actions=[{"label": "Cancel", "kind": "prompt", "value": f"Cancel scheduled action {nxt['id']}"}])
        else:
            (card or _card)(r, f"Ended: {r['what']}", f"{line or 'Done.'} It was set to repeat until now; schedule it "
                            "again to keep it going.", kind="update", expires_in=24 * 3600)
    return {"id": r["id"], "state": state, "result": text[:200]}


def expire(now: float | None = None, *, db: Path | None = None, card=None) -> int:
    """Place-triggered actions still waiting after 30 days: failed, "never arrived" / "never left"."""
    now = now or time.time()
    con = _db(db)
    try:
        rows = [_row(r) for r in con.execute("SELECT * FROM scheduled WHERE state='scheduled' AND run_at IS NULL "
                                             "AND expires_at<=?", (now,))]
    finally:
        con.close()
    n = 0
    for r in rows:
        if not _transition(r["id"], ("scheduled",), "running", runner=_runner_id(), db=db):
            continue
        t = r["trigger"]
        never = "never arrived" if t["on"] == "arrive" else "never left"
        waited = place_words(t["place"], t["on"]).replace("when you ", "for you to ", 1)
        why = f"It waited 30 days {waited}; that didn't happen, so it wasn't done."
        mark(r["id"], "failed", f"{never}: {why}", db=db)
        _result_card(r, False, why, card, title=f"Couldn't {r['what']} ({never})", urgency=1)
        n += 1
    return n


REGION_ID_RX = re.compile(r"^rg_(sa-[0-9a-f]{8})$")


def region_id(r: dict[str, Any]) -> str:
    return f"rg_{r['id']}"


def regions_for(person: str, now: float | None = None, db: Path | None = None) -> list[dict[str, Any]]:
    """GET /v1/presence/regions (#616): this person's own pending, unexpired region triggers, for their iPhone to
    watch; never anyone else's. Soonest-expiring first (the order the phone keeps when it runs out of slots)."""
    if not person or not _path(db).exists():
        return []
    now = now or time.time()
    con = _db(db)
    try:
        rows = [_row(r) for r in con.execute(
            "SELECT * FROM scheduled WHERE owner=? AND state='scheduled' AND run_at IS NULL AND region IS NOT NULL "
            "AND (expires_at IS NULL OR expires_at>?) ORDER BY expires_at, created_at", (str(person).lower(), now))]
    finally:
        con.close()
    out = []
    for r in rows:
        reg = (r.get("trigger") or {}).get("region")
        if not reg:
            continue
        item = {"id": region_id(r), "label": reg.get("label") or r["trigger"]["place"], "kind": reg.get("kind"),
                "radius_m": reg.get("radius_m"), "on": r["trigger"]["on"], "expires_at": r["expires_at"]}
        item.update({k: reg[k] for k in ("lat", "lon", "query") if reg.get(k) is not None})
        out.append(item)
    return out


def on_place_event(person: str, role: str | None, event: str, place: str, *, now: float | None = None,
                   db: Path | None = None, device=None, execute=None, card=None, store: Any = None,
                   region: str | None = None) -> list[dict[str, Any]]:
    """A person's iPhone reported arriving at or leaving a place: run each of THEIR pending actions
    waiting for exactly that (place, arrive|leave), once each (the same claim as the time runner, so a
    repeated event finds nothing left). Someone else's actions never fire. Returns [{id, state, result}].
    `region` ("rg_sa-…", #616): that one region row too, matched by id and owner."""
    on = {"arrived": "arrive", "left": "leave"}.get(str(event), str(event))
    if on not in ("arrive", "leave") or not person or not str(place or "").strip():
        return []
    if db is None and not os.environ.get("SCHEDULED_DB") and os.environ.get("AGENT_TURN_SOURCE") == "test":
        return []
    if not _path(db).exists():
        return []
    now = now or time.time()
    con = _db(db)
    try:
        rows = [_row(r) for r in con.execute(
            "SELECT * FROM scheduled WHERE state='scheduled' AND run_at IS NULL AND owner=? AND lower(place)=lower(?) "
            "AND on_event=? AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at",
            (str(person).lower(), str(place).strip(), on, now))]
        m = REGION_ID_RX.match(str(region or ""))
        if m and m.group(1) not in {r["id"] for r in rows}:
            rows += [_row(r) for r in con.execute(
                "SELECT * FROM scheduled WHERE id=? AND owner=? AND state='scheduled' AND run_at IS NULL "
                "AND region IS NOT NULL AND on_event=? AND (expires_at IS NULL OR expires_at>?)",
                (m.group(1), str(person).lower(), on, now))]
    finally:
        con.close()
    rows.sort(key=lambda r: _is_device(r["tool"]))   # titan's own first, as in run_due
    store = _store(store)
    out = []
    for r in rows:
        if _transition(r["id"], ("scheduled",), "running", runner=_runner_id(), db=db):
            out.append(_run_claimed(r, db=db, device=device, execute=execute, card=card, store=store))
    return out


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run-due"
    if cmd == "run-due":
        res = run_due()
        for x in res:
            print(f"[scheduled] {x['id']} {x['state']}: {x['result'][:120]}")
        return 0
    if cmd == "list":
        for r in list_for(None, None, include_done=True, since=time.time() - 7 * 86400):
            print(f"{r['id']}  {r['state']:<9} {row_when(r)}  {r['owner']}: {r['what']}  [{r['tool']}]")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
