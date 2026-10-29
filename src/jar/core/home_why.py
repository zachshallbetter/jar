#!/usr/bin/env python3
"""Why the house is the way it is (jellybean#431: "why is it warm?", "why are the lights on?"), from Home Assistant's
own history. No model writes any of it.

  why(target, hours)  → (status, body) for GET /v1/home/why and the home_why tool

The logbook gives each state change with its context: which automation (context_entity_id), which HA user (mapped to a
household member's first name through HA's person entities) or nothing (a device, a physical switch). For a
thermostat, the state history adds setpoint changes, which the logbook doesn't list.

Only household-visible devices: climate, lights, switches, fans, media players, humidifiers. Never locks, alarms,
garage doors or covers, cameras, sirens, or anyone's location (person, device_tracker): those answer hands_off. A person
is named by first name only, and only through a person entity; an HA user without one is "someone".
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import home_assistant as ha
import local_time

WHY_DOMAINS = {"climate", "light", "switch", "fan", "media_player", "humidifier"}
NEVER = ha.HANDS_OFF | {"person", "device_tracker", "zone"}
_NEVER_WORDS = {"lock", "locks", "alarm", "alarms", "garage", "camera", "cameras", "door", "doors", "siren",
                "where", "location", "someone"}
_KIND_WORDS = {"heat": "climate", "heating": "climate", "warm": "climate", "cold": "climate", "cool": "climate",
               "thermostat": "climate", "temperature": "climate", "furnace": "climate", "ac": "climate",
               "light": "light", "lights": "light", "lamp": "light", "lamps": "light",
               "tv": "media_player", "television": "media_player", "speaker": "media_player", "music": "media_player",
               "fan": "fan", "fans": "fan", "humidifier": "humidifier"}
_FILLER = {"the", "a", "an", "in", "on", "is", "are", "why", "it", "so", "did", "turn", "turned", "come", "came",
           "up", "upstairs", "downstairs", "all", "of", "my", "our"}


def _err(status: int, code: str, words: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"error": words, "code": code, **extra}


def _iso(when: datetime) -> str:
    return when.astimezone(local_time.zone() or timezone.utc).isoformat(timespec="seconds")


def _clock(when: datetime) -> str:
    return when.astimezone(local_time.zone() or timezone.utc).strftime("%-I:%M %p")


def resolve(states: list[dict], areas: dict[str, str], target: str) -> tuple[str, list[dict]]:
    """("ok"|"hands_off"|"not_found", candidates). A kind word ("heat", "lights") picks the domain, the rest of the
    words narrow by room or name."""
    words = [w for w in re.split(r"[^a-z0-9]+", target.lower()) if w]
    if set(words) & _NEVER_WORDS:
        return "hands_off", []
    named = ha._find(states, target)
    if named and all(s.get("entity_id", "").split(".", 1)[0] in NEVER for s in named):
        return "hands_off", []
    kinds = {_KIND_WORDS[w] for w in words if w in _KIND_WORDS}
    rest = [w for w in words if w not in _KIND_WORDS and w not in _FILLER]
    pool = [s for s in states if s.get("entity_id", "").split(".", 1)[0] in WHY_DOMAINS and s.get("state") != "unavailable"]
    if kinds:
        pool = [s for s in pool if s["entity_id"].split(".", 1)[0] in kinds]
        if rest:
            narrowed = [s for s in pool if all(w in (areas.get(s["entity_id"], "") + " " + ha._name(s)).lower()
                                                for w in rest)]
            pool = narrowed or pool
        if "light" in kinds and len(pool) > 1:
            lit = [s for s in pool if s.get("state") == "on"]
            pool = lit or pool
        return ("ok", pool) if pool else ("not_found", [])
    hits = [s for s in named if s.get("entity_id", "").split(".", 1)[0] in WHY_DOMAINS]
    return ("ok", hits) if hits else ("not_found", [])


def _people(states: list[dict]) -> dict[str, str]:
    """HA user id → first name, from person entities (their location is never read)."""
    out = {}
    for s in states:
        if s.get("entity_id", "").startswith("person."):
            a = s.get("attributes") or {}
            if a.get("user_id"):
                out[str(a["user_id"])] = ha._name(s).split()[0]
    return out


def cause(entry: dict, people: dict[str, str]) -> dict[str, Any]:
    ce = str(entry.get("context_entity_id") or "")
    if ce.startswith(("automation.", "script.")):
        return {"kind": "automation", "name": str(entry.get("context_entity_id_name") or ce.split(".", 1)[1].replace("_", " "))}
    if ce.startswith("schedule."):
        return {"kind": "schedule", "name": str(entry.get("context_entity_id_name") or "a schedule")}
    if entry.get("context_user_id"):
        return {"kind": "person", "name": people.get(str(entry["context_user_id"]), "someone")}
    if ce:
        return {"kind": "device", "name": str(entry.get("context_entity_id_name") or ce.split(".", 1)[1].replace("_", " "))}
    return {"kind": "unknown", "name": None}


def _what(domain: str, state: str) -> str:
    if domain == "climate":
        return "turned off" if state == "off" else f"turned to {state.replace('_', '/').title()}"
    if domain == "media_player":
        return {"playing": "started playing", "paused": "paused", "idle": "stopped", "off": "turned off",
                "on": "turned on"}.get(state, f"changed to {state}")
    return {"on": "turned on", "off": "turned off"}.get(state, f"changed to {state}")


def _setpoint(a: dict) -> str | None:
    unit = "°F" if str(a.get("temperature_unit") or "°F").endswith("F") else "°C"
    if a.get("temperature") is not None:
        return f"{_num(a['temperature'])}{unit}"
    if a.get("target_temp_low") is not None:
        return f"{_num(a['target_temp_low'])}–{_num(a.get('target_temp_high'))}{unit}"
    return None


def _num(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def _now_words(s: dict) -> str:
    domain, a, st = s["entity_id"].split(".", 1)[0], s.get("attributes") or {}, str(s.get("state"))
    if domain == "climate":
        bits = [st.replace("_", "/").title()]
        if a.get("current_temperature") is not None:
            bits.append(f"{_num(a['current_temperature'])}{'°F' if str(a.get('temperature_unit') or '°F').endswith('F') else '°C'}")
        sp = _setpoint(a)
        return " · ".join(bits) + (f" (set {sp})" if sp else "")
    return {"on": "On", "off": "Off"}.get(st, st.title())


def _parse(v: Any) -> datetime | None:
    return local_time.parse_iso(v)


def why(target: str, hours: int = 12, *, http: Any = None, now: datetime | None = None) -> tuple[int, dict[str, Any]]:
    http = http or ha._http
    target = " ".join(str(target or "").split())[:80]
    if not target:
        return _err(400, "bad_request", "Say what to explain, like the heat or the living room lights.")
    hours = max(1, min(int(hours or 12), 48))
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    try:
        states = http("GET", "/api/states", None)
    except PermissionError:
        return _err(503, "ha_not_connected", "Home Assistant isn't connected to titan.")
    except (urllib.error.URLError, TimeoutError, OSError):
        return _err(503, "ha_not_connected", "Home Assistant didn't answer.")
    areas = ha._areas(http)
    status, hits = resolve(states, areas, target)
    if status == "hands_off":
        return _err(403, "hands_off", "Locks, alarms, garage doors, cameras and where people are aren't explained "
                                      "here; Home Assistant has those.")
    if status == "not_found" or not hits:
        return _err(404, "not_found", f"I don't see anything called “{target}” in the house.")
    if len(hits) > 1:
        return _err(409, "ambiguous", "Which one?", candidates=[
            {"target": ha._name(s), "name": ha._name(s), "area": areas.get(s["entity_id"]) or None}
            for s in sorted(hits, key=lambda s: (areas.get(s["entity_id"]) or "~", ha._name(s)))[:12]])
    s = hits[0]
    eid, domain = s["entity_id"], s["entity_id"].split(".", 1)[0]
    people = _people(states)
    start = urllib.parse.quote(since.isoformat(timespec="seconds"))
    end = urllib.parse.quote(now.isoformat(timespec="seconds"))
    events: list[tuple[datetime, dict[str, Any]]] = []
    try:
        log = http("GET", f"/api/logbook/{start}?entity={urllib.parse.quote(eid)}&end_time={end}", None) or []
    except Exception:  # noqa: BLE001 - no logbook: the answer says so
        log = []
    for e in log if isinstance(log, list) else []:
        at = _parse(e.get("when"))
        if at is None or not e.get("state"):
            continue
        events.append((at, {"at": _iso(at), "what": _what(domain, str(e["state"])), "by": cause(e, people)}))
    if domain == "climate":   # setpoint changes aren't in the logbook
        try:
            hist = http("GET", f"/api/history/period/{start}?filter_entity_id={urllib.parse.quote(eid)}&end_time={end}", None)
        except Exception:  # noqa: BLE001
            hist = []
        series = hist[0] if isinstance(hist, list) and hist and isinstance(hist[0], list) else []
        prev = None
        for h in series:
            sp = _setpoint(h.get("attributes") or {})
            at = _parse(h.get("last_updated") or h.get("last_changed"))
            if sp and prev is not None and sp != prev and at is not None:
                near = next((ev["by"] for t, ev in events if abs((t - at).total_seconds()) <= 5), None)
                events.append((at, {"at": _iso(at), "what": f"set to {sp}", "by": near or {"kind": "unknown", "name": None}}))
            prev = sp or prev
    events.sort(key=lambda x: x[0])
    name = ha._name(s)
    body: dict[str, Any] = {"target": name, "kind": domain, "area": areas.get(eid) or None, "now": _now_words(s),
                            "since": _iso(since), "events": [ev for _, ev in events]}
    body["answer"] = answer(name, body["now"], [(t, ev) for t, ev in events], hours)
    return 200, body


def _by_words(by: dict[str, Any]) -> str:
    k, n = by.get("kind"), by.get("name")
    return {"automation": f"by the {n} automation", "person": f"by {n}", "device": f"from {n}",
            "schedule": f"on the {n} schedule"}.get(k, "")


def answer(name: str, now_words: str, events: list[tuple[datetime, dict[str, Any]]], hours: int) -> str:
    if not events:
        return f"{name} hasn't changed in the last {hours} hours; it's {now_words}."
    lines = []
    for at, ev in reversed(events[-2:]):
        by = _by_words(ev["by"])
        lines.append(f"{ev['what']} at {_clock(at)}" + (f" {by}" if by else ""))
    said = f"{name} was {lines[0]}"
    if len(lines) > 1:
        said += f"; before that, {lines[1]}"
    if not _by_words(events[-1][1]["by"]):
        said += " (Home Assistant doesn't say what changed it)"
    return said + f". Now: {now_words}."


def tool(args: dict[str, Any]) -> str:
    """The home_why tool: the same answer in words."""
    status, body = why(str(args.get("target") or args.get("device") or ""), int(args.get("hours") or 12))
    if status == 409:
        return "Which one: " + ", ".join(f"{c['name']}" + (f" ({c['area']})" if c.get("area") else "")
                                          for c in body.get("candidates", [])) + "?"
    return body.get("answer") if status == 200 else body.get("error", "Couldn't check that.")
