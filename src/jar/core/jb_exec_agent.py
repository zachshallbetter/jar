#!/usr/bin/env python3
"""titan's executor agent for another machine in the house (titan-node#1631 slice 4c): runs on that machine, not on
titan. Ships dark: installing it on a machine is Zach's step, after the machine is paired as an executor (its owner's
Add-a-device, `kind: "executor"`, platform `other`).

What it does: tells titan it's here (POST /v1/device/presence every 60 s) and long-polls its own inbox (GET
/v1/device/inbox). Its token reaches only those and the result route; titan keys its inbox to its own device id.
What it answers: `machine_status`, with uptime, load, memory, free disk and the busiest processes by bare name; and
`app_check {app}` (#1638), whether one app is running (its processes, CPU, memory, how long), its version and how many
crash reports it left this week (macOS DiagnosticReports, or Linux's dpkg status and /var/crash; counted, never read); and `repo_status {repo}` (#1636), one of the git
repositories its owner listed in CONFIG ("repos": {name: absolute path}): its branch, how far it is ahead of or behind
its upstream, how many files are changed, and its last commits. Read-only; titan never sends a path, only a listed
name. Anything else is refused ("not an operation"). It never runs a shell or a command it was sent, and nothing it answers
carries a process's arguments, the environment or a path (paths carry usernames).

  python3 jb_exec_agent.py pair CONFIG TITAN_URL CODE [NAME]
                                            claim a 6-digit code from the owner's "pair a machine" (POST
                                            /v1/me/executor-pairing) and write CONFIG, 0600, never overwriting one
  python3 jb_exec_agent.py CONFIG           run (CONFIG: a 0600 JSON file {"titan": URL, "token": "x.…", "name": "…"})
  python3 jb_exec_agent.py CONFIG --once    one status, printed, nothing sent (to see what titan would get)

Stdlib only. Polls at most once a second; a failure backs off to a minute. Installing it as a user service (systemd
or launchd), adding repositories and removing it: docs/executor-agent.md.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

PRESENCE_EVERY_S = 60.0
POLL_WAIT_S = 25
MIN_POLL_GAP_S = 1.0
BACKOFF_MAX_S = 60.0
TOP_PROCESSES = 5
NAME_RX = re.compile(r"[^A-Za-z0-9 ._+-]")
OPERATIONS = ("machine_status", "app_check", "repo_status")
GIT = "/usr/bin/git"
REPO_LOG = 5
REPO_CHANGES = 10
_PATH_RX = re.compile(r"[^A-Za-z0-9 ._/+-]")
REPOS_MAX = 20
APP_MATCH_MAX = 20
CRASH_WINDOW_S = 7 * 86400


class ConfigError(Exception):
    pass


def load_config(path: Path) -> dict[str, str]:
    """{titan, token, name}; refused when the file is readable or writable by anyone but its owner."""
    try:
        st = path.stat()
    except OSError as exc:
        raise ConfigError(f"can't read {path.name}") from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"{path.name} must be readable by its owner only (chmod 600)")
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path.name} isn't JSON") from exc
    titan, token, name = (str(cfg.get(k) or "").strip() for k in ("titan", "token", "name"))
    if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", titan):
        raise ConfigError("titan is the server's address, like http://titan:7770")
    if not token.startswith("x."):
        raise ConfigError("token is the executor token titan gave at pairing (it starts with x.)")
    out = {"titan": titan.rstrip("/"), "token": token, "name": clean_name(name) or socket.gethostname()[:40]}
    if "repos" in cfg:
        out["repos"] = repos_of(cfg["repos"])
    return out


def repos_of(raw: Any) -> dict[str, str]:
    """The owner's listed repositories, {name: absolute path}: names cleaned, paths absolute; anything else refused."""
    if not isinstance(raw, dict) or len(raw) > REPOS_MAX:
        raise ConfigError(f"repos is an object of at most {REPOS_MAX} name: absolute path")
    out = {}
    for k, v in raw.items():
        name, path = clean_name(str(k)), str(v or "")
        if not name or not path.startswith("/") or "\n" in path:
            raise ConfigError("each of repos is a name and an absolute path")
        out[name] = path
    return out


def clean_name(text: str, limit: int = 40) -> str:
    """A bare, safe name: a path's last part, odd characters dropped (a process name can be anyone's text)."""
    base = str(text or "").strip().rsplit("/", 1)[-1]
    return NAME_RX.sub("", base)[:limit].strip()


# ── what it knows about this machine ─────────────────────────────────────────────────────────────────────────────

Run = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL,
                              env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def uptime_s(proc: Path = Path("/proc/uptime"), run: Run = _run, now: float | None = None) -> float | None:
    try:
        return float(proc.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    m = re.search(r"sec = (\d+)", run(["/usr/sbin/sysctl", "-n", "kern.boottime"]))   # macOS
    return (now or time.time()) - int(m.group(1)) if m else None


def memory(meminfo: Path = Path("/proc/meminfo")) -> tuple[int, int] | None:
    """(available MB, total MB) on Linux; None elsewhere."""
    try:
        vals = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in meminfo.read_text().splitlines() if ":" in line)}
        return vals["MemAvailable"] // 1024, vals["MemTotal"] // 1024
    except (OSError, ValueError, KeyError):
        return None


def busiest(run: Run = _run, n: int = TOP_PROCESSES) -> list[tuple[str, float]]:
    """The n busiest processes by CPU: (bare name, %); never an argument or a path."""
    out = []
    for line in run(["/bin/ps", "-Ao", "%cpu=,comm="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            cpu = float(parts[0])
        except ValueError:
            continue
        name = clean_name(parts[1])
        if name:
            out.append((name, cpu))
    return sorted(out, key=lambda x: -x[1])[:n]


def status(run: Run = _run, mounts: tuple[str, ...] = ("/",), proc: Path = Path("/proc/uptime"),
           meminfo: Path = Path("/proc/meminfo")) -> str:
    """machine_status's answer, bounded: a few lines, names only."""
    lines = [f"host {clean_name(socket.gethostname())}"]
    up = uptime_s(proc, run)
    if up is not None:
        lines.append(f"up {int(up // 86400)} d {int(up % 86400 // 3600)} h")
    try:
        lines.append("load " + " ".join(f"{x:.2f}" for x in os.getloadavg()))
    except OSError:
        pass
    mem = memory(meminfo)
    if mem:
        lines.append(f"memory {mem[0]} MB free of {mem[1]} MB")
    for m in mounts:
        try:
            u = shutil.disk_usage(m)
            lines.append(f"disk {m}: {u.free // (1 << 30)} GB free of {u.total // (1 << 30)} GB")
        except OSError:
            continue
    top = busiest(run)
    if top:
        lines.append("busiest: " + ", ".join(f"{n} {c:.0f}%" for n, c in top))
    return "\n".join(lines)


def _etime_s(text: str) -> int | None:
    """ps's elapsed time, [[dd-]hh:]mm:ss → seconds."""
    m = re.fullmatch(r"(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)", text.strip())
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def _span(seconds: int) -> str:
    if seconds >= 86400:
        return f"{seconds // 86400} d {seconds % 86400 // 3600} h"
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 60} min"


def app_version(app: str, apps: Path = Path("/Applications")) -> str | None:
    """A Mac app's version from its own Info.plist, or None. The name is already a bare name: no path can be built."""
    import plistlib
    if not app or app.startswith("."):
        return None
    try:
        with open(apps / f"{app}.app" / "Contents" / "Info.plist", "rb") as f:
            v = plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return clean_name(str(v), 24) or None if v else None


def crash_reports(app: str, reports: Path = Path.home() / "Library/Logs/DiagnosticReports", now: float | None = None) -> int | None:
    """How many crash reports the app left in the last week (macOS): file names only, never their contents. None: no
    such folder here."""
    if not reports.is_dir():
        return None
    cutoff, want, n = (now or time.time()) - CRASH_WINDOW_S, app.lower(), 0
    try:
        for f in reports.iterdir():
            name = f.name.lower()
            if name.startswith(want + "-") or name.startswith(want + "_") or name.startswith(want + "."):
                try:
                    n += f.stat().st_mtime >= cutoff
                except OSError:
                    continue
    except OSError:
        return None
    return n


def package_version(app: str, status: Path = Path("/var/lib/dpkg/status")) -> str | None:
    """A Linux machine's installed package version (Debian/Ubuntu: dpkg's own status file, read here, never a command
    with the name on it), or None. The package is the app's name in lower case, spaces as hyphens."""
    want = app.strip().lower().replace(" ", "-")
    if not want or want.startswith("."):
        return None
    try:
        text = status.read_text(errors="replace")
    except OSError:
        return None
    for block in text.split("\n\n"):
        fields = dict(ln.split(": ", 1) for ln in block.splitlines() if ": " in ln and not ln.startswith(" "))
        if fields.get("Package", "").lower() == want and "installed" in fields.get("Status", "").split():
            return clean_name(fields.get("Version", ""), 40) or None
    return None


def linux_crashes(app: str, crash: Path = Path("/var/crash"), now: float | None = None) -> int | None:
    """How many crash reports the app left in the last week on Linux (apport's /var/crash/_usr_bin_<name>.<uid>.crash):
    file names only, never their contents. None: no such folder here."""
    if not crash.is_dir():
        return None
    cutoff, want, n = (now or time.time()) - CRASH_WINDOW_S, app.strip().lower(), 0
    try:
        for f in crash.iterdir():
            name = f.name.lower()
            if name.endswith(".crash") and name.split(".", 1)[0].rsplit("_", 1)[-1] == want:
                try:
                    n += f.stat().st_mtime >= cutoff
                except OSError:
                    continue
    except OSError:
        return None
    return n


def app_check(app: Any, run: Run = _run, apps: Path = Path("/Applications"),
              reports: Path = Path.home() / "Library/Logs/DiagnosticReports", now: float | None = None,
              dpkg: Path = Path("/var/lib/dpkg/status"), crash: Path = Path("/var/crash")) -> str:
    """app_check's answer: the app's own processes (by bare name, exactly, any case; else names that contain it), their
    CPU, memory and longest run, its version, and its crash reports this week. The name is matched here, in Python: it
    never reaches a command line."""
    want = clean_name(str(app or ""))
    if not want:
        return "Which app? Send its name."
    rows = []
    for line in run(["/bin/ps", "-Ao", "%cpu=,rss=,etime=,comm="]).splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            cpu, rss = float(parts[0]), int(parts[1])
        except ValueError:
            continue
        name = clean_name(parts[3])
        if name:
            rows.append((name, cpu, rss, _etime_s(parts[2])))
    exact = [r for r in rows if r[0].lower() == want.lower()]
    hits = (exact or [r for r in rows if want.lower() in r[0].lower()])[:APP_MATCH_MAX]
    if hits:
        up = max((r[3] for r in hits if r[3] is not None), default=None)
        names = sorted({r[0] for r in hits})
        lines = [f"{want}: running, {len(hits)} process{'es' if len(hits) != 1 else ''}"
                 + ("" if exact else f" ({', '.join(names[:4])})"),
                 f"CPU {sum(r[1] for r in hits):.0f}%, memory {sum(r[2] for r in hits) // 1024} MB"
                 + (f", up {_span(up)}" if up is not None else "")]
    else:
        lines = [f"{want}: not running"]
    v = app_version(want, apps) or package_version(want, dpkg)   # a Mac app's Info.plist, else a Linux package
    if v:
        lines.append(f"version {v}")
    c = crash_reports(want, reports, now)
    if c is None:
        c = linux_crashes(want, crash, now)
    if c is not None:
        lines.append(f"{c} crash report{'s' if c != 1 else ''} in the last 7 days")
    return "\n".join(lines)


def _git(path: str, *args: str) -> list[str]:
    """A read-only git command for one listed repository. Its own config can name programs git would run
    (core.fsmonitor), so those are switched off here; no optional locks, so reading never writes."""
    return [GIT, "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-C", path, *args]


def repo_status(repo: Any, repos: dict[str, str] | None, run: Run = _run) -> str:
    """repo_status's answer: a listed repository's branch, ahead/behind, changed files and last commits. No name, or
    one not listed: the listed names (never their paths)."""
    repos = repos or {}
    if not repos:
        return "No repositories are listed on this machine (its owner adds them to the agent's config)."
    want = clean_name(str(repo or ""))
    hit = next((k for k in repos if k.lower() == want.lower()), None) if want else None
    if hit is None:
        return ("Not one listed here. " if want else "") + "Listed: " + ", ".join(sorted(repos))
    path = repos[hit]
    head = run(_git(path, "status", "--porcelain=v1", "--branch", "--untracked-files=normal"))
    if not head:
        return f"{hit}: couldn't read it (not a git repository here, or git isn't installed)."
    lines = head.splitlines()
    first, changed = lines[0], [ln for ln in lines[1:] if ln.strip()]
    m = re.match(r"## (?:No commits yet on )?(\S+?)(?:\.\.\.(\S+))?(?: \[([^\]]*)\])?$", first)
    branch = clean_name(m.group(1), 60) if m else "?"
    out = [f"{hit}: on {branch}" + (f", tracking {clean_name(m.group(2), 60)}" if m and m.group(2) else ", no upstream")]
    if m and m.group(3):
        out[0] += f" ({m.group(3)[:40]})"
    untracked = sum(ln.startswith("??") for ln in changed)
    out.append(f"{len(changed) - untracked} changed file{'s' if len(changed) - untracked != 1 else ''}, {untracked} untracked"
               if changed else "nothing changed")
    if len(changed) > untracked:   # #1636: which files and by how much; never their contents
        stat = run(_git(path, "diff", "HEAD", "--numstat", "--no-ext-diff", "--no-textconv", "--no-color"))
        rows = []
        for ln in stat.splitlines():
            parts = ln.split("\t", 2)
            if len(parts) != 3:
                continue
            added, removed, name = parts
            name = _PATH_RX.sub("", name)[-80:].lstrip("/") or "?"
            rows.append(f"  {name} " + ("(binary)" if added == "-" else f"(+{added} -{removed})"))
        if rows:
            out.append("changes:")
            out += rows[:REPO_CHANGES] + ([f"  and {len(rows) - REPO_CHANGES} more"] if len(rows) > REPO_CHANGES else [])
    log = run(_git(path, "log", f"-{REPO_LOG}", "--format=%h %cs %s", "--no-decorate"))
    if log.strip():
        out.append("last commits:")
        out += ["  " + ln[:120] for ln in log.splitlines()[:REPO_LOG]]
    return "\n".join(out)


def answer(item: dict[str, Any], run: Run = _run, repos: dict[str, str] | None = None) -> dict[str, Any]:
    """One inbox item → the result body. Only OPERATIONS; anything else is refused, never run."""
    tool = item.get("tool")
    if tool not in OPERATIONS:
        return {"status": "refused", "result": "Not an operation this machine has."}
    args = item.get("args") if isinstance(item.get("args"), dict) else {}
    if tool == "app_check":
        return {"status": "done", "result": app_check(args.get("app"), run)}
    if tool == "repo_status":
        return {"status": "done", "result": repo_status(args.get("repo"), repos, run)}
    return {"status": "done", "result": status(run)}


# ── talking to titan ─────────────────────────────────────────────────────────────────────────────────────────────

def _request(cfg: dict[str, str], method: str, path: str, body: dict | None = None, timeout: float = 35) -> Any:
    req = urllib.request.Request(cfg["titan"] + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Agent-Token": cfg["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def backoff(failures: int) -> float:
    return min(BACKOFF_MAX_S, MIN_POLL_GAP_S * (2 ** max(0, failures)))


def run_forever(cfg: dict[str, str], request: Callable = _request, sleep: Callable[[float], None] = time.sleep,
                clock: Callable[[], float] = time.monotonic, rounds: int | None = None) -> None:
    """Heartbeat and long-poll until stopped (rounds: a test's limit)."""
    last_presence, failures, n = -1e9, 0, 0
    unsent: list[tuple[str, dict[str, Any]]] = []   # answers titan is waiting for that didn't go out yet, oldest first
    while rounds is None or n < rounds:
        n += 1
        started = clock()
        try:
            # titan marked these delivered and won't send them again: their answers go first, before a new poll
            while unsent:
                rid, body = unsent[0]
                request(cfg, "POST", f"/v1/device/inbox/{rid}/result", body)
                unsent.pop(0)
            if clock() - last_presence >= PRESENCE_EVERY_S:
                request(cfg, "POST", "/v1/device/presence", {"name": cfg["name"], "awake": True, "device_key": "self"})
                last_presence = clock()
            got = request(cfg, "GET", f"/v1/device/inbox?wait={POLL_WAIT_S}")
            for item in (got or {}).get("requests") or []:
                rid = str(item.get("id") or "")
                if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid):
                    unsent.append((rid, answer(item, repos=cfg.get("repos"))))
            while unsent:
                rid, body = unsent[0]
                request(cfg, "POST", f"/v1/device/inbox/{rid}/result", body)
                unsent.pop(0)
            failures = 0
        except (urllib.error.URLError, OSError, ValueError):
            failures += 1
            sleep(backoff(failures))
            continue
        gap = MIN_POLL_GAP_S - (clock() - started)
        if gap > 0:
            sleep(gap)   # never more than one poll a second, even when titan answers at once


def pair(config: Path, titan: str, code: str, name: str = "", post: Callable | None = None) -> str:
    """Claim a pairing code as this machine (platform other) and write its config, 0600. Refuses to overwrite a config
    (re-pairing is a deliberate step: delete the old one first). → the config's path, or raises ConfigError."""
    if config.exists():
        raise ConfigError(f"{config.name} already exists; remove it first to pair again")
    if not re.fullmatch(r"\d{6}", code.replace(" ", "")):
        raise ConfigError("the code is the 6 digits titan showed")
    if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", titan):
        raise ConfigError("titan is the server's address, like http://titan:7770")
    name = clean_name(name) or clean_name(socket.gethostname()) or "machine"

    def _post(url: str, body: dict) -> dict:
        req = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    try:
        got = (post or _post)(titan.rstrip("/") + "/v1/pair/claim",
                              {"code": code.replace(" ", ""), "device_name": name, "platform": "other"})
    except urllib.error.HTTPError as exc:
        raise ConfigError(f"titan refused the code ({exc.code}): it may have expired, or isn't a machine code") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ConfigError("couldn't reach titan") from exc
    token = str((got or {}).get("token") or "")
    if not token.startswith("x."):
        raise ConfigError("that code wasn't for a machine (ask its owner for a machine pairing code)")
    fd = os.open(str(config), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"titan": titan.rstrip("/"), "token": token, "name": name}, f)
    return str(config)


def main(argv: list[str]) -> int:
    if len(argv) >= 5 and argv[1] == "pair":
        try:
            print(f"paired; config written to {pair(Path(argv[2]), argv[3], argv[4], argv[5] if len(argv) > 5 else '')}")
            return 0
        except ConfigError as exc:
            print(f"jb_exec_agent: {exc}", file=sys.stderr)
            return 2
    if len(argv) < 2:
        print(__doc__.strip().splitlines()[0])
        return 2
    try:
        cfg = load_config(Path(argv[1]))
    except ConfigError as exc:
        print(f"jb_exec_agent: {exc}", file=sys.stderr)
        return 2
    if "--once" in argv:
        print(status())
        return 0
    run_forever(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
