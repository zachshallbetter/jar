#!/usr/bin/env python3
"""A room's state, never a person's (jellybean#433 slice 2; the contract is on that issue): occupied, empty, asleep
or unknown, for the rooms that have a household screen. With ROOM_HOLD=1, a hand-off aimed at a room that's asleep is held (409)
until the sender says "send anyway"; off by default until both apps can say it.

  sensors      Home Assistant's binary_sensors with device_class motion, occupancy or presence, by area (an area of
               the same folded name as the screen's room). Never one on a device that also has a camera, never a
               person or a device tracker, never a write: GET /api/states and the template home_status already uses.
  quiet hours  22:00-07:00 household time, or a room's own in /ai/config/rooms.yaml (ROOMS_FILE), e.g.
               `rooms: {Kids room: {quiet: "19:30-07:00"}}`. A missing file means the default; a bad entry too.

  state        outside quiet hours: a sensor on → occupied; a presence sensor and all off → empty; else unknown.
               in quiet hours: motion on → occupied (someone's up); presence on → asleep; a presence sensor and all
               off → empty; motion-only or no sensors → asleep (they can't tell empty from asleep).
               any of the room's sensors unavailable, or no good read in STALE_AFTER_S → unknown (outside quiet hours)
               or the quiet-hours fallback (asleep). unknown never holds anything.

Nothing is stored or logged: no history, no room's state in a log line.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOMS_FILE = os.environ.get("ROOMS_FILE", "/ai/config/rooms.yaml")
DEFAULT_QUIET = (22 * 60, 7 * 60)   # minutes after midnight: start, end
CACHE_S = 60
STALE_AFTER_S = 5 * 60
SENSOR_CLASSES = {"motion", "occupancy", "presence"}
PRESENCE_CLASSES = {"occupancy", "presence"}   # mmWave and the like: they see someone lying still
STATES = ("occupied", "empty", "asleep", "unknown")

# A template that names every binary_sensor on a device that also has a camera (never used for occupancy).
CAMERA_TEMPLATE = ("{% for s in states.binary_sensor %}{% set d = device_id(s.entity_id) %}"
                   "{% if d and (device_entities(d) | select('match', 'camera\\.') | list) %}{{ s.entity_id }}\n"
                   "{% endif %}{% endfor %}")

Http = Callable[[str, str, Any], Any]

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "good_at": 0.0, "sensors": []}


def room_key(room: Any) -> str:
    return " ".join(str(room or "").split()).casefold()


# ── quiet hours ─────────────────────────────────────────────────────────────────────────────────────────────────

_HM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_quiet(text: Any) -> tuple[int, int] | None:
    """"19:30-07:00" → (1170, 420); None for anything else."""
    m = _HM.match(str(text or ""))
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59 or (h1, m1) == (h2, m2):
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def overrides(path: str | None = None) -> dict[str, tuple[int, int]]:
    """Room (folded) → its own quiet hours, from rooms.yaml. Missing file, bad YAML or a bad entry: left out."""
    p = Path(path or os.environ.get("ROOMS_FILE") or ROOMS_FILE)
    try:
        import yaml
        doc = yaml.safe_load(p.read_text()) or {}
    except Exception:  # noqa: BLE001 - a missing or broken file means the defaults
        return {}
    rooms = doc.get("rooms") if isinstance(doc, dict) else None
    out = {}
    for name, cfg in (rooms or {}).items() if isinstance(rooms, dict) else []:
        q = parse_quiet(cfg.get("quiet")) if isinstance(cfg, dict) else None
        if q and room_key(name):
            out[room_key(name)] = q
    return out


def in_quiet(now_local: datetime, quiet: tuple[int, int]) -> bool:
    m = now_local.hour * 60 + now_local.minute
    start, end = quiet
    return start <= m < end if start < end else (m >= start or m < end)


def quiet_end(now_local: datetime, quiet: tuple[int, int]) -> str:
    """When this quiet stretch ends, in the household's words: "7:00", "6:30"."""
    end = quiet[1]
    return f"{end // 60}:{end % 60:02d}"


# ── sensors ─────────────────────────────────────────────────────────────────────────────────────────────────────

def read_sensors(http: Http, areas: Callable[[Http], dict[str, str]]) -> list[dict[str, Any]]:
    """The occupancy sensors Home Assistant has, by area: [{area, kind, on, available}]. Read only."""
    states = http("GET", "/api/states", None)
    try:
        cams = {l.strip() for l in str(http("POST", "/api/template", {"template": CAMERA_TEMPLATE})).splitlines()
                if l.strip()}
    except Exception:  # noqa: BLE001 - can't tell which are cameras: use none rather than risk one
        return []
    where = areas(http)
    out = []
    for s in states or []:
        eid = str(s.get("entity_id") or "")
        cls = (s.get("attributes") or {}).get("device_class")
        if not eid.startswith("binary_sensor.") or cls not in SENSOR_CLASSES or eid in cams:
            continue
        area = where.get(eid)
        if not area:
            continue
        st = s.get("state")
        out.append({"area": room_key(area), "kind": "presence" if cls in PRESENCE_CLASSES else "motion",
                    "on": st == "on", "available": st in ("on", "off")})
    return out


def sensors(now: float, http: Http | None = None, areas: Callable[[Http], dict[str, str]] | None = None
            ) -> tuple[list[dict[str, Any]], bool]:
    """(sensors, fresh): cached CACHE_S; fresh is False when there's been no good read in STALE_AFTER_S."""
    import home_assistant as ha
    http = http or ha._http
    areas = areas or ha._areas
    with _lock:
        if now - _cache["at"] >= CACHE_S:
            _cache["at"] = now
            try:
                _cache["sensors"] = read_sensors(http, areas)
                _cache["good_at"] = now
            except Exception:  # noqa: BLE001 - Home Assistant down or not connected: stale, not an error
                pass
        return list(_cache["sensors"]), now - _cache["good_at"] < STALE_AFTER_S


def reset_cache() -> None:
    with _lock:
        _cache.update(at=0.0, good_at=0.0, sensors=[])


# ── a room's state ──────────────────────────────────────────────────────────────────────────────────────────────

def decide(room_sensors: list[dict[str, Any]], quiet: bool, fresh: bool) -> tuple[str, str]:
    """(state, why) from the room's sensors and the clock. Pure."""
    if not fresh or any(not s["available"] for s in room_sensors):
        return ("asleep", "quiet hours") if quiet else ("unknown", "stale")
    if not room_sensors:
        return ("asleep", "quiet hours") if quiet else ("unknown", "no sensor")
    motion_on = any(s["on"] and s["kind"] == "motion" for s in room_sensors)
    presence_on = any(s["on"] and s["kind"] == "presence" for s in room_sensors)
    has_presence = any(s["kind"] == "presence" for s in room_sensors)
    if quiet:
        if motion_on:
            return "occupied", "sensor"
        if presence_on:
            return "asleep", "sensor"
        return ("empty", "sensor") if has_presence else ("asleep", "quiet hours")
    if motion_on or presence_on:
        return "occupied", "sensor"
    return ("empty", "sensor") if has_presence else ("unknown", "sensor")


def states(rooms: list[str], *, now: float | None = None, now_local: datetime | None = None,
           http: Http | None = None, areas: Callable[[Http], dict[str, str]] | None = None,
           rooms_file: str | None = None, area_of: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """GET /v1/rooms/state: each named room's state, why, and until when quiet hours hold it. `area_of`: room_key →
    its Home Assistant area, for a room renamed in Settings (#1511); else the room's own name is the area's."""
    now = time.time() if now is None else now
    if now_local is None:
        import agent_runtime_lib as lib
        now_local = datetime.fromtimestamp(now, lib._local_now().tzinfo)
    found, fresh = sensors(now, http, areas)
    own = overrides(rooms_file)
    out = []
    for room in rooms:
        key = room_key(room)
        quiet_hours = own.get(key, DEFAULT_QUIET)
        quiet = in_quiet(now_local, quiet_hours)
        area = room_key((area_of or {}).get(key, room))
        state, why = decide([s for s in found if s["area"] == area], quiet, fresh)
        row = {"room": room, "state": state, "why": why, "as_of": now}
        if state == "asleep" and why == "quiet hours":
            row["until"] = quiet_end(now_local, quiet_hours)
        out.append(row)
    return out


def hold_enabled() -> bool:
    """ROOM_HOLD=1 turns the hand-off hold on. Off by default: an app without "Send anyway?" would have no way
    through at night (Coordination, 10-04). GET /v1/rooms/state is on either way; it's read-only."""
    return os.environ.get("ROOM_HOLD", "").strip() == "1"


def hold(room: str, **kw: Any) -> dict[str, Any] | None:
    """A hand-off's check: the room's row when it's asleep (the 409's body), else None. Unknown never holds."""
    row = states([room], **kw)[0]
    if row["state"] != "asleep":
        return None
    why = f"quiet hours in {room} until {row['until']}" if row.get("until") else f"{room} is asleep (its sensor)"
    return {"room": room, "state": "asleep", "why": why}
