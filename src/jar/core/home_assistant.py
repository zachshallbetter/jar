#!/usr/bin/env python3
"""The household's devices, through Home Assistant on titan (titan-homeassistant.service).

Two agent tools:
  home_status   what's in the house and its state (optionally one room, kind or name)
  home_control  change one device: lights, switches, fans, the thermostat, media, scenes

Home Assistant holds every device login (Honeywell's cloud, HomeKit pairings, cameras); titan
only has a long-lived access token (HA_TOKEN_FILE, default /ai/secrets/homeassistant_token,
created by the user in HA's profile page). Locks, alarms, garage doors and covers, cameras,
sirens, scripts and automations are never touched from here: the result says to use Home
Assistant, until those get a confirmation step of their own.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

HA_URL = os.environ.get("HA_URL", "http://127.0.0.1:8123").rstrip("/")
TOKEN_FILE = Path(os.environ.get("HA_TOKEN_FILE", "/ai/secrets/homeassistant_token"))
NOT_CONNECTED = ("Home Assistant isn't connected to titan yet (no access token). The user creates one in "
                 "Home Assistant › Profile › Security › Long-lived access tokens and saves it on titan.")

# Domains the agent may change, and the ones it must leave to the user.
CONTROLLABLE = {"light", "switch", "fan", "climate", "media_player", "scene", "humidifier", "input_boolean", "remote"}
HANDS_OFF = {"lock", "alarm_control_panel", "cover", "camera", "siren", "script", "automation", "button", "valve"}
# What a plain "what's in the house?" lists. Sensors, remotes (an Apple TV's remote entity duplicates
# the TV) and weather show only when asked for by kind or name.
# Security-shaped devices in harmless domains (10-07 review W3): a camera's privacy switch, a motion detector's
# enable switch, a garage door or gate wired as a switch. Kids and shared household screens (restricted) never see or
# change them, whatever their domain says. A word of the entity id or friendly name (whole words, so "indoor" isn't
# "door"), plus a named list for the ones whose names don't say it: HA_RESTRICTED_ENTITIES, comma-separated ids.
# HA_KID_ALLOWED_ENTITIES (ids) lets a harmless device the words caught back in; the restricted list always wins.
SENSITIVE_WORDS = frozenset({"camera", "cam", "privacy", "motion", "detect", "detection", "garage", "gate", "door",
                             "doorbell", "lock", "alarm", "siren", "security", "record", "recording", "surveillance",
                             "nvr", "arm", "armed", "disarm", "occupancy", "presence"})


def _entity_list(var: str) -> frozenset[str]:
    return frozenset(e.strip().lower() for e in os.environ.get(var, "").split(",") if e.strip())


def _named_restricted() -> frozenset[str]:
    return _entity_list("HA_RESTRICTED_ENTITIES")


def sensitive(s: dict) -> bool:
    """Whether a device is security-shaped: off limits to restricted roles in any domain."""
    eid = str(s.get("entity_id", "")).lower()
    if eid in _named_restricted():
        return True
    if eid.split(".", 1)[0] == "light":
        return False   # a light only lights ("Front Door Light"); a relay, a scene or a boolean can open or disarm
    if eid in _entity_list("HA_KID_ALLOWED_ENTITIES"):
        return False   # a word matched a harmless device ("Garage light"): allowed by name; never beats the list above
    words = set(re.split(r"[^a-z0-9]+", (eid.split(".", 1)[-1] + " " + _name(s)).lower()))
    return bool({w[:-1] if w.endswith("s") and w[:-1] in SENSITIVE_WORDS else w for w in words} & SENSITIVE_WORDS)


SHOWN = (CONTROLLABLE - {"remote"}) | {"lock", "alarm_control_panel", "cover", "camera", "vacuum"}
ON_REQUEST = {"sensor", "binary_sensor", "remote", "weather"}
TEMP_F = (50, 85)
TEMP_C = (10, 30)

Http = Callable[[str, str, Any], Any]   # (method, path, body) -> parsed JSON


def _token() -> str | None:
    try:
        t = TOKEN_FILE.read_text().strip()
        return t or None
    except OSError:
        return None


def _http(method: str, path: str, body: Any = None) -> Any:
    tok = _token()
    if not tok:
        raise PermissionError(NOT_CONNECTED)
    req = urllib.request.Request(HA_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read().decode()
    try:
        return json.loads(raw)
    except ValueError:
        return raw


_AREAS: dict[str, Any] = {"at": 0.0, "map": {}}


def _areas(http: Http) -> dict[str, str]:
    """entity_id → area name (Home Assistant's template API; cached five minutes)."""
    if time.time() - _AREAS["at"] > 300:
        tpl = "{% for s in states %}{{ s.entity_id }}|{{ area_name(s.entity_id) or '' }}\n{% endfor %}"
        try:
            out = http("POST", "/api/template", {"template": tpl})
            _AREAS["map"] = {a: b for a, _, b in (l.partition("|") for l in str(out).splitlines()) if a}
        except Exception:  # noqa: BLE001
            _AREAS["map"] = {}
        _AREAS["at"] = time.time()
    return _AREAS["map"]


def _name(s: dict) -> str:
    return str((s.get("attributes") or {}).get("friendly_name") or s.get("entity_id"))


def _describe(s: dict) -> str:
    """One device, one line: "Living Room Lamp: on, 60%"."""
    a = s.get("attributes") or {}
    domain = s["entity_id"].split(".", 1)[0]
    state = s.get("state")
    unit = a.get("temperature_unit") or a.get("unit_of_measurement") or ""
    bits = [str(state)]
    if domain == "climate":
        bits = [f"{a.get('hvac_action') or state}"]
        if a.get("current_temperature") is not None:
            bits.append(f"{a['current_temperature']}{'°' if not unit else ''}{unit} inside".replace("°°", "°"))
        if a.get("temperature") is not None:
            bits.append(f"set to {a['temperature']}")
        elif a.get("target_temp_low") is not None:
            bits.append(f"set to {a.get('target_temp_low')}–{a.get('target_temp_high')}")
        bits.append(f"mode {state}")
        if a.get("current_humidity") is not None:
            bits.append(f"{a['current_humidity']}% humidity")
    elif domain == "light" and state == "on" and a.get("brightness") is not None:
        bits.append(f"{round(int(a['brightness']) * 100 / 255)}%")
    elif domain == "media_player" and a.get("media_title") and state in ("playing", "paused"):
        what = f"“{a['media_title']}”" + (f" by {a['media_artist']}" if a.get("media_artist") else "")
        bits = [f"playing {what}" if state == "playing" else f"paused on {what}"]
    elif domain in ("sensor",) and unit:
        bits = [f"{state} {unit}"]
    return f"{_name(s)}: {', '.join(bits)}"


# What counts as "on" for "what's on in the house" (#1356): a light, switch or fan that's on; a TV or speaker playing or
# paused; a thermostat whose mode isn't off. Idle media, open covers and locks aren't "on".
_ON_LINE_DOMAINS = ("light", "switch", "fan", "media_player", "climate")


def on_now(states: list[dict], *, hide_security: bool = False) -> list[str]:
    """The devices on right now, as "Name (state)" ("Living Room TV (playing)", "Thermostat (heat)"). Never a lock, alarm,
    cover or camera, nor a security-shaped device (a garage door opener switch), for ANY role (Coordination, 10-09: those
    have their own read-only path, and a general "on right now" line mustn't carry security states). hide_security is
    kept for the callers' symmetry with home_status; this list already leaves all of them out.
    Built in code from HA's states, so a local model only has to repeat it (#1356: after a real read the 8B said
    "everything is off" with a TV playing)."""
    out = []
    for s in states or []:
        eid = str(s.get("entity_id") or "")
        domain = eid.split(".", 1)[0]
        if domain not in _ON_LINE_DOMAINS or domain in HANDS_OFF:
            continue
        if sensitive(s):   # every role: never a security state in this general line
            continue
        st = str(s.get("state") or "").lower()
        if domain == "media_player":
            on = st in ("playing", "paused")
        elif domain == "climate":
            on = st not in ("off", "unavailable", "unknown", "")
        else:
            on = st == "on"
        if on:
            out.append(f"{_name(s)} ({st})" if domain in ("media_player", "climate") else _name(s))
    return sorted(out)


def home_status(args: dict, *, http: Http | None = None, hide_security: bool = False) -> str:
    """args: area (room), kind (light, climate, media_player…), name (contains).
    hide_security (kids, shared household devices): locks, alarms, garage doors and cameras
    aren't listed at all, nor security-shaped devices in other domains (sensitive())."""
    http = http or _http   # resolved per call, so a test can swap the module's client
    try:
        states = http("GET", "/api/states", None)
    except PermissionError as exc:
        return str(exc)
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"Couldn't reach Home Assistant: {exc}"
    areas = _areas(http)
    area = str(args.get("area") or "").strip().lower()
    kind = str(args.get("kind") or "").strip().lower().rstrip("s")
    kind = {"thermostat": "climate", "light switch": "switch", "tv": "media_player", "speaker": "media_player",
            "media": "media_player", "lock": "lock", "camera": "camera"}.get(kind, kind)
    name = str(args.get("name") or "").strip().lower()
    rows: dict[str, list[str]] = {}
    for s in states:
        eid = s.get("entity_id", "")
        domain = eid.split(".", 1)[0]
        if domain not in SHOWN and not (domain in ON_REQUEST and (kind or name)):
            continue
        if hide_security and (domain in HANDS_OFF or sensitive(s)):
            continue
        if s.get("state") == "unavailable" and not name:
            continue
        room = areas.get(eid) or "Elsewhere"
        if area and area not in room.lower():
            continue
        if kind and domain != kind:
            continue
        if name and name not in _name(s).lower() and name not in eid:
            continue
        rows.setdefault(room, []).append(_describe(s))
    if not rows:
        return "No matching devices in Home Assistant." if (area or kind or name) else \
            "Home Assistant has no devices yet: add them in its Settings › Devices & services."
    out = []
    for room in sorted(rows, key=lambda r: (r == "Elsewhere", r)):
        out.append(f"[{room}]")
        out += [f"• {line}" for line in sorted(rows[room])[:40]]
    return "\n".join(out)


def _find(states: list[dict], target: str) -> list[dict]:
    t = target.strip().lower()
    exact = [s for s in states if s.get("entity_id", "").lower() == t or _name(s).lower() == t]
    if exact:
        return exact
    words = [w for w in re.split(r"\W+", t) if w]
    return [s for s in states if all(w in (_name(s) + " " + s.get("entity_id", "")).lower() for w in words)]


# "here", "in here", "this room" (#433): the asker's own room hint, never a guess
_HERE_RX = re.compile(r"\b(?:in\s+)?(?:here|this\s+room)\b", re.I)
_KIND_WORDS = {"light": "light", "lights": "light", "lamp": "light", "lamps": "light", "tv": "media_player",
               "television": "media_player", "speaker": "media_player", "speakers": "media_player",
               "music": "media_player", "fan": "fan", "fans": "fan", "switch": "switch", "thermostat": "climate"}
NO_ROOM = "I don't know which room you're in right now. Which room?"


def _here(states: list[dict], target: str, room: str, http: Http) -> tuple[list[dict], str]:
    """The devices in the asker's room that the rest of the words name: (candidates, what's left of the words)."""
    areas = _areas(http)
    want = " ".join(room.split()).lower()
    inside = [st for st in states if " ".join(str(areas.get(st.get("entity_id", "")) or "").split()).lower() == want]
    rest = " ".join(w for w in re.split(r"\s+", _HERE_RX.sub(" ", target)) if w and w.lower() not in
                    ("the", "it", "a", "all", "my", "on", "off", "of"))
    kinds = {_KIND_WORDS[w.lower()] for w in rest.split() if w.lower() in _KIND_WORDS}
    if not rest or (kinds and all(w.lower() in _KIND_WORDS for w in rest.split())):
        return ([st for st in inside if st.get("entity_id", "").split(".", 1)[0] in kinds] if kinds else inside), rest
    return _find(inside, rest), rest


# "Turn everything off" (#1343, Coordination 10-09): lights, switches, fans, and TVs and speakers (paused when playing,
# else turned off; never a volume change). Never climate, covers (blinds too), locks, alarms, cameras, or anything
# security-shaped (sensitive()), for any role. It only ever acts after the person confirms the listed devices.
EVERYTHING_OFF_DOMAINS = frozenset({"light", "switch", "fan", "media_player"})
_ON_STATES = {"on", "playing", "paused", "idle", "buffering"}
EVERYTHING_RX = re.compile(r"^\s*(?:everything|all(?: the)?(?: devices| things| lights and switches)?|the whole house|"
                           r"every ?thing in the house)\s*$", re.I)


def everything_off_plan(states: list[dict], domains: frozenset[str] | set[str] | None = None) -> list[dict]:
    """What "turn everything off" would turn off right now: [{entity_id, name, domain}] for each light, switch, fan or
    media player that's on (a media player that's playing, paused or idle), within `domains` (the asker's role), never
    hands-off and never security-shaped. Names are HA's own friendly_name."""
    allowed = EVERYTHING_OFF_DOMAINS if domains is None else EVERYTHING_OFF_DOMAINS & set(domains)
    out = []
    for s in states or []:
        eid = str(s.get("entity_id") or "")
        domain = eid.split(".", 1)[0]
        if domain not in allowed or domain in HANDS_OFF or sensitive(s):
            continue
        state = str(s.get("state") or "").lower()
        if (domain == "media_player" and state in _ON_STATES) or (domain != "media_player" and state == "on"):
            out.append({"entity_id": eid, "name": _name(s), "domain": domain})
    return sorted(out, key=lambda x: (x["domain"], x["name"]))


def everything_off(entity_ids: list[str], *, http: Http | None = None, domains: frozenset[str] | set[str] | None = None,
                   dry_run: bool = False) -> str:
    """Turn off what the person confirmed (`entity_ids`, from the plan they saw), re-checked against the live states:
    only the ones still on and still allowed (the plan's rules again), so a device that changed, or a list that was
    tampered with, can't widen it. Media: media_pause when playing, else turn_off."""
    http = http or _http
    try:
        states = http("GET", "/api/states", None)
    except (urllib.error.URLError, TimeoutError, PermissionError) as exc:
        return f"Couldn't reach Home Assistant: {exc}"
    still = {d["entity_id"]: d for d in everything_off_plan(states, domains)}
    by_id = {str(s.get("entity_id")): s for s in states or []}
    done, failed = [], []
    for eid in dict.fromkeys(str(e) for e in entity_ids or []):
        d = still.get(eid)
        if not d:
            continue
        playing = d["domain"] == "media_player" and str(by_id[eid].get("state")).lower() == "playing"
        service = "media_player/media_pause" if playing else f"{d['domain']}/turn_off"
        if dry_run:
            done.append(d["name"])
            continue
        try:
            http("POST", f"/api/services/{service}", {"entity_id": eid})
            done.append(d["name"] + (" (paused)" if playing else ""))
        except (urllib.error.URLError, TimeoutError) as exc:
            failed.append(f"{d['name']} ({type(exc).__name__})")
    if not done and not failed:
        return "Nothing to turn off: what you confirmed is already off."
    return (("Done (eval: not sent): " if dry_run else "Turned off: ") + ", ".join(done) + "."
            + (f" Couldn't turn off: {', '.join(failed)}." if failed else ""))


def home_control(args: dict, *, http: Http | None = None, domains: frozenset[str] | set[str] | None = None,
                 refusal: str = "That's not something I can do for you.", dry_run: bool = False,
                 here: str | None = None, restricted: bool = False) -> str:
    """args: device (name or entity id), action (on, off, toggle, set_temperature, set_mode,
    brightness, play, pause, volume, activate), value (a number or a mode).
    domains: the domains this person may change (a kid's shorter list); anything else gets
    `refusal`, and so do the hands-off domains (without saying what the device is).
    dry_run: read the real state and plan the real call, but send nothing (eval turns), so an
    eval sees the same refusals, notes and ranges a person would.
    here: the asker's own room (room_hint, #433) for a device named by "here" / "this room"; None asks which room.
    restricted (kids, shared household screens; implied by `domains`): a security-shaped device (sensitive()) gets
    `refusal` in any domain, and isn't offered among several matches."""
    http = http or _http   # resolved per call, so a test can swap the module's client
    target = str(args.get("device") or args.get("entity") or "").strip()
    action = str(args.get("action") or "").strip().lower().replace(" ", "_")
    value = args.get("value")
    if not target or not action:
        return "Say which device and what to do (e.g. device='Living Room Lamp', action='off')."
    try:
        states = http("GET", "/api/states", None)
    except PermissionError as exc:
        return str(exc)
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"Couldn't reach Home Assistant: {exc}"
    if _HERE_RX.search(target):
        if not here:
            return NO_ROOM
        hits, rest = _here(states, target, here, http)
        if not hits:
            return f"No {rest or 'device'} in the {here} that Home Assistant knows about."
    else:
        hits = _find(states, target)
    if not hits:
        return f"No device called “{target}” in Home Assistant."
    restricted = restricted or domains is not None
    if restricted:
        # hands-off domains keep their own answer below (a lock is "a lock", or the refusal for a kid)
        hits = [h for h in hits if h.get("entity_id", "").split(".", 1)[0] in HANDS_OFF or not sensitive(h)]
        if not hits:
            return refusal
    if domains is not None:
        mine = [h for h in hits if h.get("entity_id", "").split(".", 1)[0] in domains - HANDS_OFF]
        if not mine:
            return refusal
        hits = mine
    if len(hits) > 1:
        return "Several devices match; ask which one:\n" + "\n".join(f"• {_name(s)} ({s['entity_id']})" for s in hits[:10])
    s = hits[0]
    eid = s["entity_id"]
    domain = eid.split(".", 1)[0]
    if domains is not None and (domain not in domains or domain in HANDS_OFF):
        return refusal
    if domain in HANDS_OFF:
        return (f"{_name(s)} is a {domain.replace('_', ' ')}: titan doesn't change those yet. "
                "Use Home Assistant for it.")
    if domain not in CONTROLLABLE:
        return f"{_name(s)} ({domain}) isn't something titan can change."
    try:
        service, data = _service(domain, action, value, s)
        note = data.pop("_note", None)
        if data.get("_remote"):   # a remote button on a TV: sent to its remote entity (same device)
            data.pop("_remote")
            rid = "remote." + eid.split(".", 1)[1]
            eid = rid
    except ValueError as exc:
        return str(exc)
    if dry_run:
        return ("Done (eval: not sent): " + f"{_name(s)} → {service.split('.', 1)[1].replace('_', ' ')}"
                + (f" {', '.join(f'{k} {v}' for k, v in data.items())}" if data else "") + "." + (f" {note}" if note else ""))
    try:
        http("POST", f"/api/services/{service.replace('.', '/')}", {"entity_id": eid, **data})
    except urllib.error.HTTPError as exc:
        return f"Home Assistant refused that ({exc.code}): {exc.reason}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return f"Couldn't reach Home Assistant: {exc}"
    return f"Done: {_name(s)} → {service.split('.', 1)[1].replace('_', ' ')}" + \
        (f" {', '.join(f'{k} {v}' for k, v in data.items())}" if data else "") + \
        (". (Honeywell's cloud can take a few minutes to show the change.)" if domain == "climate" else ".") + \
        (f" {note}" if note else "")


def _fmt(v: Any) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def _num(v: Any) -> float:
    try:
        return float(str(v).strip().rstrip("°FfCc%").strip())
    except ValueError:
        raise ValueError(f"“{v}” isn't a number.") from None


# Apple TV remote buttons (Home Assistant's apple_tv remote.send_command), by what people call them.
REMOTE_BUTTONS = {"up": "up", "down": "down", "left": "left", "right": "right", "select": "select", "ok": "select",
                  "enter": "select", "menu": "menu", "back": "menu", "home": "home", "tv": "home",
                  "top_menu": "top_menu", "play_pause": "play_pause", "skip_forward": "skip_forward",
                  "skip_backward": "skip_backward", "wake": "wakeup", "wakeup": "wakeup", "sleep": "suspend",
                  "suspend": "suspend"}


def _norm_app(v: Any) -> str:
    return " ".join(str(v or "").replace("\u00a0", " ").lower().split()).replace("+", " plus").strip()


def _service(domain: str, action: str, value: Any, s: dict) -> tuple[str, dict]:
    a = s.get("attributes") or {}
    if action in ("on", "turn_on") and domain != "scene":
        return f"{domain}.turn_on", {}
    if action in ("off", "turn_off"):
        return f"{domain}.turn_off", {}
    if action == "toggle":
        return f"{domain}.toggle", {}
    if domain == "scene" and action in ("activate", "on", "turn_on", "run"):
        return "scene.turn_on", {}
    if domain == "light" and action in ("brightness", "dim", "set_brightness"):
        pct = max(1, min(100, int(_num(value))))
        return "light.turn_on", {"brightness_pct": pct}
    if domain == "climate" and action in ("set_temperature", "temperature", "set", "set_range"):
        unit = str(a.get("temperature_unit") or "").upper()
        parts = [p for p in re.split(r"\s*(?:-|–|—|to)\s*", str(value or "").strip()) if p]
        temps = [_num(p) for p in parts[:2]]
        if not temps:
            raise ValueError("Say a temperature (e.g. 70) or a range (e.g. 68-72).")
        lo, hi = TEMP_C if "C" in unit or (not unit and temps[0] < 40) else TEMP_F
        for t in temps:
            if not lo <= t <= hi:
                raise ValueError(f"{t:g}° is outside the safe range ({lo}–{hi}°); not changed.")
        in_range_mode = s.get("state") == "heat_cool" or (a.get("target_temp_low") is not None and a.get("temperature") is None)
        if len(temps) == 2:
            low, high = sorted(temps)
            if high - low < 2:
                raise ValueError("Keep at least 2° between the low and high settings.")
            return "climate.set_temperature", {"target_temp_low": low, "target_temp_high": high}
        if in_range_mode:
            # One number on Auto (heat_cool): keep the mode and move the range so it centres on that
            # number, the same width as now (09-30: "set it to 72" on 69–73 switched the mode to heat).
            cur_lo, cur_hi = a.get("target_temp_low"), a.get("target_temp_high")
            width = max(2.0, float(cur_hi) - float(cur_lo)) if cur_lo is not None and cur_hi is not None else 4.0
            low = round(temps[0] - width / 2)
            high = low + round(width)
            if not (lo <= low and high <= hi):
                raise ValueError(f"A range around {temps[0]:g}° ({low:g}–{high:g}°) is outside the safe range ({lo}–{hi}°); "
                                 "not changed.")
            was = f" (was {_fmt(cur_lo)}–{_fmt(cur_hi)})" if cur_lo is not None and cur_hi is not None else ""
            return "climate.set_temperature", {"target_temp_low": float(low), "target_temp_high": float(high), "_note": (
                f"It's on Auto (heat/cool), which holds a range, so the range now centres on {temps[0]:g}°: "
                f"{low:g}–{high:g}°{was}. The mode is unchanged. Tell the user, and that a different range or a "
                "switch to heat or cool is theirs to ask for.")}
        return "climate.set_temperature", {"temperature": temps[0]}
    if domain == "climate" and action in ("set_mode", "mode", "set_hvac_mode"):
        mode = str(value or "").strip().lower()
        modes = [m.lower() for m in a.get("hvac_modes") or []]
        if modes and mode not in modes:
            raise ValueError(f"The thermostat's modes are {', '.join(modes)}; “{mode}” isn't one.")
        return "climate.set_hvac_mode", {"hvac_mode": mode}
    if domain == "media_player" and action in ("play", "pause", "stop", "next", "previous"):
        svc = {"play": "media_play", "pause": "media_pause", "stop": "media_stop",
               "next": "media_next_track", "previous": "media_previous_track"}[action]
        return f"media_player.{svc}", {}
    if domain == "media_player" and action in ("open", "launch", "app", "source", "open_app", "select_source"):
        want = _norm_app(value)
        if not want:
            raise ValueError("Say which app to open.")
        apps = [str(x) for x in a.get("source_list") or []]
        hit = (next((x for x in apps if _norm_app(x) == want), None)
               or next((x for x in apps if _norm_app(x).startswith(want)), None)
               or next((x for x in apps if want in _norm_app(x)), None))
        if not hit:
            listed = ", ".join(x.replace("\u00a0", " ") for x in apps[:25]) or "none listed"
            raise ValueError(f"{_name(s)} has no app called “{value}”. Its apps: {listed}.")
        return "media_player.select_source", {"source": hit}
    if domain == "media_player" and action in ("mute", "unmute"):
        return "media_player.volume_mute", {"is_volume_muted": action == "mute"}
    if domain == "media_player" and action in ("volume_up", "louder", "volume_down", "quieter"):
        return f"media_player.volume_{'up' if action in ('volume_up', 'louder') else 'down'}", {}
    if domain == "media_player" and action in ("remote", "press", "button", "key"):
        cmd = REMOTE_BUTTONS.get(str(value or "").strip().lower().replace(" ", "_"))
        if not cmd:
            raise ValueError(f"The remote's buttons are: {', '.join(sorted(set(REMOTE_BUTTONS)))}.")
        return "remote.send_command", {"command": cmd, "_remote": True}
    if domain == "media_player" and action in ("volume", "set_volume"):
        return "media_player.volume_set", {"volume_level": max(0.0, min(1.0, _num(value) / 100))}
    if domain == "fan" and action in ("speed", "set_speed", "percentage"):
        return "fan.set_percentage", {"percentage": max(0, min(100, int(_num(value))))}
    raise ValueError(f"“{action}” isn't something titan can do to a {domain.replace('_', ' ')}.")
