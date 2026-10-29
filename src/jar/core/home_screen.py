#!/usr/bin/env python3
"""Home, for a shared screen (titan-node#669; the Apple TV's Home, jellybean#416): the household's day at a glance,
as blocks built in code. No model writes any of it.

  header     date, time, weather (weather.today_summary), and one state pill when it matters today: where the kids
             are staying (custody), only on people's own devices or when FAMILY_SCREEN_CUSTODY=1 (Zach's decision)
  up_next    the next household event today, with minutes to go
  today      today's household events (a calendar the adults made the household's), past ones marked
  chores     today's household chores (garbage, recycling, bins): from the same events
  house      Home Assistant tiles: the thermostat, lights on, never locks, alarms, garage doors or cameras
  playing    media players in the house that are playing or paused
  shelf      up to 4 household cards (proactive cards for the household), each with its own buttons

house (GET/POST /v1/home/house, jellybean#420): the house room by room (lights, thermostats) and its scenes, and the
few changes a shared screen may make to them, through home_assistant.home_control. Never locks, alarms, garage doors
or cameras.

week (GET /v1/home/week, jellybean#417): seven days of household events with who and who's driving (the family
meeting's answers), the day's chores (world.chores_due), where the kids' stay changes (only with custody allowed), and
repeating events once, in `series`, with their rhythm (connectors_runner.series_text) and next date in the week.

Household scope only, whoever asks: a person's own events, cards, mail or money never appear (titan-node#418).
Every block is optional: one that can't be read is left out, never an error.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

SHELF_MAX = 4
_CHORE_RX = re.compile(r"\b(?:garbage|trash|recycl\w*|bins?|compost|yard waste)\b", re.I)
TILE_DOMAINS = ("climate", "light", "media_player")


def _clock(d: datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def events_block(evs: list, now: datetime) -> tuple[list[dict], dict | None, list[dict]]:
    """(today, up_next, chores) from household_coordination Events: only the household's (audience household)."""
    today, chores = [], []
    up = None
    for ev in evs:
        if getattr(ev, "audience", "") != "household":
            continue
        item = {"at": _clock(ev.start), "start": ev.start.isoformat(timespec="minutes"), "title": ev.title,
                "past": ev.end <= now}
        today.append(item)
        if _CHORE_RX.search(ev.title):
            chores.append({"text": f"{ev.title} by {_clock(ev.start)}", "at": item["at"]})
        if up is None and ev.start > now:
            up = {**item, "in_minutes": int((ev.start - now).total_seconds() // 60)}
    return today, up, chores


def pill(people: list[dict], at: datetime, state_at: Callable[[str, datetime], dict | None] | None) -> str | None:
    """One line when the kids aren't here: "Kids at their dad's until Thu 9 AM". None when they're home or nothing says."""
    if state_at is None:
        return None
    kids = [p for p in people if p.get("role") == "kid"]
    away = []
    for p in kids:
        s = state_at(str(p["id"]), at)
        if s and s.get("state") == "away" and s.get("until"):
            away.append((str(p.get("name") or "").split(" ")[0], s["until"], p.get("other_parent") or "their other parent"))
    if not away:
        return None
    until = min(a[1] for a in away)
    who = "Kids" if len(away) == len(kids) and len(kids) > 1 else " and ".join(a[0] for a in away)
    return f"{who} at {away[0][2]}'s until {until:%a} {_clock(until)}"


def house_block(states: list[dict]) -> tuple[list[dict], list[dict]]:
    """(tiles, playing) from Home Assistant states: the thermostat(s) and how many lights are on; media playing. Locks,
    alarms, covers (garage doors), cameras and the rest of home_assistant.HANDS_OFF never appear."""
    import home_assistant as ha
    tiles, playing = [], []
    lights_on = 0
    for s in states or []:
        eid = str(s.get("entity_id") or "")
        domain = eid.split(".", 1)[0]
        if domain in ha.HANDS_OFF or domain not in TILE_DOMAINS or s.get("state") == "unavailable":
            continue
        a = s.get("attributes") or {}
        name = str(a.get("friendly_name") or eid)
        if domain == "climate":
            t = {"kind": "climate", "name": name, "state": s.get("state")}
            if a.get("current_temperature") is not None:
                t["inside"] = a["current_temperature"]
            if a.get("temperature") is not None:
                t["set_to"] = a["temperature"]
            if a.get("hvac_action"):
                t["action"] = a["hvac_action"]
            tiles.append(t)
        elif domain == "light" and s.get("state") == "on":
            lights_on += 1
        elif domain == "media_player" and s.get("state") in ("playing", "paused") and a.get("media_title"):
            playing.append({"id": eid, "where": name, "state": s["state"], "title": a["media_title"],
                            "artist": a.get("media_artist") or ""})
    if lights_on:
        tiles.append({"kind": "lights", "on": lights_on})
    return tiles, playing


MEDIA_ACTIONS = ("play", "pause", "next", "previous")


def media_control(entity_id: str, action: str, *, http: Callable[[str, str, Any], Any] | None = None) -> tuple[int, dict]:
    """POST /v1/home/media (jellybean#421): play, pause or skip on a media player that Home lists right now (playing or
    paused, with a title), from any household screen or person. Nothing else: no other action (volume, source, power),
    no other domain, no player Home isn't showing. The change goes through home_assistant.home_control, held to
    media_player, so its service map and HANDS_OFF apply as they do for the agent. Returns (HTTP status, body)."""
    import urllib.error

    import home_assistant as ha
    http = http or ha._http
    eid, action = (entity_id or "").strip(), (action or "").strip().lower()
    if action not in MEDIA_ACTIONS:
        return 400, {"error": f"action must be one of {', '.join(MEDIA_ACTIONS)}"}
    if not re.fullmatch(r"media_player\.[a-z0-9_]+", eid):
        return 403, {"error": "Only a media player Home shows can be played or paused here."}
    try:
        states = http("GET", "/api/states", None)
    except PermissionError as exc:
        return 503, {"error": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 502, {"error": f"Couldn't reach Home Assistant: {exc}"}
    _, playing = house_block(states if isinstance(states, list) else [])
    if eid not in {p["id"] for p in playing}:
        return 404, {"error": "That isn't playing in the house right now."}
    out = ha.home_control({"device": eid, "action": action}, http=http, domains=frozenset({"media_player"}),
                          refusal="Only a media player Home shows can be played or paused here.")
    ok = out.startswith("Done")
    return (200 if ok else 502), {"ok": ok, "text": out}


HOUSE_DOMAINS = ("light", "climate", "scene")
# What the house view may do, by domain: nothing else, whatever home_control could.
HOUSE_ACTIONS = {"light": ("on", "off", "toggle", "brightness"), "climate": ("set_temperature",), "scene": ("activate",)}
ELSEWHERE = "Elsewhere"


def _house_domains(role: str | None) -> frozenset[str]:
    """The house view's domains for a role: light, climate and scene, less what the role may not change (a kid's
    KID_HOME_DOMAINS), never home_assistant.HANDS_OFF."""
    import home_assistant as ha
    import household_identity as hh
    allowed = hh.home_domains_for(role)
    return frozenset(d for d in HOUSE_DOMAINS if d not in ha.HANDS_OFF and (allowed is None or d in allowed))


def house_view(states: list[dict], areas: dict[str, str], domains: frozenset[str] | set[str] = frozenset(HOUSE_DOMAINS)) -> dict:
    """The house, room by room (jellybean#420): {rooms: [{name, lights: [{id, name, on, brightness?}], climate: [{id,
    name, state, inside?, set_to?, low?, high?, action?}]}], scenes: [{id, name}]}. Rooms are Home Assistant areas
    (A to Z, ELSEWHERE last; empty ones left out). Only `domains` (light, climate, scene at most): locks, alarms, garage
    doors, cameras and the rest never appear; unavailable devices are left out."""
    import home_assistant as ha
    rooms: dict[str, dict] = {}
    scenes = []
    for s in states or []:
        eid = str(s.get("entity_id") or "")
        domain = eid.split(".", 1)[0]
        if domain not in domains or domain not in HOUSE_DOMAINS or domain in ha.HANDS_OFF \
                or s.get("state") in ("unavailable", None):
            continue
        a = s.get("attributes") or {}
        name = str(a.get("friendly_name") or eid)
        if domain == "scene":
            scenes.append({"id": eid, "name": name})
            continue
        room = rooms.setdefault(areas.get(eid) or ELSEWHERE, {"lights": [], "climate": []})
        if domain == "light":
            t = {"id": eid, "name": name, "on": s.get("state") == "on"}
            if t["on"] and isinstance(a.get("brightness"), (int, float)):
                t["brightness"] = max(1, round(a["brightness"] / 255 * 100))
            room["lights"].append(t)
        else:
            t = {"id": eid, "name": name, "state": s.get("state")}
            for key, attr in (("inside", "current_temperature"), ("set_to", "temperature"), ("low", "target_temp_low"),
                              ("high", "target_temp_high"), ("action", "hvac_action")):
                if a.get(attr) is not None:
                    t[key] = a[attr]
            room["climate"].append(t)
    order = sorted(rooms, key=lambda r: (r == ELSEWHERE, r.lower()))
    return {"rooms": [{"name": r, "lights": sorted(rooms[r]["lights"], key=lambda x: x["name"].lower()),
                       "climate": rooms[r]["climate"]} for r in order],
            "scenes": sorted(scenes, key=lambda x: x["name"].lower())}


def gather_house(role: str | None, *, http: Callable[[str, str, Any], Any] | None = None) -> tuple[int, dict]:
    """GET /v1/home/house: (HTTP status, house_view). 503 when Home Assistant isn't connected, 502 when it can't be
    reached; an empty house (no lights, thermostats or scenes) is a 200 with empty lists."""
    import urllib.error

    import home_assistant as ha
    http = http or ha._http
    try:
        states = http("GET", "/api/states", None)
    except PermissionError as exc:
        return 503, {"error": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 502, {"error": f"Couldn't reach Home Assistant: {exc}"}
    return 200, house_view(states if isinstance(states, list) else [], ha._areas(http), _house_domains(role))


def house_control(entity_id: str, action: str, value: Any, role: str | None, *,
                  http: Callable[[str, str, Any], Any] | None = None) -> tuple[int, dict]:
    """POST /v1/home/house (jellybean#420): a light on, off, toggled or dimmed (value 1-100), a thermostat's set point
    (value: one number; home_control's safe range and Auto-range rules apply), or a scene activated, on a device the
    house view lists right now for this role. Nothing else. Goes through home_assistant.home_control held to the
    view's domains, so its service map and HANDS_OFF apply as they do for the agent. Returns (HTTP status, body)."""
    import urllib.error

    import home_assistant as ha
    http = http or ha._http
    eid, action = (entity_id or "").strip(), (action or "").strip().lower()
    domains = _house_domains(role)
    m = re.fullmatch(r"([a-z_]+)\.[a-z0-9_]+", eid)
    if not m or m.group(1) not in domains:
        return 403, {"error": "Only a light, thermostat or scene the house shows can be changed here."}
    if action not in HOUSE_ACTIONS[m.group(1)]:
        return 400, {"error": f"action must be one of {', '.join(HOUSE_ACTIONS[m.group(1)])}"}
    args: dict[str, Any] = {"device": eid, "action": action}
    if action in ("brightness", "set_temperature"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 400, {"error": "value must be a number"}
        args["value"] = value
    try:
        states = http("GET", "/api/states", None)
    except PermissionError as exc:
        return 503, {"error": str(exc)}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 502, {"error": f"Couldn't reach Home Assistant: {exc}"}
    view = house_view(states if isinstance(states, list) else [], {}, domains)
    shown = {x["id"] for r in view["rooms"] for x in r["lights"] + r["climate"]} | {x["id"] for x in view["scenes"]}
    if eid not in shown:
        return 404, {"error": "The house doesn't show that right now."}
    out = ha.home_control(args, http=http, domains=domains,
                          refusal="Only a light, thermostat or scene the house shows can be changed here.")
    if out.startswith("Done"):
        return 200, {"ok": True, "text": out}
    unreachable = out.startswith("Couldn't reach") or out.startswith("Home Assistant refused")
    return (502 if unreachable else 422), {"ok": False, "text": out}


def shelf_block(cards: list[dict]) -> list[dict]:
    """Up to SHELF_MAX household cards, most urgent first, each a snapshot with its own buttons (acted on through
    POST /v1/proactive/{card}/action, like the reply's action blocks)."""
    picked = sorted(cards or [], key=lambda c: (-int(c.get("urgency") or 0), -(c.get("updated_at") or 0)))[:SHELF_MAX]
    return [{"card": c["id"], "title": str(c.get("title") or "")[:120], "kind": c.get("kind"),
             "actions": [{**{k: a.get(k) for k in ("id", "label", "kind", "value")},
                          **({"open": a["open"]} if a.get("open") else {})}   # a screen to open (jellybean#425)
                         for a in (c.get("actions") or [])][:3]}
            for c in picked]


def build(now: datetime, people: list[dict], evs: list, *, weather: dict | None = None,
          state_at: Callable[[str, datetime], dict | None] | None = None, ha_states: list[dict] | None = None,
          cards: list[dict] | None = None) -> dict[str, Any]:
    today, up, chores = events_block(evs, now)
    tiles, playing = house_block(ha_states or [])
    return {"header": {"date": f"{now:%a %b} {now.day}", "time": _clock(now), "weather": weather,
                       "pill": pill(people, now, state_at)},
            "up_next": up, "today": today, "chores": chores, "house": tiles, "playing": playing,
            "shelf": shelf_block(cards or [])}


def gather(role: str | None, now: datetime | None = None) -> dict[str, Any]:
    """Home for a viewer, from titan's live sources. Household items only, whatever the role; the kids' stays only
    on people's own devices or with FAMILY_SCREEN_CUSTODY=1."""
    import household_coordination as hc
    import household_identity as hh
    import local_time
    import world
    tz = local_time.zone()
    now = now or datetime.now(tz)
    people = hh.get_store().people()
    try:
        evs = hc.events_for(now.date(), hc.fetch_nodes(now.date(), tz), people, tz, household=world.household_calendars())
    except Exception:  # noqa: BLE001 - a block that can't be read is left out
        evs = []
    try:
        import weather
        wx = weather.today_summary()
    except Exception:  # noqa: BLE001
        wx = None
    state_at = None
    try:
        import custody
        import family_meeting
        if role in ("admin", "adult", "kid") or family_meeting.custody_on_shared():
            state_at = custody.state_at
    except Exception:  # noqa: BLE001
        pass
    try:
        import home_assistant as ha
        states = ha._http("GET", "/api/states", None)
    except Exception:  # noqa: BLE001
        states = []
    try:
        import proactive
        cards = proactive.list_cards(person="household", role="household")   # household-audience cards only
    except Exception:  # noqa: BLE001
        cards = []
    h = build(now, people, evs, weather=wx, state_at=state_at, ha_states=states if isinstance(states, list) else [],
              cards=cards)
    try:
        h["chores"] = merge_chores(h["chores"], world.chores_due(world.get(), now.date(), people, state_at, zone=tz,
                                                                  person="household", role="household"),
                                   reasons=state_at is not None)
    except Exception:  # noqa: BLE001
        pass
    return h


def merge_chores(from_calendar: list[dict], computed: list[dict], reasons: bool = True) -> list[dict]:
    """Today's chores: the household's own rules (world.chores_due: "Recycling: yes, the kids are at their other
    parent's") first, then calendar chore events not already named. A conditional chore without custody to read
    says it depends, never yes or no."""
    out, names = [], set()
    for c in computed:
        t = c["at"].strftime("%I:%M %p").lstrip("0").replace(":00 ", " ") if c.get("timed", True) else ""
        by = f" by {t}" if t else " today"
        if c["when"] is None:
            text = f"{c['name']}{by}"
        elif c["due"] is None:
            text = f"{c['name']}: depends on the kids' week"
        elif c["due"]:
            text = f"{c['name']}{by}" + (f" ({c['why']})" if reasons else "")
        else:
            text = f"No {c['name'].lower()} tonight" + (f" ({c['why']})" if reasons else "")
        out.append({"text": text, "at": t or None, "due": c["due"]})
        names.add(c["name"].lower())
    out += [c for c in from_calendar if not any(n in c["text"].lower() for n in names)]
    return out


DAY_EVENTS_MAX, SERIES_MAX, TITLE_MAX = 12, 8, 80
LOOKBACK_DAYS = 21   # a repeating event's rhythm is read from the three weeks before the week as well


def _rhythm(title: str, starts: list[datetime], now: datetime) -> str | None:
    import connectors_runner as cx
    m = re.search(r"^Repeats: (.+?) \(\d+ synced", cx.series_text(title, starts, now, title), re.M)
    return m.group(1) if m else None


def week(start: date, by_day: dict[date, list], now: datetime, people: list[dict],
         chores: Callable[[date], list[dict]] | None = None,
         state_at: Callable[[str, datetime], dict | None] | None = None,
         answers: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """The week, from household_coordination Events per day (by_day: the week and LOOKBACK_DAYS before it). Household
    events only. An event that happens twice or more in the span is a series: once in `series`, out of `days`."""
    tz = now.tzinfo
    names = {str(p["id"]): str(p.get("name") or p["id"]).split(" ")[0] for p in people}
    span: dict[tuple[str, str], list] = {}
    for d, evs in by_day.items():
        for ev in evs:
            if getattr(ev, "audience", "") == "household":
                span.setdefault((ev.title.lower(), ev.calendar.lower()), []).append(ev)
    days7 = [start + timedelta(days=i) for i in range(7)]
    in_week = {k: [e for e in v if e.start.date() in days7] for k, v in span.items()}
    repeating = {k for k, v in span.items() if len(v) >= 2 and in_week[k]}
    series = []
    for k in sorted(repeating, key=lambda k: min(e.start for e in in_week[k]))[:SERIES_MAX]:
        evs = sorted(span[k], key=lambda e: e.start)
        nxt = next((e for e in sorted(in_week[k], key=lambda e: e.start) if e.end > now), None)
        item = {"title": evs[0].title[:TITLE_MAX], "rhythm": _rhythm(evs[0].title, [e.start for e in evs], now)}
        who = " and ".join(evs[0].people)
        if who:
            item["who"] = who
        if nxt:
            item["next"] = f"{nxt.start:%a %b} {nxt.start.day}, {_clock(nxt.start)}"
        series.append(item)
    taken = {item: next((names.get(pid, pid) for pid, a in said.items() if a == "take"), None)
             for item, said in (answers or {}).items()}
    days = []
    for d in days7:
        evs = [e for e in by_day.get(d, []) if getattr(e, "audience", "") == "household"
               and (e.title.lower(), e.calendar.lower()) not in repeating]
        items = []
        for e in sorted(evs, key=lambda e: e.start)[:DAY_EVENTS_MAX]:
            it = {"at": _clock(e.start), "start": e.start.isoformat(timespec="minutes"),
                  "end": e.end.isoformat(timespec="minutes"), "title": e.title[:TITLE_MAX], "past": e.end <= now}
            if e.people:
                it["who"] = " and ".join(e.people)
                drv = next((taken.get(f"{d.isoformat()}:{w.lower().replace(' ', '-')}:{e.start:%H%M}") for w in e.people
                            if taken.get(f"{d.isoformat()}:{w.lower().replace(' ', '-')}:{e.start:%H%M}")), None)
                if drv:
                    it["driver"] = drv
            items.append(it)
        day = {"date": d.isoformat(), "label": f"{d:%a} {d.day}", "today": d == now.date(), "events": items,
               "chores": []}
        for c in (chores(d) if chores else []):
            t = _clock(c["at"]) if c.get("timed", True) else None
            day["chores"].append({"text": c["name"] if c["when"] is None else
                                  (f"{c['name']}: depends on the kids' week" if c["due"] is None else
                                   c["name"] if c["due"] else f"No {c['name'].lower()}"),
                                  "at": t, "start": c["at"].isoformat(timespec="minutes") if t else None, "due": c["due"]})
        if state_at is not None:
            lo = datetime.combine(d, time(0), tz)
            for p in [p for p in people if p.get("role") == "kid"][:1]:
                s = state_at(str(p["id"]), lo)
                if s and s.get("until") is not None and lo <= s["until"] < lo + timedelta(days=1):
                    other = p.get("other_parent") or "their other parent"
                    day["state"] = (f"Kids home from {s['until']:%a} {_clock(s['until'])}" if s.get("then") == "here" else
                                    f"Kids at {other}'s from {s['until']:%a} {_clock(s['until'])}")
        days.append(day)
    return {"start": start.isoformat(), "days": days, "series": series}


def gather_week(role: str | None, start: date | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The week for a viewer: household items only, whatever the role; the kids' stays only with custody allowed."""
    import household_coordination as hc
    import household_identity as hh
    import local_time
    import world
    tz = local_time.zone()
    now = now or datetime.now(tz)
    start = start or now.date()
    people = hh.get_store().people()
    house = world.household_calendars()
    try:
        import family_meeting as fm
        nodes = fm.fetch_nodes(start - timedelta(days=LOOKBACK_DAYS), 7 + LOOKBACK_DAYS, tz)
        answers = fm.answers_for(start.isoformat())
        allowed = role in ("admin", "adult", "kid") or fm.custody_on_shared()
    except Exception:  # noqa: BLE001
        nodes, answers, allowed = [], {}, False
    by_day = {}
    for i in range(-LOOKBACK_DAYS, 7):
        d = start + timedelta(days=i)
        try:
            by_day[d] = hc.events_for(d, nodes, people, tz, household=house)
        except Exception:  # noqa: BLE001
            by_day[d] = []
    state_at = None
    if allowed:
        try:
            import custody
            state_at = custody.state_at
        except Exception:  # noqa: BLE001
            pass

    def chores(d: date) -> list[dict]:
        try:
            return world.chores_due(world.get(), d, people, state_at, zone=tz, person="household", role="household")
        except Exception:  # noqa: BLE001
            return []
    return week(start, by_day, now, people, chores=chores, state_at=state_at, answers=answers)


if __name__ == "__main__":
    print(json.dumps(gather("household"), indent=1, default=str))
