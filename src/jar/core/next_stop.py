#!/usr/bin/env python3
"""Next stop, on leaving a place (titan-node#513, the slice Coordination approved 10-05; no new location data).

When a person's own iPhone reports leaving a place (POST /v1/presence/place, after its jitter hold), and THEIR OWN
calendar has a timed event with a location starting within the next 3 hours that no trip Live Activity already
covers, one card goes to them only: "Next: <title> at <time>", the location under it, and a button that asks their
iPhone how long it'll take (the existing travel_time, run on the phone). Titan sends nothing to anyone else and keeps
no location: only "carded" per event key (a hash), dropped a day after the event.

  their own   an appointment the person synced themselves, on a calendar that is theirs: never one merely shared
              with them (calendar_owner "shared"), never one assigned to someone else, never one they've hidden
              (the world-model gotcha, 10-05). A household-calendar event only when it's a plan they made or are in
              (#1363, Coordination 10-09: event_proposals.goers; each adult gets their own from their own departure,
              a kid none), else skipped as before
  who         adults and the admin; kids are Zach's open question (#513 triage); never a shared screen
  when        once per event; not in quiet hours
  modes       NEXT_STOP_CARDS=0 (off, default), dry (log that a card would go, never a title or place), 1 (send)
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

AHEAD = timedelta(hours=3)
OWNERS = frozenset({"admin", "adult"})


def mode() -> str:
    m = os.environ.get("NEXT_STOP_CARDS", "0").strip().lower()
    return m if m in ("0", "dry", "1") else "0"


def _db(db: Path | None = None):
    import places
    con = places._db(db)
    con.execute("CREATE TABLE IF NOT EXISTS next_stop_cards (key TEXT PRIMARY KEY, person TEXT NOT NULL, "
                "start REAL NOT NULL, at REAL NOT NULL)")
    return con


def _parse(v: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def own(person: str, d: dict[str, Any], people: list[dict[str, Any]], household: set[str],
        goers: Callable[[dict[str, Any], list[dict[str, Any]]], set[str]] | None = None) -> bool:
    """The event is on one of this person's own calendars (see the module's note), or it's a household-calendar event
    from a plan they made or are in (event_proposals.goers: its poster and the adults it names; never a kid)."""
    cal = str(d.get("calendar") or "").strip().lower()
    if cal and cal in household:
        if goers is None:
            import event_proposals
            goers = event_proposals.goers
        try:
            return person in goers(d, people)
        except Exception:  # noqa: BLE001 - no plan to read: a household event stays skipped
            return False
    prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
    if str(prov.get("owner") or "") != person or d.get("calendar_owner") == "shared":
        return False
    assigned = {str(c).lower(): str(p.get("id")) for p in people for c in (p.get("calendars") or [])}
    if cal and assigned.get(cal, person) != person:
        return False
    me = next((p for p in people if str(p.get("id")) == person), {})
    if cal and cal in {str(c).lower() for c in (me.get("calendars_ignored") or [])}:
        return False
    return True


def _fetch(now: datetime) -> list[dict[str, Any]]:
    import agent_runtime_lib as lib
    path = (f"/v1/nodes?node_type=appointment&field=start&gte={now:%Y-%m-%dT%H:%M:%SZ}"
            f"&lte={(now + AHEAD):%Y-%m-%dT%H:%M:%SZ}")
    return (lib._runtime_request("record", path, "GET") or {}).get("nodes") or []


def _trip_running(person: str, source_id: str) -> bool:
    import push
    import trip_push
    con = push._db()
    try:
        return con.execute("SELECT 1 FROM activity_tokens WHERE person=? AND ref=? LIMIT 1",
                           (person, trip_push.ref_for(source_id))).fetchone() is not None
    finally:
        con.close()


def on_left(person: str, role: str | None, *, now: datetime | None = None, nodes: list[dict] | None = None,
            people: list[dict] | None = None, household: set[str] | None = None,
            trip_running: Callable[[str, str], bool] | None = None, quiet: bool | None = None,
            send: Callable[..., Any] | None = None, db: Path | None = None, log: Callable[[str], None] | None = None,
            how: str | None = None, tz: Any = None,
            goers: Callable[[dict[str, Any], list[dict[str, Any]]], set[str]] | None = None) -> dict[str, int]:
    """A departure: card the person's next own located event, if there is one. → counts only."""
    how = how or mode()
    stats = {"events": 0, "sent": 0, "would_send": 0, "already": 0, "quiet": 0}
    if how == "0" or role not in OWNERS or not person:
        return stats
    now = now or datetime.now(timezone.utc)
    con = _db(db)
    try:   # keys go a day after their event, whatever happens on this departure
        con.execute("DELETE FROM next_stop_cards WHERE start < ?", (now.timestamp() - 86400,))
        con.commit()
    finally:
        con.close()
    log = log or (lambda s: print(s, file=sys.stderr))
    if people is None:
        import household_identity as hh
        people = [dict(p) for p in hh.get_store().people()]
    if household is None:
        import world
        household = {c.lower() for c in world.household_calendars()}
    nodes = _fetch(now) if nodes is None else nodes
    trip_running = trip_running or _trip_running
    picks = []
    for n in nodes:
        d = n.get("data") or {}
        start = _parse(d.get("start"))
        loc = " ".join(str(d.get("location") or "").split())
        if d.get("all_day") or not start or not loc or not (now < start <= now + AHEAD):
            continue
        if not own(person, d, people, household, goers):
            continue
        sid = str(((d.get("provenance") or {}) if isinstance(d.get("provenance"), dict) else {}).get("source_id") or "")
        if sid and trip_running(person, sid):
            continue
        picks.append((start, str(d.get("title") or "").strip() or "your next event", loc, sid or str(n.get("node_id"))))
    if not picks:
        return stats
    start, title, loc, sid = min(picks)
    stats["events"] = 1
    key = hashlib.sha1(f"{person}\0{sid}\0{start.isoformat()}".encode()).hexdigest()[:16]
    con = _db(db)
    try:
        if con.execute("SELECT 1 FROM next_stop_cards WHERE key=?", (key,)).fetchone():
            stats["already"] = 1
            con.commit()
            return stats
        if how == "dry":
            mins = int((start - now).total_seconds() // 60)
            log(f"[next-stop dry] would card: an own event with a location in {mins} min")   # never its title or place
            stats["would_send"] = 1
        else:
            if quiet is None:
                import agent_runtime_lib as lib
                import mail_bills
                quiet = mail_bills._quiet(now.astimezone(lib._local_now().tzinfo))   # the passed now, in the household zone (#1515)
            if quiet:
                stats["quiet"] = 1
                con.commit()
                return stats
            if send is None:
                import proactive
                send = proactive.add_card
            if tz is None:
                import agent_runtime_lib as lib
                tz = lib._local_now().tzinfo
            local = start.astimezone(tz)
            clock = f"{local:%-I:%M %p}".replace(":00 ", " ")
            send(f"Next: {title} at {clock}", loc, kind="info", urgency=1, source="next-stop", to=[person],
                 key=f"next-stop:{key}", expires_in=max(600.0, (start - now).total_seconds()),
                 actions=[{"id": "eta", "label": "How long?", "kind": "prompt",
                           "value": f"How long will it take me to get to {loc}?"}],
                 data={"kind": "next_stop", "start": start.isoformat()})
            stats["sent"] = 1
        con.execute("INSERT OR REPLACE INTO next_stop_cards VALUES (?,?,?,?)", (key, person, start.timestamp(), time.time()))
        con.commit()
    finally:
        con.close()
    return stats
