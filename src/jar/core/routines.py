#!/usr/bin/env python3
"""Routines: named, ordered steps the household runs together ("Goodnight": lock up, lights off,
set the alarm). The design's Routines view, rehearsal and one-tap run.

titan stores them (SQLite, ROUTINES_DB, default /ai/datasets/connectors/routines.db) and, at a
routine's scheduled time, raises a proactive card with a "Run it" button. Nothing runs unattended:
running a routine is an ordinary chat turn ("Run my routine …" with its steps), so every step goes
through Jellybean's trust tiers and confirmation cards like any other request.

Shapes: {id, name, icon, steps: [str], schedule: {"time": "HH:MM", "days": [0-6]} | null,
         enabled, created_at, updated_at, last_nudged, owner, audience}  (days: 0 = Monday, local time).

Who (scripts/audience.py): a routine has an owner (who made it; none for ones from before owners)
and an audience: "household" (the default, and every routine made before audiences), "private"
(the owner's own), "adults" or a list of ids. Only those it's for see it and get its "Time for …"
card. Changing or deleting it: its owner, the admin, or a shared household device when it's the
household's.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("ROUTINES_DB", "/ai/datasets/connectors/routines.db"))
MAX_STEPS = 12
ICONS = {"moon", "sun", "house", "car", "briefcase", "sparkles", "bell", "bed", "cup", "leaf"}
_lock = threading.Lock()
_TIME_RX = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS routines (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT, steps TEXT NOT NULL, schedule TEXT,
        enabled INTEGER NOT NULL DEFAULT 1, created_at REAL, updated_at REAL, last_nudged TEXT)""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(routines)")]
    for col in ("owner", "audience", "trigger", "last_place_nudge"):
        if col not in cols:
            con.execute(f"ALTER TABLE routines ADD COLUMN {col} TEXT")
    return con


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "icon": r["icon"] or "sparkles", "steps": json.loads(r["steps"]),
            "schedule": json.loads(r["schedule"]) if r["schedule"] else None, "enabled": bool(r["enabled"]),
            "created_at": r["created_at"], "updated_at": r["updated_at"], "last_nudged": r["last_nudged"],
            "owner": r["owner"], "audience": r["audience"] or "household",
            "trigger": json.loads(r["trigger"]) if r["trigger"] else None}


def visible(r: dict[str, Any], person: str | None, role: str | None) -> bool:
    """Whether a person sees a routine (person and role None: titan itself sees all)."""
    import audience
    return audience.visible(r.get("audience") or "household", person, role, owner=r.get("owner"))


def can_edit(r: dict[str, Any], person: str | None, role: str | None) -> bool:
    """Its owner, the admin, or a household device for a household routine (None/None: titan)."""
    if person is None and role is None:
        return True
    if role == "admin":
        return True
    if r.get("owner") and person and str(r["owner"]).lower() == str(person).lower():
        return True
    return role == "household" and (r.get("audience") or "household") == "household"


def validate(body: dict[str, Any]) -> dict[str, Any]:
    """Normalise a routine from a request or a tool call; raises ValueError with a readable reason."""
    name = " ".join(str(body.get("name") or "").split())[:60]
    if not name:
        raise ValueError("a routine needs a name")
    raw = body.get("steps") or []
    if isinstance(raw, str):
        raw = [s for s in re.split(r"\n|;", raw)]
    # Drop a leading list marker ("1.", "2)", "-", "•") only; "6:45" at the end must survive.
    steps = [" ".join(re.sub(r"^\s*(?:[-•*]+|\d+[.)])\s*", "", str(s)).split())[:200] for s in raw]
    steps = [s for s in steps if s][:MAX_STEPS]
    if not steps:
        raise ValueError("a routine needs at least one step")
    icon = str(body.get("icon") or "sparkles")
    icon = icon if icon in ICONS else "sparkles"
    sched = body.get("schedule")
    if sched in (None, "", {}):
        sched = None
    else:
        if not isinstance(sched, dict) or not _TIME_RX.match(str(sched.get("time") or "")):
            raise ValueError("schedule needs a time like 22:30")
        days = sched.get("days")
        days = sorted({int(d) for d in days if 0 <= int(d) <= 6}) if days else list(range(7))
        sched = {"time": str(sched["time"]), "days": days}
    trig = body.get("trigger")
    if trig in (None, "", {}):
        trig = None
    else:
        if not isinstance(trig, dict) or trig.get("type", "place") != "place":
            raise ValueError("a trigger is {type: place, place, on: arrive|leave}")
        on = str(trig.get("on") or "arrive").strip().lower()
        on = {"arrived": "arrive", "left": "leave", "leaving": "leave", "arriving": "arrive"}.get(on, on)
        if on not in ("arrive", "leave"):
            raise ValueError("a place trigger is on arrive or leave")
        import places
        trig = {"type": "place", "place": places.clean_place(trig.get("place")), "on": on}
    return {"name": name, "icon": icon, "steps": steps, "schedule": sched, "trigger": trig,
            "enabled": body.get("enabled", True) is not False}


_KEPT = ("name", "icon", "steps", "schedule", "trigger", "enabled")


def save(body: dict[str, Any], db: Path | None = None, *, person: str | None = None,
         role: str | None = None) -> dict[str, Any]:
    """Create, or update when body has an existing id (or a routine with the same name exists that
    this person sees). A new routine is the person's, for body["audience"] (default "household").
    Updating one they may not change raises PermissionError; an update keeps the owner, and widens
    the audience only when body asks for one (audience.widen), and keeps any field body leaves out."""
    import audience
    now = time.time()
    with _lock:
        con = _db(db)
        try:
            rid = str(body.get("id") or "")
            if not rid:
                name = " ".join(str(body.get("name") or "").split())[:60]
                hits = [_row(h) for h in con.execute("SELECT * FROM routines WHERE lower(name)=lower(?)", (name,))]
                hit = next((h for h in hits if visible(h, person, role)), None)
                rid = hit["id"] if hit else "rt-" + uuid.uuid4().hex[:10]
                if hit:
                    body = {**body, "name": hit["name"]}   # matched by name: keep how it was first spelled
            old = con.execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone()
            if old:
                # An update keeps what the body leaves out; an explicit null still clears (a schedule, a trigger).
                # 10-03: the Mac's routine editor has no trigger field, so every edit there cleared a place trigger.
                cur = _row(old)
                body = {**{k: cur.get(k) for k in _KEPT}, **body}
            r = validate(body)
            if old:
                if not visible(cur, person, role) or not can_edit(cur, person, role):
                    raise PermissionError(f"“{cur['name']}” isn't yours to change; ask whoever made it.")
                aud = audience.widen(cur["audience"], body["audience"]) if body.get("audience") else cur["audience"]
                con.execute("UPDATE routines SET name=?, icon=?, steps=?, schedule=?, enabled=?, updated_at=?, "
                            "audience=?, trigger=? WHERE id=?",
                            (r["name"], r["icon"], json.dumps(r["steps"]), json.dumps(r["schedule"]) if r["schedule"] else None,
                             int(r["enabled"]), now, aud, json.dumps(r["trigger"]) if r["trigger"] else None, rid))
            else:
                aud = audience.normalize(body.get("audience"), default="household")
                con.execute("INSERT INTO routines (id, name, icon, steps, schedule, enabled, created_at, updated_at, "
                            "owner, audience, trigger) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (rid, r["name"], r["icon"], json.dumps(r["steps"]),
                             json.dumps(r["schedule"]) if r["schedule"] else None, int(r["enabled"]), now, now,
                             str(person).lower() if person else None, aud,
                             json.dumps(r["trigger"]) if r["trigger"] else None))
            con.commit()
            return _row(con.execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone())
        finally:
            con.close()


def list_all(db: Path | None = None, *, person: str | None = None, role: str | None = None) -> list[dict[str, Any]]:
    """The routines this person sees (no person and role: all of them)."""
    con = _db(db)
    try:
        return [x for x in (_row(r) for r in con.execute("SELECT * FROM routines ORDER BY lower(name)"))
                if visible(x, person, role)]
    finally:
        con.close()


def get(rid: str, db: Path | None = None, *, person: str | None = None, role: str | None = None) -> dict[str, Any] | None:
    con = _db(db)
    try:
        rows = [_row(r) for r in con.execute("SELECT * FROM routines WHERE id=? OR lower(name)=lower(?)", (rid, rid))]
        return next((r for r in rows if visible(r, person, role)), None)
    finally:
        con.close()


_FILLER = {"routine", "the", "my", "our", "a", "an"}


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(name).lower()) if w not in _FILLER}


def similar(name: str, db: Path | None = None, *, person: str | None = None,
            role: str | None = None) -> list[dict[str, Any]]:
    """Routines this person sees that are probably the one they mean by another spelling: one
    name's words all inside the other's. On 10-03 a phone edit of "Nightlight dinner log" came in as
    "Dinner Log", missed the exact-name match and made a second (household) routine."""
    w = _words(name)
    if not w:
        return []
    return [r for r in list_all(db, person=person, role=role)
            if (ws := _words(r["name"])) and (w <= ws or ws <= w)]


def delete(rid: str, db: Path | None = None, *, person: str | None = None, role: str | None = None) -> bool:
    """False when there's no such routine this person sees; PermissionError when they see it but may
    not delete it."""
    with _lock:
        con = _db(db)
        try:
            row = con.execute("SELECT * FROM routines WHERE id=?", (rid,)).fetchone()
            if not row or not visible(_row(row), person, role):
                return False
            if not can_edit(_row(row), person, role):
                raise PermissionError(f"“{row['name']}” isn't yours to delete; ask whoever made it.")
            cur = con.execute("DELETE FROM routines WHERE id=?", (rid,))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def run_prompt(r: dict[str, Any]) -> str:
    """The chat message that runs a routine (what "Run it" sends)."""
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(r["steps"], 1))
    return (f"Run my routine “{r['name']}” now, step by step, in this order, and tell me briefly how each "
            f"step went:\n{steps}")


_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def days_text(days: list[int] | None) -> str:
    """"daily", "weekdays", "weekends", "Mon, Wed, Fri" (days: 0 = Monday)."""
    d = sorted(set(days or range(7)))
    if d == list(range(7)):
        return "daily"
    if d == list(range(5)):
        return "weekdays"
    if d == [5, 6]:
        return "weekends"
    return ", ".join(_DAY_NAMES[i] for i in d)


def next_run(r: dict[str, Any], now: datetime) -> datetime | None:
    """When a scheduled, enabled routine next nudges (its time on its next day, household time)."""
    s = r.get("schedule")
    if not (s and r.get("enabled", True)):
        return None
    try:
        hh, mm = map(int, str(s["time"]).split(":"))
    except (KeyError, ValueError):
        return None
    days = set(s.get("days") or range(7))
    for k in range(8):
        d = (now + timedelta(days=k)).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if d.weekday() in days and d > now:
            return d
    return None


def _span(seconds: float) -> str:
    """"5 min", "2 h", "8 h 30 min"."""
    s = int(max(0, seconds))
    h, mi = s // 3600, (s % 3600) // 60
    return f"{h} h {mi} min" if h and mi else f"{h} h" if h else f"{mi} min"


def describe(r: dict[str, Any], now: datetime) -> str:
    """One routine for the model, every fact worked out here (10-01: the list said "daily" for a
    weekdays routine, never said when it runs next or that it was paused, and steps weren't numbered):
      - Goodnight (weekdays at 22:30 · next Thu Oct 1, 10:30 PM, in 8 h): 1. Lock all doors; 2. Turn off the lights
      - Morning (daily at 06:45 · paused): 1. Start the coffee
      - Arrive home (when you arrive at Home): 1. Lights on"""
    notes = []
    s = r.get("schedule")
    if s:
        notes.append(f"{days_text(s.get('days'))} at {s['time']}")
        if not r.get("enabled", True):
            notes.append("paused")
        else:
            n = next_run(r, now)
            if n:
                when = "today" if n.date() == now.date() else "tomorrow" if (n.date() - now.date()).days == 1 else f"{n:%a %b} {n.day}"
                notes.append(f"next {when}, {n.strftime('%I:%M %p').lstrip('0')}"
                             + (f", in {_span((n - now).total_seconds())}" if n.date() == now.date() else ""))
    t = r.get("trigger")
    if t and t.get("place"):
        notes.append(f"when you {'arrive at' if t.get('on', 'arrive') == 'arrive' else 'leave'} {t['place']}")
    if not notes:
        notes.append("not scheduled")
    steps = "; ".join(f"{i}. {x}" for i, x in enumerate(r.get("steps") or [], 1))
    return f"- {r['name']} ({' · '.join(notes)}): {steps}"


def due(now: datetime, db: Path | None = None) -> list[dict[str, Any]]:
    """Enabled routines whose time is now (this minute or up to 10 minutes late, e.g. after a
    restart) and that haven't been nudged today."""
    today = now.strftime("%Y-%m-%d")
    out = []
    for r in list_all(db):
        s = r["schedule"]
        if not (r["enabled"] and s) or r["last_nudged"] == today or now.weekday() not in s["days"]:
            continue
        hh, mm = map(int, s["time"].split(":"))
        late = (now.hour * 60 + now.minute) - (hh * 60 + mm)
        if 0 <= late <= 10:
            out.append(r)
    return out


def nudge_due(now: datetime | None = None, db: Path | None = None, *, card=None) -> int:
    """Raise a "Time for …" card for each due routine (once per day). Returns how many."""
    import agent_runtime_lib as lib
    now = now or lib._local_now()
    n = 0
    for r in due(now, db):
        (card or _card)(r)
        with _lock:
            con = _db(db)
            try:
                con.execute("UPDATE routines SET last_nudged=? WHERE id=?", (now.strftime("%Y-%m-%d"), r["id"]))
                con.commit()
            finally:
                con.close()
        n += 1
    return n


def card_audience(r: dict[str, Any]) -> Any:
    """Who a routine's "Time for …" card goes to: everyone it's for (a private one: its owner;
    a list: those people and the owner; an ownerless private one: the admin)."""
    aud = r.get("audience") or "household"
    owner = r.get("owner")
    if aud == "private":
        return [owner] if owner else "admins"
    if aud.startswith("["):
        return sorted(set(json.loads(aud)) | ({owner} if owner else set()))
    return aud


def _card(r: dict[str, Any]) -> None:
    import proactive
    steps = "; ".join(r["steps"][:4]) + ("…" if len(r["steps"]) > 4 else "")
    proactive.add_card(f"Time for {r['name']}", f"{len(r['steps'])} steps: {steps}", kind="suggestion", urgency=2,
                       source="routine", action_prompt=run_prompt(r), to=card_audience(r),
                       actions=[{"label": "Run it", "kind": "prompt", "value": run_prompt(r)},
                                {"label": "Not tonight", "kind": "dismiss"}])


PLACE_DEBOUNCE_S = 600


def on_place_event(person: str, role: str | None, event: str, place: str, db: Path | None = None, *,
                   card=None, now: float | None = None) -> int:
    """A person arrived at or left a place: suggest each enabled routine with that place trigger
    that is theirs or that they share in. A personal routine's card goes to them only; a household
    routine's card goes to its audience and never says who arrived (item 9). Returns how many."""
    on = {"arrived": "arrive", "left": "leave"}.get(event, event)
    place = str(place or "").strip().lower()
    now = now or time.time()
    n = 0
    for r in list_all(db, person=person, role=role):
        t = r.get("trigger") or {}
        if not (r["enabled"] and t.get("type") == "place" and t.get("on") == on
                and str(t.get("place", "")).lower() == place):
            continue
        con = _db(db)
        try:
            last = con.execute("SELECT last_place_nudge FROM routines WHERE id=?", (r["id"],)).fetchone()[0]
        finally:
            con.close()
        if last and now - float(last) < PLACE_DEBOUNCE_S:
            continue
        personal = (r.get("audience") or "household") in ("private",) or str(r.get("audience", "")).startswith("[")
        (card or _place_card)(r, [person] if personal else card_audience(r))
        with _lock:
            con = _db(db)
            try:
                con.execute("UPDATE routines SET last_place_nudge=? WHERE id=?", (str(now), r["id"]))
                con.commit()
            finally:
                con.close()
        n += 1
    return n


def _place_card(r: dict[str, Any], to: Any) -> None:
    import proactive
    steps = "; ".join(r["steps"][:4]) + ("…" if len(r["steps"]) > 4 else "")
    proactive.add_card(f"Time for {r['name']}", f"{len(r['steps'])} steps: {steps}", kind="suggestion", urgency=2,
                       source="routine", action_prompt=run_prompt(r), to=to, key=f"place-{r['id']}",
                       expires_in=3600,
                       actions=[{"label": "Run it", "kind": "prompt", "value": run_prompt(r)},
                                {"label": "Not now", "kind": "dismiss"}])
