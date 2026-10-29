#!/usr/bin/env python3
"""Arrivals and departures a person's iPhone reports (opt-in, "Always" location; see
notes/location-coordination-2026-09-30.md, v1).

Only a place's name ("home", or one the person named on the phone) and arrived/left, never
coordinates. Each event is the person's own (item 9): shown to them and, for a kid, to their parents (Zach,
10-02: "places only; parents see kids' places"), never to another adult or on a household device. It also lets that
person's place-triggered routines suggest themselves
(routines.on_place_event) and runs their own place-triggered scheduled actions (scheduled.py).
Kept 30 days. Removing one device forgets that device's events; removing the person's last device forgets all of them.

Store: SQLite at $PLACES_DB (default /ai/datasets/connectors/places.db).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("PLACES_DB", "/ai/datasets/connectors/places.db"))
KEEP_DAYS = 30
EVENTS = ("arrived", "left")
_lock = threading.Lock()


REGISTRY = """
CREATE TABLE IF NOT EXISTS places (id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'named',
    owner TEXT, audience TEXT NOT NULL DEFAULT 'household', aliases TEXT NOT NULL DEFAULT '[]', org_entity TEXT,
    sightings INTEGER NOT NULL DEFAULT 0, confirmed_by TEXT, updated_at REAL NOT NULL DEFAULT 0,
    archived_at REAL);
"""


def ensure_registry(con: sqlite3.Connection) -> None:
    """The place registry (relational FE graph §7.2, #1399): named places, no coordinates. kind named | candidate;
    a candidate (a free-text location nobody named) counts its sightings; a person's confirmation (confirmed_by) is
    their own decision and lives here, in a kept store, never in the derived graph."""
    con.executescript(REGISTRY)
    if "archived_at" not in {r[1] for r in con.execute("PRAGMA table_info(places)")}:
        con.execute("ALTER TABLE places ADD COLUMN archived_at REAL")     # #1510: a removed place is archived, not deleted


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("PLACES_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE IF NOT EXISTS place_events (
        id TEXT PRIMARY KEY, person TEXT NOT NULL, event TEXT NOT NULL, place TEXT NOT NULL,
        at TEXT, received_at REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_place_person ON place_events(person, received_at)")
    # Which device sent it (10-01, #421): removing one device forgets only its events. Older rows: NULL.
    if "device" not in {r[1] for r in con.execute("PRAGMA table_info(place_events)")}:
        con.execute("ALTER TABLE place_events ADD COLUMN device TEXT")
    # A scheduled region's arrival (#616, "rg_sa-…"): kept like any other, never one of the person's known places.
    if "region" not in {r[1] for r in con.execute("PRAGMA table_info(place_events)")}:
        try:
            con.execute("ALTER TABLE place_events ADD COLUMN region TEXT")
        except sqlite3.OperationalError:
            pass   # another process added it first
    # A departure waiting out the jitter window (jellybean#666): kept here, so a restart (every deploy) replays it
    # instead of dropping its routine nudge, TV lock and scheduled "leave" actions.
    con.execute("CREATE TABLE IF NOT EXISTS held_departures (id TEXT PRIMARY KEY, person TEXT NOT NULL, role TEXT, "
                "place TEXT NOT NULL, region TEXT, left_at REAL NOT NULL, due_at REAL NOT NULL)")
    return con


def clean_place(place: object) -> str:
    p = " ".join(str(place or "").split())
    if not p or len(p) > 40:
        raise ValueError("place must be 1-40 characters")
    return "home" if p.lower() == "home" else p


def record(event_id: str, person: str, event: str, place: str, at: str | None,
           db: Path | None = None, device: str | None = None, region: str | None = None) -> bool:
    """Store one event; False when that id was already received (the phone's retry)."""
    if event not in EVENTS:
        raise ValueError("event must be arrived or left")
    eid = str(event_id or "").strip()[:64]
    if not eid:
        raise ValueError("id is required")
    with _lock:
        con = _db(db)
        try:
            con.execute("DELETE FROM place_events WHERE received_at < ?", (time.time() - KEEP_DAYS * 86400,))
            cur = con.execute("INSERT OR IGNORE INTO place_events (id, person, event, place, at, received_at, device, region) "
                              "VALUES (?,?,?,?,?,?,?,?)", (eid, person, event, clean_place(place), at, time.time(), device,
                                                           str(region)[:64] if region else None))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


# A "left" undone by an "arrived" at the same place within this long is the phone's geofence jitter, not a trip
# (jellybean#666, 10-04: "Left the house" while nobody had).
JITTER_S = int(os.environ.get("PLACES_JITTER_S", "300"))   # 0: no hold (tests that need a departure at once)


def event_ts(ev: dict) -> float:
    """When it happened: the phone's own `at` (ISO with its offset), else when titan received it. Ordering by
    received_at made a late or retried event the "latest" (jellybean#666)."""
    import local_time
    t = local_time.parse_iso(ev.get("at"))
    if t is not None:
        if t.tzinfo is None:
            t = t.replace(tzinfo=local_time.zone())
        return t.timestamp()
    return float(ev.get("received_at") or 0)


def recent(person: str, limit: int = 20, db: Path | None = None) -> list[dict]:
    """A person's own recent events (never anyone else's), newest first by when they happened."""
    con = _db(db)
    try:
        rows = con.execute("SELECT id, event, place, at, received_at FROM place_events WHERE person=?",
                           (person,)).fetchall()   # 30 days of one person's arrivals and departures: small
    finally:
        con.close()
    evs = [dict(zip(("id", "event", "place", "at", "received_at"), r)) for r in rows]
    evs.sort(key=lambda e: (event_ts(e), float(e["received_at"] or 0)), reverse=True)
    return evs[:limit]


def settled(evs: list[dict]) -> list[dict]:
    """Newest-first events without jitter: a "left" followed within JITTER_S by an "arrived" at the same place drops,
    with that arrival (the person never went anywhere)."""
    drop: set[str] = set()
    for i, ev in enumerate(evs):
        if ev["event"] != "arrived" or ev["id"] in drop:
            continue
        for prev in evs[i + 1:]:
            if prev["id"] in drop:
                continue
            if event_ts(ev) - event_ts(prev) > JITTER_S:
                break
            if prev["event"] == "left" and prev["place"].lower() == ev["place"].lower():
                drop.update((ev["id"], prev["id"]))
                break
    return [e for e in evs if e["id"] not in drop]


def where_now(person: str, db: Path | None = None) -> dict | None:
    """A person's latest place event ({event, place, at, received_at}), None without one; jitter skipped. For their own
    view and, for a kid, their parents' (Zach, 10-02: places only, parents see kids' places; jellybean#186/#308)."""
    got = settled(recent(person, limit=50, db=db))
    return got[0] if got else None


def still_away(person: str, place: str, left_at: float, db: Path | None = None) -> bool:
    """Whether a "left" at `left_at` still stands: no arrival at that place since (a jitter pair cancels a held
    departure's routine nudge, TV lock and scheduled actions)."""
    for ev in recent(person, limit=50, db=db):
        if event_ts(ev) < left_at - 1:
            break
        if ev["event"] == "arrived" and ev["place"].lower() == str(place).lower():
            return False
    return True


def when_text(ev: dict, now: float | None = None) -> str:
    """A person-readable time for an event, from code (the model restated raw ISO wrongly), as a phrase to follow
    "left home": "at 8:12 AM today (3 h ago)", "yesterday at 9:04 PM", "on Fri Oct 2 at 7:10 AM"."""
    import local_time
    from datetime import datetime
    z = local_time.zone()
    n = datetime.fromtimestamp(now if now is not None else time.time(), z)
    t = datetime.fromtimestamp(event_ts(ev), z)
    clock = t.strftime("%I:%M %p").lstrip("0")
    if t.date() == n.date():
        return f"at {clock} today ({local_time.ago(t, n)})"
    if (n.date() - t.date()).days == 1:
        return f"yesterday at {clock}"
    return f"on {t:%a %b} {t.day} at {clock}"


def viewable(viewer: str | None, role: str | None, people: list[dict]) -> list[str]:
    """Whose places this viewer may see: their own, plus, for a parent, their kids'. Never another adult's, never on
    a shared household device."""
    if not viewer or role == "household":
        return []
    me = viewer.lower()
    kids = [p["id"] for p in people if p.get("role") == "kid" and me in [str(x).lower() for x in (p.get("parents") or [])]]
    return [me] + [k for k in kids if k != me]


def known(person: str, db: Path | None = None, named: list[str] | None = None) -> list[str]:
    """The places titan knows for a person: home, the places they named on their iPhone (PATCH
    /v1/me {places}), then any other place their iPhone has reported (as last spelled)."""
    if named is None:
        try:
            import household_identity as hh
            named = hh.get_store().places(person)
        except Exception:  # noqa: BLE001 - no household store here: events only
            named = []
    con = _db(db)
    try:
        rows = con.execute("SELECT place, MAX(received_at) FROM place_events WHERE person=? AND region IS NULL "
                           "GROUP BY lower(place) "
                           "ORDER BY MAX(received_at) DESC", (person,)).fetchall()
    finally:
        con.close()
    out = ["home"]
    for name in [*named, *(r[0] for r in rows)]:
        if name and name.lower() not in (o.lower() for o in out):
            out.append(name)
    return out


MODES = ("always", "while_using", "off")


def _modes(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS location_modes (device TEXT PRIMARY KEY, person TEXT NOT NULL, "
                "device_name TEXT, value TEXT NOT NULL, precise INTEGER, at REAL NOT NULL)")


def set_mode(person: str, device: str, device_name: str | None, value: object, precise: object,
             db: Path | None = None, now: float | None = None) -> dict:
    """A phone's location permission as it reports it (#1133): always | while_using | off, and precise or not. One
    row per device; only that device's own person ever reads it."""
    v = str(value or "").strip().lower().replace(" ", "_")
    if v not in MODES:
        raise ValueError(f"value is one of: {', '.join(MODES)}")
    if precise is not None and not isinstance(precise, bool):
        raise ValueError("precise is true, false or absent")
    with _lock:
        con = _db(db)
        try:
            _modes(con)
            con.execute("INSERT OR REPLACE INTO location_modes VALUES (?,?,?,?,?,?)",
                        (device, person, device_name, v, None if precise is None else int(precise),
                         time.time() if now is None else now))
            con.commit()
        finally:
            con.close()
    return mode_of(person, db) or {}


def mode_of(person: str, db: Path | None = None) -> dict | None:
    """The person's latest reported mode, from whichever of their own devices reported last; None until one has."""
    con = _db(db)
    try:
        _modes(con)
        r = con.execute("SELECT value, precise, device_name, at FROM location_modes WHERE person=? ORDER BY at DESC "
                        "LIMIT 1", (person,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    from datetime import datetime, timezone
    return {"value": r[0], "precise": None if r[1] is None else bool(r[1]), "device": r[2],
            "at": datetime.fromtimestamp(r[3], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def my_places(person: str, db: Path | None = None, now: float | None = None) -> list[dict]:
    """#1133: the places a person has arrived at (named places, not scheduled regions), each with its latest arrival,
    newest first: [{place, last_arrived (ISO), when}]. Their own events only; names as stored, never coordinates."""
    con = _db(db)
    try:
        rows = con.execute("SELECT id, event, place, at, received_at FROM place_events WHERE person=? AND "
                           "event='arrived' AND region IS NULL", (person,)).fetchall()
    finally:
        con.close()
    latest: dict[str, dict] = {}
    for r in rows:
        ev = dict(zip(("id", "event", "place", "at", "received_at"), r))
        k = str(ev["place"]).lower()
        if k not in latest or event_ts(ev) > event_ts(latest[k]):
            latest[k] = ev
    from datetime import datetime, timezone
    out = [{"place": ev["place"], "last_arrived": datetime.fromtimestamp(event_ts(ev), timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"), "when": when_text(ev, now)}
           for ev in sorted(latest.values(), key=event_ts, reverse=True)]
    return out


def forget_device(person: str, device: str, db: Path | None = None) -> int:
    """One device removed while the person keeps others: only that device's events go (10-01, #421: removing
    an old iPad wiped the phone's place history). Events from before devices were recorded stay."""
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM place_events WHERE person=? AND device=?", (person, device)).rowcount
            _modes(con)
            con.execute("DELETE FROM location_modes WHERE device=? AND person=?", (device, person))
            con.commit()
            return n
        finally:
            con.close()


def forget_person(person: str, db: Path | None = None) -> int:
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM place_events WHERE person=?", (person,)).rowcount
            _modes(con)
            con.execute("DELETE FROM location_modes WHERE person=?", (person,))   # #1133: their phones' modes too
            con.commit()
            return n
        finally:
            con.close()


# A held departure whose window passed this long ago (the API was down for hours) is dropped, not run late: a "time to
# leave" nudge or a scheduled leave action an hour after the fact is wrong.
HELD_STALE_S = 3600


def hold(event_id: str, person: str, role: str | None, place: str, region: str | None, left_at: float, due_at: float,
         db: Path | None = None) -> None:
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO held_departures VALUES (?,?,?,?,?,?,?)",
                        (event_id, person, role, place, region, left_at, due_at))
            con.commit()
        finally:
            con.close()


def held(db: Path | None = None) -> list[dict]:
    """Every departure still waiting (for the API's startup replay)."""
    con = _db(db)
    try:
        rows = con.execute("SELECT id, person, role, place, region, left_at, due_at FROM held_departures").fetchall()
    finally:
        con.close()
    return [dict(zip(("id", "person", "role", "place", "region", "left_at", "due_at"), r)) for r in rows]


def take_held(event_id: str, db: Path | None = None) -> bool:
    """Claim a held departure to act on it: True once, for whoever takes it first (the live task or a replay)."""
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM held_departures WHERE id=?", (event_id,)).rowcount
            con.commit()
            return n > 0
        finally:
            con.close()
