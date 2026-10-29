#!/usr/bin/env python3
"""What's set up and working right now, checked live, for the `capabilities` tool.

The agent used to answer "what could I give you?" and "is X set up?" from a line in its prompt
("…sync exists but each source is off until the user turns it on") that stopped being true. 10-01
it told Zach to turn on calendar sync and connect Home Assistant an hour after turning on his TV
through Home Assistant. Every fact here is read when asked:

  self            which titan is answering: its code version and deploy time, and which models
                  answer (10-01: it said it "runs locally… without external content guardrails")
  sync            each Jellybean source the connectors ledger has heard from, and when
  github          the admin's GitHub stars from the last year (the github_stars tool reads them), synced daily
  home            Home Assistant: connected, and what it has (lights, thermostat, TVs and speakers…)
  devices         the asking person's own paired devices and when titan last heard from them, and
                  their Mac's phone-to-Mac channel (live, wakeable); never anyone else's (each
                  person's data stays theirs: plan item 9)
  household       who's in it (names and roles, ages when Settings › Household has them): the
                  household store, never the KB (10-01: KB notes added two children and made a
                  22-year-old one)
  abilities       titan's tools by the machine they run on (titan, the Mac, the iPhone), from the
                  registry itself (10-01: it said Find My works from the iPhone)
  routes          whether the routes titan builds through are running: Claude's todo list, its
                  sync to GitHub issues, and titan's nightly coder (10-01: it called itself a requester)
  services        titan's models and runtimes, up or down
  new             what shipped to titan in the last few days (its own git log)

Each part fails on its own ("unknown: <why>"), never the whole answer.

  python3 scripts/capabilities.py          (prints the snapshot)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops_ports import CODE_MODEL_URL  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SYNC_SOURCES = ("calendar", "mail", "downloads", "browsing")   # what Jellybean's settings can turn on
STALE_S = 24 * 3600
NEW_DAYS = 3
_HA_KINDS = {"light": "lights", "switch": "switches", "fan": "fans", "climate": "thermostat", "media_player": "TVs and speakers",
             "lock": "locks (read only)", "cover": "doors and covers", "camera": "cameras (read only)",
             "alarm_control_panel": "alarm (read only)", "scene": "scenes"}
_cache: dict[str, tuple[float, str]] = {}


def _ago(seconds: float) -> str:
    import agent_runtime_lib as lib
    return lib._ago(seconds)


def _cached(key: str, ttl: float, make: Callable[[], str]) -> str:
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = make()
    _cache[key] = (time.monotonic(), val)
    return val


def _iso_age(stamp: Any, now: float) -> float | None:
    from datetime import datetime
    try:
        return now - datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=5).stdout.strip()


def version_line(git: Callable[..., str] | None = None, now: float | None = None) -> str:
    """titan's code version (#1688): the deployed commit, when it was deployed, and whether it's the latest on main as
    the hourly main-health check last fetched it (no network here). One fact for the whole host."""
    now = now or time.time()
    try:
        g = git or _git
        sha, _, subject = g("log", "-1", "--format=%h%x09%s").partition("\t")
        moved = g("log", "-g", "-1", "--format=%ct")   # the checkout's last move: the deploy
        when = f", deployed {_ago(now - float(moved))} ago" if moved.isdigit() else ""
        ver = f"titan-node {sha}{when} ({subject[:80]})" if sha else "titan-node version unknown"
    except Exception as exc:  # noqa: BLE001
        ver = f"titan-node version unknown ({type(exc).__name__})"
    try:   # #1688: is this the latest? main as the hourly main-health check fetched it (no network here)
        behind = (git or _git)("rev-list", "--count", "HEAD..refs/main-health/main")
        if behind.isdigit() and int(behind) > 0:
            ver += f"; {behind} merged commit{'s' if int(behind) != 1 else ''} on main not deployed yet (as of the last hourly check)"
        elif behind == "0":
            ver += "; the latest on main as of the last hourly check"
    except Exception:  # noqa: BLE001  (no main-health ref yet, or no git: say nothing)
        pass
    return ver


def self_part(git: Callable[..., str] | None = None, now: float | None = None) -> str:
    """Which titan is answering: the code version and when it was deployed, and which models answer."""
    ver = version_line(git, now)
    chat = os.environ.get("AGENT_GEMINI_MODEL", "gemini-3.5-flash-lite")
    return (f"Titan: {ver}. Every chat turn runs on Google Gemini ({chat}), so Google's content rules apply; "
            "the local Qwen3-8B takes over when Gemini fails and runs background work (memory, mail bills, the letter).")


def _systemd_times(unit: str) -> tuple[float | None, float | None, str]:
    """(last trigger, next trigger) of a timer as epoch seconds, and its service's last result."""
    from datetime import datetime
    out = subprocess.run(["systemctl", "show", unit, "-p", "LastTriggerUSec", "-p", "NextElapseUSecRealtime"],
                         capture_output=True, text=True, timeout=5).stdout
    vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def ts(v: str | None) -> float | None:
        try:
            return datetime.strptime(str(v).strip(), "%a %Y-%m-%d %H:%M:%S %Z").timestamp() if v and v.strip() not in ("", "n/a") else None
        except ValueError:
            return None
    res = subprocess.run(["systemctl", "show", unit.replace(".timer", ".service"), "-p", "Result"],
                         capture_output=True, text=True, timeout=5).stdout.strip().partition("=")[2]
    return ts(vals.get("LastTriggerUSec")), ts(vals.get("NextElapseUSecRealtime")), res


def routes_part(timers: Callable[[str], tuple] | None = None, open_claude: Callable[[], int] | None = None,
                now: float | None = None) -> str:
    """Whether the routes titan builds through are running right now."""
    now = now or time.time()
    try:
        t = timers or _systemd_times
        if open_claude is None:
            import todos
            open_claude = lambda: sum(1 for x in todos.list_for(["claude"]) if not x.get("done_at"))   # noqa: E731
        n = open_claude()

        def when(unit: str) -> str:
            last, nxt, res = t(unit)
            bits = [f"last {_ago(now - last)} ago" + ("" if res in ("", "success") else f" ({res})") if last else "never run"]
            if nxt:
                bits.append(f"next in {_ago(nxt - now)}")
            return ", ".join(bits)
        return (f"Build routes: a code change goes on Claude's todo list ({n} open), which syncs to GitHub issues "
                f"every 10 min ({when('titan-todo-issues.timer')}); titan's coder drafts titan-node pull requests "
                f"nightly ({when('titan-autonomous-coder.timer')}); the user merges. Mac scripts become saved skills "
                "on the Mac. Cards, routines, scheduled actions (one thing at a later time, with a card to "
                "cancel it) and sharing need no setup.")
    except Exception as exc:  # noqa: BLE001
        return f"Build routes: unknown ({type(exc).__name__})"


def sync_part(status: dict | None = None, now: float | None = None) -> str:
    """Household sync sources: what Jellybean has sent titan, and how recently."""
    now = now or time.time()
    try:
        if status is None:
            import connectors_runner as cx
            status = cx.status()
    except Exception as exc:  # noqa: BLE001
        return f"Sync from Jellybean: unknown ({type(exc).__name__})"
    lines = []
    for src in SYNC_SOURCES:   # titan's own sources (daily log, test fixtures) aren't Jellybean settings
        s = status.get(src)
        if not s or not s.get("items"):
            lines.append(f"{src}: not syncing (nothing received)")
            continue
        age = _iso_age(s.get("last_sync"), now)
        when = f"last {_ago(age)} ago" if age is not None else "last time unknown"
        flag = "" if age is not None and age < STALE_S else ", STALE"
        lines.append(f"{src}: on, {s['items']} items, {when}{flag}")
    return "Sync from Jellybean: " + "; ".join(lines)


def github_part() -> str:
    """The admin's GitHub stars (github_stars.py, #707): synced or not, how many, how long ago."""
    try:
        import github_stars
        return github_stars.status_line()
    except Exception as exc:  # noqa: BLE001
        return f"GitHub stars: unknown ({type(exc).__name__})"


def home_part(states: Callable[[], list] | None = None) -> str:
    """Home Assistant: connected or not, and what it has."""
    def make() -> str:
        try:
            import home_assistant as ha
            if states is None and not ha._token():
                return "Home Assistant: not connected (no access token on titan)"
            got = states() if states else ha._http("GET", "/api/states")
        except Exception as exc:  # noqa: BLE001
            return f"Home Assistant: token present but not answering ({type(exc).__name__})"
        kinds = Counter(str(s.get("entity_id", "")).split(".")[0] for s in got or [])
        have = [f"{kinds[d]} {label}" for d, label in _HA_KINDS.items() if kinds.get(d)]
        return "Home Assistant: connected" + (f" ({', '.join(have)})" if have else " (no devices)")
    return make() if states else _cached("home", 60, make)


def devices_part(person_id: str, people: list[dict] | None = None, macs: Callable[[str], list] | None = None,
                 live: Callable[[str, str], bool] | None = None, now: float | None = None) -> str:
    """The asking person's own paired devices, last heard from, and their Mac's phone-to-Mac channel."""
    now = now or time.time()
    try:
        if people is None or macs is None:
            import household_identity as hh
            store = hh.get_store()
            people = store.people() if people is None else people
            macs = macs or store.macs_for
        if live is None:
            import cross_device as xd
            live = lambda pid, key: xd.CHANNEL.live((pid, key))   # noqa: E731
    except Exception as exc:  # noqa: BLE001
        return f"Devices: unknown ({type(exc).__name__})"
    out = []
    for p in (p for p in people if p.get("id") == person_id):
        bits = []
        try:   # #1631: their paired machines (an executor's presence key is its device id)
            ex = macs(p["id"], kind="executor") or []
        except TypeError:   # a test's macs() without kinds
            ex = []
        ex_ids = {m["device_key"] for m in ex}
        for d in p.get("devices") or []:
            if d.get("revoked") or d.get("id") in ex_ids:
                continue   # a paired machine named itself: its name is its text, so it's counted below, not listed
            age = _iso_age(d.get("last_seen"), now)
            app = f"Jellybean {d['app_version']}, " if d.get("app_version") else ""
            bits.append(f"{d.get('name') or d.get('platform')} ({app}last seen {_ago(age)} ago)" if age is not None
                        else f"{d.get('name') or d.get('platform')} ({app}never seen)")
        for m in macs(p["id"]) or []:
            reach = ("channel live" if live(p["id"], m["device_key"]) else
                     "can be woken" if m.get("mac_address") or (m.get("lan_ip") and m.get("wake_port")) else
                     "can't be woken")
            age = _iso_age(m.get("updated_at"), now)
            app = f" (Jellybean {m['app_version']})" if m.get("app_version") else ""
            missing = _mac_tools_missing(m.get("device_tools"))
            if missing:
                reach += f"; needs a newer Jellybean for {', '.join(missing[:8])}" + ("…" if len(missing) > 8 else "")
            bits.append(f"Mac {m.get('name') or ''}{app} for phone requests: {reach}"
                        + (f", checked in {_ago(age)} ago" if age is not None else ""))
        if ex:   # counted, never named (machine_status reads them, wrapped)
            on = sum(1 for m in ex if live(p["id"], m["device_key"]))
            bits.append(f"{len(ex)} paired machine{'s' if len(ex) != 1 else ''} ({on} connected; machine_status reads them)")
        if bits:
            out.append(", ".join(bits))
    return "Your devices: " + ("; ".join(out) if out else "none paired")


def eval_macs(state: str, now: float | None = None) -> tuple[list[dict], bool]:
    """An eval turn's simulated Mac (eval_mac: live | wakeable | unreachable | never): its presence
    rows and whether its channel is live. The agent API's "Your Mac" line uses the same."""
    if state == "never":
        return [], False
    wake = {"mac_address": "02:00:00:00:00:01"} if state in ("live", "wakeable") else {}
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((now or time.time()) - 7200))
    return [{"device_key": "eval-mac", "name": "Eval Mac", "awake": True, **wake, "updated_at": at}], state == "live"


def eval_devices_part(person_id: str, state: str, now: float | None = None) -> str:
    """devices_part for an eval turn: the asking phone and the simulated Mac, never real devices
    (10-01: "your MacBook checked in 5 seconds ago" on a turn simulating a phone with no Mac)."""
    now = now or time.time()
    macs, live = eval_macs(state, now)
    me = [{"id": person_id, "devices": [{"name": "this iPhone", "platform": "ios",
                                         "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}]}]
    return devices_part(person_id, me, lambda pid: macs, lambda pid, key: live, now=now)


def _mac_tools_missing(reported: Any) -> list[str]:
    """Mac tools titan has that this Mac's Jellybean doesn't run (none when it sent no list)."""
    try:
        import agent_runtime_lib as lib
        runs = lib.mac_runs(reported)
        if runs is None:
            return []
        return sorted(n for n, spec in lib._DEVICE_TOOLS.items()
                      if n not in runs and n not in lib.PHONE_ONLY_TOOLS and not spec.get("internal"))
    except Exception:  # noqa: BLE001
        return []


def household_part(people: list[dict] | None = None) -> str:
    """Who's in the household, from the household store (names, roles, ages when set)."""
    try:
        if people is None:
            import household_identity as hh
            people = hh.get_store().people()
    except Exception as exc:  # noqa: BLE001
        return f"Household: unknown ({type(exc).__name__})"
    def one(p: dict) -> str:
        role = {"admin": "adult, admin", "household": "shared household device"}.get(p.get("role"), p.get("role"))
        age = f", {p['age']}" if isinstance(p.get("age"), int) else ""
        return f"{p.get('name')} ({role}{age})"
    return "Household: " + (", ".join(one(p) for p in people) if people else "nobody yet")


def abilities_part(titan: list[str] | None = None, mac: list[str] | None = None, phone: list[str] | None = None,
                   risk: dict[str, str] | None = None, executor: list[dict] | None = None) -> str:
    """titan's tools by where they run, straight from the registry; on the devices, split into reads
    (no approval) and changes (the device asks first unless the user trusts that tool). `executor`: titan-exec's own
    capability list (#1631), only while it's installed: its reads, and what changes titan, held for the admin's OK."""
    try:
        if executor is None and titan is None:
            import agent_runtime_lib as lib
            import titan_exec
            executor = titan_exec.capabilities() if lib._titan_exec_on() else []
        if titan is None or mac is None or phone is None:
            import agent_runtime_lib as lib
            import file_tool   # #1251 C: the old file tools are `files` modes, never named to the model
            titan = sorted(n for n in lib._TOOL_REGISTRY if n not in file_tool.ALIASES)
            phone = sorted(lib.PHONE_DEVICE_TOOLS)
            mac = sorted(n for n in lib._DEVICE_TOOLS if n not in lib.PHONE_ONLY_TOOLS
                         and not lib._DEVICE_TOOLS[n].get("internal") and n not in file_tool.ALIASES)
            risk = {n: str(spec.get("risk") or "") for n, spec in lib._DEVICE_TOOLS.items()}
    except Exception as exc:  # noqa: BLE001
        return f"Abilities: unknown ({type(exc).__name__})"

    def split(names: list[str]) -> str:
        if not risk:
            return ", ".join(names)
        reads = [n for n in names if risk.get(n) == "read"]
        changes = [n for n in names if risk.get(n) != "read"]
        return (f"reads, no approval: {', '.join(reads) or 'none'}; "
                f"changes, the device asks first unless trusted: {', '.join(changes) or 'none'}")
    out = ("Abilities by machine (internal tool names: never show these to the user; say what they do):\n"
           f"- On titan, from any device: {', '.join(titan)}\n"
           f"- On the user's Mac (from the iPhone only while the Mac is reachable): {split(mac)}\n"
           f"- On the user's iPhone itself: {split(phone)}")
    if executor:
        reads = sorted({c["tool"] for c in executor if not c.get("changes")})
        changes = sorted({c["tool"] for c in executor if c.get("changes")})
        out += (f"\n- On titan itself, through its executor (the admin only): reads: {', '.join(reads) or 'none'}; "
                f"changes titan, held for the admin's OK every time: {', '.join(changes) or 'none'}")
    return out


# What a person can ask titan for, in plain words (titan-node#355, decided 10-02: a plain capability summary for everyone;
# titan's system details stay the admin's). Keys are household_identity.capabilities' own; nothing here names a
# service, model, machine or tool.
_PLAIN = [("calendar", "check and add to the calendar"), ("reminders", "set and check reminders"),
          ("weather", "tell you the weather"), ("web", "look things up on the web"),
          ("knowledge_base", "find what the household has saved"), ("knowledge_base_edit", "save notes for you"),
          ("knowledge_base_share", "share your notes with someone in the household"),
          ("routines", "run the household's routines"), ("routines_edit", "set up routines"),
          ("scheduled", "do something later, at a time or when you get somewhere"),
          ("personal_memory", "remember things you tell me"), ("house_control", "turn lights and devices on or off"),
          ("contacts", "look up contacts"), ("messages", "send messages for you (you approve each one)"),
          ("mail", "draft emails"), ("notes", "search your notes"), ("files", "find and organize your files"),
          ("records", "find bills and records"), ("cards", "send you reminders and heads-ups as cards")]
_NEVER = "I can't unlock doors, open the garage, turn off the alarm or show cameras from a chat."


def plain_summary(role: str | None) -> str:
    """What titan can do for this person, in plain words, from their role's permissions (the same map /v1/me gives the
    apps). No system details: those are the admin's."""
    try:
        import household_identity as hh
        can = hh.capabilities(str(role or ""))
    except Exception:  # noqa: BLE001
        return ""
    yes = [words for key, words in _PLAIN if can.get(key)]
    if not yes:
        return ""
    who = {"kid": "you", "household": "everyone at this screen"}.get(str(role or ""), "you")
    return (f"What titan can do for {who} (a plain summary; titan's own system details are the admin's): "
            + "; ".join(yes) + ". " + _NEVER)


def service_urls() -> dict[str, str]:
    """titan's models and runtimes: name → base URL (shared with device_health)."""
    import agent_runtime_lib as lib
    urls = {"local brain (Qwen3-8B)": os.environ.get("AGENT_ENDPOINT", "http://127.0.0.1:8010"),
            "code model (Qwen3.6)": os.environ.get("CODE_MODEL_URL", CODE_MODEL_URL),
            "Decider": os.environ.get("DECIDER_URL", "http://127.0.0.1:8031")}
    urls.update(dict(sorted(lib._RUNTIME_BASES.items())))
    return urls


def services_part(checks: dict[str, Callable[[], bool]] | None = None) -> str:
    """titan's models and runtimes: up or down."""
    if checks is None:
        import agent_runtime_lib as lib
        checks = {name: (lambda u=url: lib.health_ok(u)) for name, url in service_urls().items()}
    up, down = [], []
    for name, ok in checks.items():
        try:
            (up if ok() else down).append(name)
        except Exception:  # noqa: BLE001
            down.append(name)
    return f"Services: {len(up)} up" + (f"; DOWN: {', '.join(down)}" if down else "")


def new_part(log: Callable[[], str] | None = None, days: int = NEW_DAYS) -> str:
    """What shipped to titan lately: its own git log (merged PR titles)."""
    try:
        text = log() if log else subprocess.run(
            ["git", "-C", str(REPO), "log", f"--since={days}.days", "--no-merges", "--pretty=%s"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:  # noqa: BLE001
        return f"New on titan: unknown ({type(exc).__name__})"
    titles = [t.strip() for t in text.splitlines() if t.strip()]
    if not titles:
        return f"New on titan in the last {days} days: nothing"
    shown = titles[:15]
    more = f" (and {len(titles) - len(shown)} more)" if len(titles) > len(shown) else ""
    return f"New on titan in the last {days} days, newest first{more}:\n" + "\n".join(f"- {t}" for t in shown)


def snapshot(person_id: str | None = None, eval_mac: str | None = None) -> str:
    """Everything, one part per line; a part that can't be read says so. Devices are the asker's own
    (on an eval turn, its simulated ones)."""
    if person_id is None:
        import household_identity as hh
        person_id = hh.ADMIN_PERSON_ID
    devices = eval_devices_part(person_id, eval_mac) if eval_mac else devices_part(person_id)
    return "\n".join([self_part(), sync_part(), github_part(), home_part(), devices, household_part(), abilities_part(), routes_part(),
                      services_part(), new_part(),
                      "(The Mac's privacy grants aren't visible from titan; a run_check or the Mac's settings say "
                      "those. An app's version shows here once the app reports it.)"])


if __name__ == "__main__":
    print(snapshot())
