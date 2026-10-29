#!/usr/bin/env python3
"""The household's day, across people: what clashes and who needs a driver, as one card for the adults.

The calendars Jellybean syncs (connector "calendar" → record nodes of type appointment, with start,
end, calendar and calendar_owner) hold everyone's day. Each event is someone's: the person Settings
› Household assigns its calendar to, else the one the calendar is named for, else the people its
title names ("Pickup Leo"), else the admin for the user's own calendars. From that, facts in code:

  overlap  two of one person's timed events overlap ("Zach CTO 2:00–3:00 runs into Pickup Leo 2:50")
  driver   a kid's event with no grown-up on it, and every grown-up busy then ("Ava: soccer 5:00, and
           Zach and Robin both have something then: who's taking Ava?"); when one is free it's
           named instead of flagged

Who sees what (scripts/audience.py): a person's own overlaps go to that person only (to=[id]); the
driver questions go to the adults (to="adults"), and a grown-up's clashing event shows as "busy"
unless its calendar is one the adults designated as the household's (world.household_calendars) or
the event itself was shared with the household or the adults. The Mac's calendar_owner "shared" is not
that: it means shared *with* the user (a colleague's calendar, a meeting someone else organizes), so it
never names an event to the other adults (10-01, item 9). Each card
is keyed by day so a re-run updates it, with the conflicts as data for the phone's deck. No
conflicts, no card.

  python3 scripts/household_coordination.py [--date YYYY-MM-DD] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

KID_AGE = 16            # younger than this needs a grown-up to get there (role "kid" when no age is set)
LONG_EVENT = timedelta(hours=12)   # longer than this is a state ("Kids with Mom"), not an appointment
BEFORE = timedelta(minutes=30)     # a driver is busy from this long before a kid's event starts


@dataclass
class Event:
    title: str
    start: datetime
    end: datetime
    calendar: str = ""
    shared: bool = False
    location: str = ""
    people: list[str] = field(default_factory=list)   # first names whose event this is
    audience: str = "private"                           # who may see its title (audience.py)

    def ref(self) -> dict[str, Any]:
        out = {"title": self.title, "start": self.start.isoformat(timespec="minutes"), "calendar": self.calendar}
        if self.location:
            out["at"] = self.location
        return out


def _tz():
    import agent_runtime_lib as lib
    return lib._local_now().tzinfo


def _clock(d: datetime) -> str:
    return f"{d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}"


def _parse(value: Any, tz) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(tz)


def fetch_nodes(day: date, tz, get: Callable[[str], dict] | None = None) -> list[dict]:
    """Appointment nodes that may touch `day`: starting up to 7 days before (a multi-day event) to its end."""
    if get is None:
        import agent_runtime_lib as lib
        get = lambda path: lib._runtime_request("record", path, "GET")   # noqa: E731
    lo = datetime.combine(day - timedelta(days=7), time(0), tz).astimezone(timezone.utc)
    hi = datetime.combine(day + timedelta(days=1), time(0), tz).astimezone(timezone.utc)
    path = (f"/v1/nodes?node_type=appointment&field=start&gte={lo:%Y-%m-%dT%H:%M:%SZ}"
            f"&lte={hi:%Y-%m-%dT%H:%M:%SZ}")
    return (get(path) or {}).get("nodes") or []


def household() -> list[dict[str, Any]]:
    """[{id, name, role, age, calendars}] from the household store."""
    import household_identity as hh
    return [{k: p.get(k) for k in ("id", "name", "role", "age", "calendars")} for p in hh.get_store().people()]


def _first(p: dict) -> str:
    return str(p.get("name") or "").split(" ")[0]


def _named(text: str, people: list[dict]) -> list[dict]:
    return [p for p in people if len(_first(p)) >= 2
            and re.search(r"(?<![A-Za-z])" + re.escape(_first(p)) + r"(?![a-z])", text, re.I)]


def _is_kid(p: dict) -> bool:
    age = p.get("age")
    return age < KID_AGE if isinstance(age, int) else p.get("role") == "kid"


def _is_grownup(p: dict) -> bool:
    return p.get("role") in ("admin", "adult") and not _is_kid(p)


def events_for(day: date, nodes: list[dict], people: list[dict], tz,
               household: set[str] | None = None) -> list[Event]:
    """The day's timed appointments (no all-day or multi-day states), each with whose it is, one per
    title and start (the same event on two calendars counts once). `household`: the lowercased titles
    of the calendars designated as the household's (default world.household_calendars())."""
    if household is None:
        import world
        household = world.household_calendars()
    lo = datetime.combine(day, time(0), tz)
    hi = lo + timedelta(days=1)
    admin = next((p for p in people if p.get("role") == "admin"), None)
    hidden_by = {str(p.get("id", "")).lower(): {c.lower() for c in (p.get("calendars_ignored") or [])} for p in people}
    assigned_to = {str(c).lower(): str(p.get("id")) for p in people for c in (p.get("calendars") or [])}
    from world import hidden_here as world_hidden_here
    seen: dict[tuple[str, datetime], Event] = {}
    for n in nodes:
        d = n.get("data") or {}
        if d.get("all_day"):
            continue
        s, e = _parse(d.get("start"), tz), _parse(d.get("end"), tz)
        if not s or not e or not (s < hi and e > lo) or e - s > LONG_EVENT:
            continue
        title = str(d.get("title") or "").strip() or "(untitled)"
        cal = str(d.get("calendar") or "")
        # A calendar the person who synced it has hidden (Jellybean's calendar picker): ignored, unless
        # it's assigned to someone else or is the household's (jellybean#247: hidden from their views only).
        synced_by = str(((d.get("provenance") or {}) if isinstance(d.get("provenance"), dict) else {}).get("owner") or "zach")
        if cal and world_hidden_here(cal, synced_by, hidden_by, assigned_to, household):
            continue
        shared = d.get("calendar_owner") == "shared"
        owner = next((p for p in people if cal.lower() in (c.lower() for c in (p.get("calendars") or []))), None)
        if owner is None:
            named = _named(cal, people)
            owner = named[0] if len(named) == 1 else None
        who = ([owner] if owner else []) + [p for p in _named(title, people) if p is not owner]
        if not who and not shared and admin:
            who = [admin]   # the user's own calendar, naming no one: the user's
        prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
        aud = "household" if cal.lower() in household else str(d.get("audience") or prov.get("audience") or "private")
        ev = seen.get((title.lower(), s))
        if ev is None:
            ev = seen[(title.lower(), s)] = Event(title, s, e, cal, shared, str(d.get("location") or ""),
                                                  audience=aud)
        elif aud in ("household", "adults") and ev.audience not in ("household", "adults"):
            ev.audience = aud   # the same event on a household calendar too: it's the household's
        for p in who:
            if _first(p) not in ev.people:
                ev.people.append(_first(p))
    return sorted(seen.values(), key=lambda x: (x.start, x.title))


def conflicts(events: list[Event], people: list[dict]) -> list[dict[str, Any]]:
    by_name = {_first(p): p for p in people}
    out: list[dict[str, Any]] = []
    # overlap: one person, two events at once
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if b.start >= a.end:
                continue
            for name in (n for n in a.people if n in b.people):
                if by_name.get(name) and _is_kid(by_name[name]) and not any(
                        _is_grownup(by_name.get(x, {})) for x in a.people + b.people):
                    continue   # two kid events: a driver question, below
                out.append({"who": name, "event": a.ref(), "with": b.ref(), "need": "overlap",
                            "text": f"{name}: {a.title} ({_clock(a.start)}–{_clock(a.end)}) runs into "
                                    f"{b.title} ({_clock(b.start)})."})
    # driver: a kid's event no grown-up is on
    grownups = [p for p in people if _is_grownup(p)]
    for ev in events:
        kids = [n for n in ev.people if by_name.get(n) and _is_kid(by_name[n])]
        if not kids or any(_is_grownup(by_name.get(n, {})) for n in ev.people) or not grownups:
            continue
        window = (ev.start - BEFORE, ev.start + timedelta(minutes=15))
        busy = {}
        for g in grownups:
            clash = next((o for o in events if o is not ev and _first(g) in o.people
                          and o.start < window[1] and o.end > window[0]), None)
            if clash:
                busy[_first(g)] = clash
        free = [_first(g) for g in grownups if _first(g) not in busy]
        who = " and ".join(kids)
        if free:
            continue   # someone can take them; not a conflict (the day's list says who's free)
        # The card goes to every adult: a grown-up's own event is named only when its calendar is
        # shared; otherwise it's just "busy".
        said = {n: (o.title if o.audience in ("household", "adults") else None) for n, o in busy.items()}
        first_name, first = next(iter(busy.items()))
        out.append({"who": who, "event": ev.ref(),
                    "with": first.ref() if said[first_name] else {"title": "busy",
                                                                  "start": first.start.isoformat(timespec="minutes")},
                    "need": "driver",
                    "busy": {n: t or "busy" for n, t in said.items()},
                    "text": f"{who}: {ev.title} at {_clock(ev.start)}, and "
                            + " and ".join(f"{n} has {t}" if t else f"{n} is busy" for n, t in said.items())
                            + f" then. Who's taking {who}?"})
    return out


DATA_MAX_BYTES = 4000   # cards keep at most 4 KB of data (#191)


def card_data(found: list[dict]) -> dict[str, Any]:
    """{"conflicts": [...]} for the phone's deck, as many as fit in a card's data; "more" says how
    many didn't (the text still has every one)."""
    items = [{k: v for k, v in c.items() if k != "text"} for c in found]
    while items and len(json.dumps({"conflicts": items, "more": len(found) - len(items)})) > DATA_MAX_BYTES:
        items.pop()
    out: dict[str, Any] = {"conflicts": items}
    if len(items) < len(found):
        out["more"] = len(found) - len(items)
    return out


def card(day: date, found: list[dict]) -> tuple[str, str]:
    n = len(found)
    title = f"{day:%A}: {n} thing{'s' if n != 1 else ''} to sort out"
    return title, "\n".join("• " + c["text"] for c in found)


def run(day: date, dry_run: bool = False, nodes: list[dict] | None = None, people: list[dict] | None = None,
        send: Callable[..., dict] | None = None) -> dict[str, Any]:
    tz = _tz()
    people = household() if people is None else people
    nodes = fetch_nodes(day, tz) if nodes is None else nodes
    found = conflicts(events_for(day, nodes, people, tz), people)
    result: dict[str, Any] = {"date": day.isoformat(), "conflicts": found}
    if not found or dry_run:
        return result
    if send is None:
        import proactive
        send = proactive.add_card
    try:
        import inspect
        takes_data = "data" in inspect.signature(send).parameters
    except (TypeError, ValueError):
        takes_data = False
    # One card per audience, all under the day's key (a card is keyed by key and audience): each
    # person's own overlaps to them, the driver questions to the adults.
    ids = {_first(p): p.get("id") for p in people}
    groups: dict[str, tuple[Any, list[dict]]] = {}
    for c in found:
        if c["need"] == "overlap" and ids.get(c["who"]):
            pid = str(ids[c["who"]])
            groups.setdefault(pid, ([pid], []))[1].append(c)
        else:
            groups.setdefault("adults", ("adults", []))[1].append(c)
    result["cards"] = []
    for to, items in groups.values():
        title, detail = card(day, items)
        kw = {"kind": "suggestion", "urgency": 2, "source": "coordination", "to": to,
              "key": f"coordination-{day.isoformat()}", "expires_in": 20 * 3600}
        if takes_data:
            kw["data"] = card_data(items)
        result["cards"].append(send(title, detail, **kw))
    result["card"] = result["cards"][-1] if result["cards"] else None
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, household time)")
    ap.add_argument("--dry-run", action="store_true", help="print what the card would say; send nothing")
    a = ap.parse_args()
    if a.date:
        day = date.fromisoformat(a.date)
    else:
        import agent_runtime_lib as lib
        day = lib._local_now().date()
    r = run(day, dry_run=a.dry_run)
    if a.dry_run:
        print(json.dumps(r, indent=1, default=str))
    else:
        print(f"{r['date']}: {len(r['conflicts'])} conflict(s)" + (", card sent" if r.get("card") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
