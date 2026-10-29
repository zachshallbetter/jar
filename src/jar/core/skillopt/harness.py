#!/usr/bin/env python3
"""Score a household skill (scripts/skills/<name>/SKILL.md) on its task bank, through the real turn.

Each task is one question (optionally with history) sent to titan's HTTP API as an eval turn:
nothing is remembered, logged, carded or acted on (tools with real effects are simulated, device
tools are answered by the fake Mac below, fixture dates are relative to today). A candidate skill
is sent as `skill_overrides`, so the Jev skill pick and the prompt are exactly production's.

Tasks live in tasks/<skill>.jsonl, one per line:

  {"id": "cal-tomorrow", "question": "What's on my calendar tomorrow?", "history": [],
   "expect": {"tools_any": ["list_calendar_events"], "reply_all": ["piano", "dinner"],
              "reply_none": ["standup"]}}

Checks (each counts once; a task's score is the share that passed):
  tools_all / tools_any / tools_none   tool names, Mac device tools and titan tools alike
  reply_all / reply_any / reply_none   case-insensitive substrings of the reply
  args  [{"tool", "key", "re"}]        a device tool was called with args[key] matching re
  reply_re  [pattern]                  the reply matches each regex (case-insensitive)
  calls_max {tool: n}                  a tool was called at most n times (device calls, or titan
                                       tools by their status lines)
Question and substring text may use {weekday+N} / {date+N} (N days from today).

  python3 scripts/skillopt/harness.py --skill calendar [--candidate FILE] [--task ID] [-v] [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Task banks with no skill of their own: scored for regressions, never optimised (run.py needs a
# SKILL.md). health: Apple Health on the iPhone (titan-node#247).
REGRESSION_BANKS = frozenset({"health"})
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import turn_eval as te  # noqa: E402  (fake calendar, fixtures, token, time zone)
from ops_ports import AGENT_API_URL  # noqa: E402

API = os.environ.get("TURN_EVAL_API", AGENT_API_URL)
TASKS = HERE / "tasks"
SKILLS = SCRIPTS / "skills"


# ── fake Mac: turn_eval's fixtures plus what the skills' tasks need ──────────────────────────────

# Beyond turn_eval's events: an event that shows up on two calendars (the real 2026-09-30 School
# pickup case) and two events at the same time. (day offset, hour, minute, title, calendar, id)
EXTRA_EVENTS = [(3, 10, 30, "Coffee with Sam", "Home", "E31"), (3, 10, 30, "Swim practice", "Kids", "E32"),
                (5, 14, 50, "School pickup", "Family", "E51"), (5, 14, 50, "School pickup", "Home", "E52")]
# A day with the user's own event and two on a calendar Robin shares, in Jellybean's calendar-note
# format (" [calendar: <title>]", ", shared" when it isn't the user's). (day offset, hour, minute,
# title, note)
NOTED_EVENTS = [(6, 11, 0, "Team sync", "Work"), (6, 15, 0, "Quinn's appointment", "Robin, shared"),
                (6, 18, 0, "Leo's skate night", "Robin, shared"), (6, 20, 0, "Movie night", "Family, shared")]


def _events_in(args: dict, gone: set, created: list | None = None) -> list[str]:
    """Like the Mac's list_calendar_events over turn_eval's events plus EXTRA_EVENTS, in time order,
    without what this turn deleted, with what it created or moved."""
    start = te._parse(args.get("start")) or te._today()
    raw_end = str(args.get("end") or "")
    end = te._parse(raw_end) or start + timedelta(days=7)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_end.strip()):
        end += timedelta(days=1)   # a bare end date covers that day
    rows = [(d, te._line(d, t)) for d, t in te.fixture_events() if t not in gone]
    for days, h, m, title, cal, eid in EXTRA_EVENTS:
        if eid not in gone:
            d = te._today() + timedelta(days=days, hours=h, minutes=m)
            rows.append((d, f"{te._line(d, title)} ({cal}) [event_id: {eid}]"))
    for days, h, m, title, note in NOTED_EVENTS:
        if title not in gone:
            d = te._today() + timedelta(days=days, hours=h, minutes=m)
            rows.append((d, f"{te._line(d, title)} [calendar: {note}]"))
    for d, title, cal in created or []:
        rows.append((d, f"{te._line(d, title)} [calendar: {cal}]"))
    return [line for d, line in sorted(rows) if start <= d < end]


REMINDERS = ("• Buy milk (due today)\n"
             "• Renew passport (due {date+10})\n"
             "• Call Rivera Plumbing about the slow drain (no due date)")
# Read-only Mac tools the dedicated-tool tasks need (fictional content).
MESSAGES = ("[Messages: 2 matches, newest first]\n"
            "• Bright Smile Dental (yesterday 4:12 PM): Reminder: your cleaning is {weekday+6} at 9:40 AM. Reply C to confirm.\n"
            "• Sam (Tue 8:03 PM): Coffee Saturday at 10:30 still good? Blue Door Cafe")
FILE_HITS = ("[search_file_contents: 3 matches in ~/Projects/demo]\n"
             "~/Projects/demo/src/main.py:14: # TODO: retry the upload\n"
             "~/Projects/demo/src/api.py:88: # TODO: paginate\n"
             "~/Projects/demo/README.md:3: TODO: screenshots")
GIT = "~/Projects/demo on main — 2 modified: src/main.py, README.md · 1 untracked: notes.txt · up to date with origin"
PORTS = "5173  node (vite)  pid 4411\n7770  ssh tunnel  pid 902"
LOG_TAIL = ("[~/Projects/demo/server.log, last 3 lines]\n"
            "12:01:07 GET /health 200\n12:01:09 POST /upload 500 timeout after 30s\n12:01:12 GET /health 200")
FOUND = "~/Projects/demo/README.md\n~/Projects/demo/docs/README.old.md"
PLEX = json.dumps({"sessions": [{"title": "The Grand Budapest Hotel", "type": "movie", "player": "Living Room TV",
                                 "state": "playing", "progress_pct": 42}], "server": "reachable"})


def render(text: str) -> str:
    """Relative to now (household TZ): {weekday+N} → Friday, {date+N} → 2026-10-02, {day+N} → Fri Oct 2
    (an event line's date), {time+M} → 4:35 PM and {hm+M} → 16:35 (M minutes from now, on 5 minutes)."""
    t = te._today()
    now = datetime.now(t.tzinfo)
    now = now.replace(second=0, microsecond=0) + timedelta(minutes=(5 - now.minute % 5) % 5)

    def sub(m: re.Match) -> str:
        kind, n = m.group(1), int(m.group(2))
        if kind in ("time", "hm"):
            d = now + timedelta(minutes=n)
            return f"{d.hour % 12 or 12}:{d:%M} {'AM' if d.hour < 12 else 'PM'}" if kind == "time" else d.strftime("%H:%M")
        d = t + timedelta(days=n)
        return {"weekday": d.strftime("%A"), "date": d.strftime("%Y-%m-%d"), "day": f"{d:%a %b} {d.day}"}[kind]
    return re.sub(r"\{(weekday|date|day|time|hm)\+(\d+)\}", sub, text)


def _match(title: str, lines: list[str]) -> str | None:
    """The fixture title a delete names (the Mac matches by title, case-insensitively)."""
    low = title.lower()
    return next((l for l in lines if low and low in l.lower()), None)


def _stamp(d) -> str:
    """The Mac's EventText.stamp ('Wed Oct 7, 2:50 PM')."""
    return te._line(d, "x")[2:].split(" — ")[0]


def _when(value):
    d = te._parse(value)
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=te._today().tzinfo)
    return d


def _find_event(title: str, gone: set, created: list):
    """(start, title, calendar, forget) for the first event whose title matches, or None."""
    low = title.lower()
    for i, (d, t, cal) in enumerate(created):
        if low in t.lower():
            return d, t, cal, lambda i=i: created.pop(i)
    for d, t in te.fixture_events():
        if t not in gone and low in t.lower():
            return d, t, "Home", lambda t=t: gone.add(t)
    for days, h, m, t, cal, eid in EXTRA_EVENTS:
        if eid not in gone and low in t.lower():
            return te._today() + timedelta(days=days, hours=h, minutes=m), t, cal, lambda e=eid: gone.add(e)
    for days, h, m, t, note in NOTED_EVENTS:
        if t not in gone and low in t.lower():
            return te._today() + timedelta(days=days, hours=h, minutes=m), t, note, lambda t=t: gone.add(t)
    return None


# The iPhone's travel_time, in its own words (notes/location-coordination-2026-09-30.md, v3; Jellybean's
# TravelToolTests pins them). Picked by destination; fictional places.
# Times are relative to now ({time+M}), so they never contradict the clock the model is given.
TRAVEL = [
    ("smile studio", "Smile Studio (2210 NE Broadway, Portland, OR): 22 min by car, 4.9 mi, from this iPhone at "
                     "{time+0}. Leave by tomorrow 8:33 AM to arrive by tomorrow 9:00 AM."),
    ("appointment", "Which one: Dentist (today {time+120}, Pearl Dental, on={date+0}T{hm+120}); "
                    "Dentist (tomorrow 9:00 AM, Smile Studio, on={date+1}T09:00)"),
    ("dentist", "Dentist (today {time+120}) at Pearl Dental (1101 NW Lovejoy St, Portland, OR): 14 min by car, 1.8 mi, "
                "from this iPhone at {time+0}. Leave by {time+101} to arrive by {time+120}."),
    ("pearl", "Pearl Dental (1101 NW Lovejoy St, Portland, OR): 14 min by car, 1.8 mi, from this iPhone at {time+0}."),
    ("standup", "“Standup” (today {time+60}) has no place on it."),
    ("ivan", "“Call with Ivan” (today {time+180}) is a call, not a place."),
    ("school", "Pickup Leo (today {time+10}) at Sunnyside Elementary (3421 SE Salmon St, Portland, OR): 18 min by car, "
               "6.2 mi, from this iPhone at {time+0}. About 8 min late if you leave now (starts {time+10})."),
]


def fake_travel(args: dict) -> str:
    dest = str(args.get("destination") or "").strip()
    if not dest:
        return "Say where to: a place, an address, or an event's title."
    low = dest.lower()
    on = str(args.get("on") or "")
    if on and any(k in low for k in ("appointment", "dentist")):
        # The "Which one" follow-up: the same destination and the chosen event's on=.
        return render(TRAVEL[0][1] if on.startswith(render("{date+1}")) else
                      next(line for key, line in TRAVEL if key == "dentist"))
    out = next((render(line) for key, line in TRAVEL if key in low), f'Couldn\'t find "{dest}" on the map.')
    mode = str(args.get("mode") or "drive").lower()
    if mode in ("transit", "walk") and " by car" in out:
        out = out.replace(" by car", " by transit" if mode == "transit" else " on foot")
    return out


# The iPhone's Music app tools, in their own words (notes/location-coordination-2026-09-30.md, Apple
# Music contract v1; Jellybean's MusicToolTests pins them). Fictional library.
MUSIC = [("kind of blue", "Playing the album “Kind of Blue” by Miles Davis on this iPhone."),
         ("clair de lune", "Playing “Clair de Lune” by Claude Debussy on this iPhone."),
         ("morning mix", "Playing the playlist “Morning Mix” on this iPhone."),
         ("radiohead", "Playing songs by Radiohead on this iPhone."),
         ("pure jazz", "Playing the station “Pure Jazz” on this iPhone.")]
NOW_PLAYING = "Playing on this iPhone: “So What” by Miles Davis, from “Kind of Blue” (1:23 of 3:45)."
MUSIC_CONTROL = {"pause": "Paused.", "play": "Playing “So What” by Miles Davis.",
                 "next": "Skipped to “Freddie Freeloader” by Miles Davis.", "previous": "Back to “So What” by Miles Davis.",
                 "shuffle_on": "Shuffle on.", "shuffle_off": "Shuffle off.", "repeat_on": "Repeat on.",
                 "repeat_off": "Repeat off."}


def fake_music(tool: str, args: dict, state: dict) -> str:
    if tool == "play_music":
        q = str(args.get("query") or "").strip()
        hit = next((line for key, line in MUSIC if key in q.lower()), None)
        if hit:
            state["music"] = "playing"
            return hit
        return f"Couldn't find “{q}” in your music or Apple Music."
    if tool == "music_control":
        action = str(args.get("action") or "").strip().lower()
        if state.get("music") == "stopped":
            return "Nothing is playing on this iPhone."
        state["music"] = "paused" if action == "pause" else "playing"
        return MUSIC_CONTROL.get(action, "Couldn't start playback.")
    if state.get("music") == "paused":
        return "Paused on this iPhone: “So What” by Miles Davis."
    return NOW_PLAYING if state.get("music") != "stopped" else "Nothing is playing on this iPhone."


def fake_mac(tool: str, args: dict, state: dict | None = None) -> str:
    """Answers device tools from fixtures. `state` (one per turn) remembers deletions, so a re-read
    after a delete no longer shows it, like the real Mac; deletes answer in the Mac's own words."""
    state = state if state is not None else {}
    overrides = state.get("overrides") or {}
    if tool in overrides:   # a task's own device answers ("device": {tool: text}), e.g. its day's calendar
        return render(overrides[tool])
    gone: set[str] = state.setdefault("deleted", set())
    created: list = state.setdefault("created", [])
    title = str(args.get("title") or args.get("name") or "").strip()
    if tool == "delete_calendar_event":
        eid = str(args.get("event_id") or "").strip()
        extra = [(e[3], e[5]) for e in EXTRA_EVENTS if e[5] not in gone]
        if eid:
            hit = next((t for t, i in extra if i == eid), None)
            if not hit:
                return f"Couldn't find an event with id {eid}. Nothing changed."
            gone.add(eid)
            return f"Cancelled “{hit}” on {args.get('on') or 'that day'}"
        same = [i for t, i in extra if title and title.lower() in t.lower()]
        if len(same) > 1:
            return (f"Nothing changed: {len(same)} events match “{title}”. Ask the user which one, then call "
                    f"delete_calendar_event again with its event_id ({', '.join(same)}).")
        if same:
            gone.add(same[0])
            return f"Cancelled “{next(t for t, i in extra if i == same[0])}” on {args.get('on') or 'that day'}"
        hit = _match(title, [t for _, t in te.fixture_events() if t not in gone])
        if not hit:
            return f"Couldn't find an event “{title}”. Nothing changed."
        gone.add(hit)
        return f"Cancelled “{hit}” on {args.get('on') or 'that day'}"
    if tool == "delete_reminder":
        names = ["Buy milk", "Renew passport", "Call Rivera Plumbing about the slow drain"]
        hit = _match(title, [n for n in names if n not in gone])
        if not hit:
            return f"Couldn't find an open reminder “{title}”. Nothing changed."
        gone.add(hit)
        return f"Deleted reminder “{hit}”."
    if tool == "list_calendar_events":
        return "\n".join(_events_in(args, gone, created)) or "No events in that range."
    if tool == "list_reminders":
        return "\n".join(l for l in render(REMINDERS).splitlines() if not any(g in l for g in gone)) \
            or "No incomplete reminders."
    if tool == "create_calendar_event":
        # The Mac's wording, and the event really appears on a re-read (titan checks after a create).
        d = _when(args.get("start"))
        if d is None:
            return "Couldn't understand the start time."
        cal = str(args.get("calendar") or "").strip() or "Home"
        created.append((d, title or "New event", cal))
        return f"Created '{title or 'New event'}' on {_stamp(d)} on the {cal} calendar."
    if tool == "update_calendar_event":
        hit = _find_event(title, gone, created) if title else None
        if not hit:
            return f"Couldn't find an event “{title}”. Nothing changed."
        d, old, cal, forget = hit
        new_d, new_t = _when(args.get("start")) or d, str(args.get("new_title") or "").strip() or old
        forget()
        created.append((new_d, new_t, cal))
        if new_d != d:
            return f"Moved “{old}” from {_stamp(d)} to {_stamp(new_d)}." + (f" Renamed it “{new_t}”." if new_t != old else "")
        return f"Renamed “{old}” to “{new_t}” ({_stamp(d)})." if new_t != old else f"Updated “{old}” ({_stamp(d)})."
    if tool == "create_reminder":
        return f"Created reminder “{title or 'reminder'}”"
    if tool in ("complete_reminder", "update_reminder"):
        return f"Done: {tool.replace('_', ' ')} “{title}”"
    if tool == "set_alarm":
        return f"Alarm set for {args.get('time') or args.get('at') or 'the requested time'}"
    if tool == "travel_time":
        return fake_travel(args)
    if tool in ("play_music", "music_control", "now_playing"):
        return fake_music(tool, args, state)
    if tool == "get_plex_state":
        return PLEX
    if tool == "search_messages":
        return render(MESSAGES)
    if tool == "search_file_contents":
        return FILE_HITS
    if tool == "git_status":
        return GIT
    if tool == "list_listening_ports":
        return PORTS
    if tool == "read_file" and "server.log" in str(args.get("path", "")):
        return LOG_TAIL
    if tool == "find_files" and "readme" in str(args.get("query", "")).lower():
        return FOUND
    if tool == "run_shortcut":
        return f"Ran shortcut “{args.get('name', '')}”"
    return te.fake_mac(tool, args)


# ── one turn ─────────────────────────────────────────────────────────────────────────────────────

def _status_tools() -> dict[str, set[str]]:
    """Status text → titan tools that announce themselves with it (the turn's SSE status lines).
    Read from agent_runtime_lib's source, not imported: the SkillOpt venv doesn't carry the agent's
    dependencies, and the harness must work in both."""
    import ast
    src = (SCRIPTS / "agent_runtime_lib.py").read_text()
    for node in ast.parse(src).body:
        target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) else None)
        if isinstance(target, ast.Name) and target.id == "_TOOL_STATUS":
            out: dict[str, set[str]] = {}
            for name, text in ast.literal_eval(node.value).items():
                out.setdefault(text, set()).add(name)
            return out
    raise RuntimeError("_TOOL_STATUS not found in agent_runtime_lib.py")


STATUS_TOOLS = _status_tools()
# Device reads titan's situational context may make before the model runs (_situational_context).
CONTEXT_TOOLS = {"get_field_state", "get_plex_state"}


@dataclass
class Turn:
    text: str = ""
    device: list[tuple[str, dict]] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    ms: int = 0
    error: str = ""
    context: list[str] = field(default_factory=list)   # device reads titan made itself, before the model
    skills: dict | None = None   # eval turns: {"picked": [names] | None, "body": bool, "read": [names]}
                                 # (the guide preloaded, and the guides the model read with skill_guide)

    def tools(self) -> set[str]:
        """The tools the model chose. Titan's own per-turn context reads (screen state and Plex,
        fetched by fixed patterns before the model runs) aren't the skill's doing: see `context`."""
        used = {t for t, _ in self.device}
        for s in self.status:
            used |= STATUS_TOOLS.get(s.strip(), set())
        return used


class InfraError(RuntimeError):
    """titan's API couldn't be reached (a restart or deploy): not the skill's fault."""


def _api_up(wait_s: float) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{API}/health", timeout=3).read()
            return True
        except Exception:  # noqa: BLE001
            time.sleep(3)
    return False


def ask(question: str, history: list | None = None, skill_overrides: dict | None = None,
        timeout: int = 180, attempts: int = 4, client: str | None = None, mac: str | None = None,
        device: dict | None = None) -> Turn:
    """One eval turn. If the API is unreachable (a restart or deploy mid-run), wait for it and try
    again; if it stays down, raise InfraError rather than score an outage as a skill failure."""
    for attempt in range(attempts):
        t = _ask_once(question, history, skill_overrides, timeout, client=client, mac=mac, device=device)
        infra = t.error and any(k in t.error for k in ("URLError", "Connection refused", "RemoteDisconnected",
                                                        "ConnectionResetError", "HTTP Error 502", "HTTP Error 503"))
        if not infra:
            return t
        if not _api_up(180):
            break
    raise InfraError(f"titan's agent API is unreachable: {t.error}")


def _ask_once(question: str, history: list | None, skill_overrides: dict | None, timeout: int,
              client: str | None = None, mac: str | None = None, device: dict | None = None) -> Turn:
    """client "iphone" asks as the phone (a task's "client"); mac is the simulated Mac behind it
    ("eval_mac": live | wakeable | unreachable | never; an eval turn never uses a real Mac)."""
    tok = te._token()
    H = {"X-Agent-Token": tok, "Content-Type": "application/json"}
    import eval_names as _names   # fictional names here, real ones on titan's side (#878)
    body = {"prompt": _names.out(question), "history": _names.out(history or []), "tools": True, "device": True,
            "detail": "brief", "source": "eval", "remember": False, "device_trust_tiers": te.TIERS,
            "eval_device": "fake"}   # fake_mac answers every device request
    if skill_overrides:
        body["skill_overrides"] = skill_overrides
    if client:
        body["client"] = client
        if client == "iphone":
            body["device_tools"] = te.phone_tools()
    if mac:
        body["eval_mac"] = mac
    out, event, t0, mac_state = Turn(), None, time.time(), {"overrides": device or {}}
    req = urllib.request.Request(f"{API}/v1/prompt/stream", data=json.dumps(body).encode(), headers=H)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                    continue
                if not line.startswith("data: "):
                    continue
                d = json.loads(line[6:])
                if event == "status":
                    out.status.append(_names.back(str(d.get("text", ""))))
                elif event == "device":
                    tool, args = d["tool"], _names.back(d.get("args") or {})
                    announced = bool(out.status) and tool in STATUS_TOOLS.get(out.status[-1].strip(), set())
                    if announced or tool not in CONTEXT_TOOLS:
                        out.device.append((tool, args))
                    else:
                        out.context.append(tool)
                    rq = urllib.request.Request(f"{API}/v1/device/result/{d['turn_id']}", headers=H,
                                                data=json.dumps({"request_id": d["request_id"],
                                                                 "result": _names.out(fake_mac(tool, args, mac_state))}).encode())
                    urllib.request.urlopen(rq, timeout=10).read()
                elif event == "done":
                    out.text = _names.back(d.get("text", ""))
                    out.skills = d.get("skills")
                    break
                elif event == "error":
                    out.error = str(d)[:200]
                    break
    except Exception as exc:  # noqa: BLE001
        out.error = f"{type(exc).__name__}: {exc}"[:200]
    out.ms = int((time.time() - t0) * 1000)
    return out


# ── scoring ──────────────────────────────────────────────────────────────────────────────────────

def score(task: dict, t: Turn) -> tuple[float, list[str]]:
    """(share of checks passed, one line per failed check)."""
    if t.error:
        return 0.0, [f"turn failed: {t.error}"]
    e, low, used = task.get("expect", {}), t.text.lower(), t.tools()
    checks: list[tuple[bool, str]] = []
    for name in e.get("tools_all", []):
        checks.append((name in used, f"didn't call {name}"))
    if e.get("tools_any"):
        checks.append((bool(used & set(e["tools_any"])), f"called none of {e['tools_any']}"))
    for name in e.get("tools_none", []):
        checks.append((name not in used, f"called {name}, which it shouldn't"))
    for s in e.get("reply_all", []):
        checks.append((render(s).lower() in low, f"reply is missing “{render(s)}”"))
    if e.get("reply_any"):
        opts = [render(s).lower() for s in e["reply_any"]]
        checks.append((any(o in low for o in opts), f"reply says none of {opts}"))
    for s in e.get("reply_none", []):
        checks.append((render(s).lower() not in low, f"reply says “{render(s)}”, which it shouldn't"))
    for pat in e.get("reply_re", []):
        checks.append((bool(re.search(render(pat), t.text, re.I)), f"reply doesn't match /{render(pat)}/"))
    for tool, most in e.get("calls_max", {}).items():
        n = sum(1 for x, _ in t.device if x == tool) + sum(1 for s in t.status if STATUS_TOOLS.get(s.strip()) == {tool})
        checks.append((n <= most, f"called {tool} {n} times (at most {most})"))
    for a in e.get("args", []):
        hit = any(tool == a["tool"] and re.search(render(a["re"]), str(args.get(a["key"], "")), re.I)
                  for tool, args in t.device)
        checks.append((hit, f"{a['tool']} not called with {a['key']} ~ /{a['re']}/"))
    if not checks:
        return 1.0, []
    failed = [why for ok, why in checks if not ok]
    return (len(checks) - len(failed)) / len(checks), failed


def load_tasks(skill: str) -> list[dict]:
    path = TASKS / f"{skill}.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip() and not l.startswith("//")]


def run_task(task: dict, skill: str, candidate: str | None = None) -> dict:
    """One scored rollout, with the trajectory SkillOpt reflects on."""
    history = [dict(m, content=render(str(m.get("content", "")))) for m in task.get("history") or []]
    t = ask(render(task["question"]), history, {skill: candidate} if candidate else None,
            client=task.get("client"), mac=task.get("mac"), device=task.get("device"))
    s, failed = score(task, t)
    return {"id": task["id"], "score": s, "failed": failed, "tools": sorted(t.tools()),
            "device_calls": [{"tool": x, "args": a} for x, a in t.device], "context_reads": t.context,
            # shown: the pick preloaded the skill's guide; read: the model fetched it with skill_guide
            # (kept apart so a pick regression doesn't hide behind the extra round trip)
            "skill_shown": None if t.skills is None else bool(t.skills.get("body") and skill in (t.skills.get("picked") or [])),
            "skill_read": None if t.skills is None else skill in (t.skills.get("read") or []),
            "skill_picked": None if t.skills is None else t.skills.get("picked"),
            "status": t.status,
            "reply": t.text[:1500], "ms": t.ms}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    ap.add_argument("--candidate", help="a SKILL.md to try instead of the deployed one")
    ap.add_argument("--task")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    candidate = Path(a.candidate).read_text() if a.candidate else None
    tasks = [t for t in load_tasks(a.skill) if not a.task or t["id"] == a.task]
    results = [run_task(t, a.skill, candidate) for t in tasks]
    if a.json:
        print(json.dumps(results, indent=1))
    for r in results:
        if a.verbose or r["failed"]:
            print(f"── {r['id']} {r['score']:.2f} ({r['ms'] / 1000:.1f}s) tools={r['tools']}")
            for f in r["failed"]:
                print(f"   ✗ {f}")
            if a.verbose:
                print("   " + r["reply"][:300].replace("\n", "\n   "))
    mean = sum(r["score"] for r in results) / max(len(results), 1)
    print(f"\n{a.skill}: mean score {mean:.3f} over {len(results)} tasks "
          f"({sum(r['score'] == 1.0 for r in results)} fully passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
