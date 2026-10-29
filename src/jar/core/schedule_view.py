#!/usr/bin/env python3
"""Household schedules, per viewer: the events titan holds that this person may see, grouped by whose they
are (zachshallbetter/jellybean#219: a "people" filter in the calendar views). Contract: docs/schedule-api.md.

Events come from the calendar items the household's devices sync (connectors_runner: record nodes of type
appointment, owner = the person whose device sent them, audience private). An event is shown to a viewer
when any of these holds, and every fence it touches admits them (fences.py):
  - it's theirs (their device synced it, or it was shared with them: audience.visible);
  - its calendar is one of the household's (world.household_calendars, set by the adults, #335): everyone;
  - its calendar is assigned to them in Settings › Household (person_for_calendar);
  - its calendar is assigned to their child: parents see and manage their kids' schedules (Zach, 10-01, #354).
A shared household device (no person) sees the household calendars only.

Whose an event is: the person its calendar is assigned to, else "household" for a household calendar, else
the person whose device synced it. The same event synced by two people's devices is shown once.

Who manages it (`managed_by`, jellybean#206/#172: cards and notifications skip what someone else in the
household runs, while the calendar still shows it), per event, computed at read time:
  - a household calendar → "household";
  - its calendar assigned to an adult → that person;
  - its calendar assigned to a kid → the household member who organized it (kids don't run their own
    appointments: Ava's therapy that Robin booked is Robin's to manage), else the kid;
  - else its organizer: the syncing person when `organizer_is_me` (the Mac sends it, #258), or the household
    member the organizer's name is (exactly, or by first name when only one person has it);
  - else the person whose device synced it.
None when the organizer is someone outside the household. `managed_by_you`: it's the viewer's to run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable


def _when(s: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _member(name: Any, people: list[dict[str, Any]]) -> str | None:
    """The household member an organizer's name is: the whole name, else a first name only one person has."""
    n = " ".join(str(name or "").split()).lower()
    if not n:
        return None
    names = {str(p.get("id")): " ".join(str(p.get("name") or "").split()).lower() for p in people}
    whole = [pid for pid, nm in names.items() if nm and nm == n]
    if len(whole) == 1:
        return whole[0]
    first = [pid for pid, nm in names.items() if nm and nm.split()[0] == n.split()[0]]
    return first[0] if len(first) == 1 else None


def managed_by(d: dict[str, Any], cal: str, owner: str, people: list[dict[str, Any]], cal_of: dict[str, str],
               hh: set[str]) -> str | None:
    """Who runs this event (module docstring); None for someone outside the household."""
    key = cal.lower()
    if key in hh:
        return "household"
    folks = {str(p.get("id")): p for p in people}
    flag = d.get("organizer_is_me")
    mine = flag in (True, "true", "1", 1)
    organizer = owner if mine else (_member(d.get("organizer"), people) if flag in (False, "false", "0", 0) else None)
    assigned = cal_of.get(key)
    if assigned:
        if (folks.get(assigned) or {}).get("role") == "kid" and organizer and (folks.get(organizer) or {}).get("role") != "kid":
            return organizer
        return assigned
    if mine or organizer:
        return organizer
    if flag in (False, "false", "0", 0) and str(d.get("organizer") or "").strip():
        return None                       # organized by someone outside the household
    return owner


def view(nodes: Iterable[dict[str, Any]], start: date, end: date, viewer: str | None, role: str | None,
         people: list[dict[str, Any]], household_cals: set[str], only: set[str] | None = None,
         fence: Callable[..., bool] | None = None, zone=None) -> dict[str, Any]:
    """→ {"start", "end", "people": [{id, name, role, you, events}], "household": {"events"}}. `only`: the
    person ids (and/or "household") the caller filtered to; None for everyone visible."""
    import audience
    folks = {str(p.get("id")): p for p in people}
    cal_of: dict[str, str] = {}
    for p in people:
        for c in p.get("calendars") or []:
            cal_of[" ".join(str(c).split()).lower()] = str(p.get("id"))
    hh = {" ".join(str(c).split()).lower() for c in household_cals}
    lo = datetime.combine(start, datetime.min.time())
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time())
    out: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for n in nodes:
        d = n.get("data") or {}
        title = str(d.get("title") or "").strip()
        s = _when(d.get("start"))
        if not title or s is None:
            continue
        local = s.astimezone(zone).replace(tzinfo=None) if (s.tzinfo and zone) else s.replace(tzinfo=None)
        if not lo <= local < hi:
            continue
        cal = " ".join(str(d.get("calendar") or "").split())
        prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
        owner = str(prov.get("owner") or "zach")
        whose = cal_of.get(cal.lower()) or ("household" if cal.lower() in hh else owner)
        if viewer is None:
            ok = cal.lower() in hh
        else:
            kid = folks.get(whose) or {}
            ok = (audience.visible(str(prov.get("audience") or "private"), viewer, role, owner=owner)
                  or cal.lower() in hh or whose == viewer
                  or (kid.get("role") == "kid" and viewer in (kid.get("parents") or [])))
        if not ok:
            continue
        if fence is not None and not fence(viewer, role, owner, text=title, item=str(n.get("node_id") or "")):
            continue
        if only is not None and whose not in only:
            continue
        key = (title.lower(), local.isoformat(), cal.lower())
        if key in seen:
            continue                      # one event synced by two people's devices
        seen.add(key)
        mb = managed_by(d, cal, owner, people, cal_of, hh)
        out.setdefault(whose, []).append({
            "id": str(n.get("node_id") or ""), "title": title, "start": str(d.get("start")),
            "end": str(d.get("end")) if d.get("end") else None,
            "all_day": d.get("all_day") in (True, "true", "1", 1), "location": d.get("location") or None,
            "calendar": cal or None, "managed_by": mb, "managed_by_you": viewer is not None and mb == viewer})
    for evs in out.values():
        evs.sort(key=lambda e: e["start"])
    rows = []
    for p in people:                      # household order; only people with something to show
        pid = str(p.get("id"))
        if pid in out:
            rows.append({"id": pid, "name": p.get("name"), "role": p.get("role"), "you": pid == viewer,
                         "events": out[pid]})
    return {"start": start.isoformat(), "end": end.isoformat(), "people": rows,
            "household": {"events": out.get("household", [])}}


def for_viewer(start: date, end: date, viewer: str | None, role: str | None,
               only: set[str] | None = None) -> dict[str, Any]:
    """The live view: appointment records from the record runtime, the people store, the household calendars."""
    import agent_runtime_lib as lib
    import fences
    import household_identity as hh
    import local_time
    import world
    r = {"nodes": lib.record_nodes("appointment")[0]}   # every one, paged (#345)
    fs = fences.get()
    fence = (lambda v, ro, o, *, text, item: fs.admits(v, ro, o, text=text, item=item)) if viewer else None
    out = view(r.get("nodes") or [], start, end, viewer, role, hh.get_store().people(), world.household_calendars(),
               only=only, fence=fence, zone=local_time.zone())
    out["partial"] = len(r.get("nodes") or []) >= 500
    return out
