#!/usr/bin/env python3
"""titan acting on itself, slice 1 (#1647; part of #1631, #1641): a restricted executor with typed operations.

Not a shell. The executor runs as its own user (`titan-exec`, no login shell, no general sudo) as titan-exec.service,
listening on a unix socket only the agent's account can reach (SO_PEERCRED is checked against ALLOWED_UIDS). A request
is one JSON line, {"op": ..., "args": {...}}; the answer is one JSON line, {"ok": bool, "text": ..., ...}. Every
privileged step is one exact command line in the sudoers file this module writes (`sudoers`), with no wildcard, so the
allowlist and the code can't drift (test_titan_exec checks they match).

Operations (slice 1):
  service.status   {unit}         is-active, sub-state, since, restarts, memory (systemctl show; no sudo)
  service.journal  {unit}         its last 200 log lines, secrets scrubbed (the one sudoers line per unit)
  service.restart  {unit}         restart an allowlisted unit (the one sudoers line per unit)
  gpu.status       {}             each GPU's memory used/total and its processes' PIDs and names (nvidia-smi)
  disk.free        {}             free space on /, /ai and /ai/datasets
  health.last      {check}        canary | preflight | main: how its last run came out, when, its log's last lines
  health.run       {check}        start the canary now (not preflight: root; not main: a gate lane for an hour)
  resources.usage  {}             free disk and the named areas' sizes (/tmp, /ai/tmp, /ai/logs)
  resources.sweep  {area}         start the agreed sweep for a named area (tmp: tmp_sweep.sh), at most once an hour
  models.status    {}             each model server: running or not, since when, the GPUs and memory it holds
  models.stop      {unit}         stop titan-code-model or titan-vision (Zach 10-14), measuring what it held first
  models.start     {unit}         start one again, if its measured memory + 2 GiB is free where it runs

The runbook's rules, in code (a model can't skip them):
  - only the units in UNITS; titan-agent-api (the executor's own caller) is never restarted from inside a turn;
  - no restart inside an eval window (scripts/eval_windows.conf, with its lead) or under an unexpired deploy hold
    (/ai/tmp/deploy.hold holds a future epoch);
  - nothing kills by pattern: a restart is systemd's, by unit name; there is no kill operation here;
  - no secret is printed: journal lines are scrubbed before they leave, and nothing takes a secret on argv.

Install (an admin, after review): bash server-setup/install_titan_exec.sh. It copies this file, eval_windows.py and
eval_windows.conf to /opt/titan-exec, owned by root and read-only: the code that holds the sudo lines never runs from a
checkout the agent's account can write. Re-run it after a deploy that changes them. CLI:
  python3 titan_exec.py sudoers            the sudoers file, to stdout (the installer checks it with visudo)
  python3 titan_exec.py serve              the service (titan-exec.service)
  python3 titan_exec.py call OP [JSON]     one request to the running service (as the agent's account)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))   # eval_windows beside it (root-owned /opt/titan-exec)

SOCKET = Path(os.environ.get("TITAN_EXEC_SOCKET", "/run/titan-exec/exec.sock"))
DEPLOY_HOLD = Path(os.environ.get("TITAN_DEPLOY_HOLD", "/ai/tmp/deploy.hold"))
EVAL_CONF = Path(os.environ.get("EVAL_WINDOWS_CONF") or Path(__file__).resolve().parent / "eval_windows.conf")
JOURNAL_LINES = 200
REPLY_CHARS = 12000
SYSTEMCTL = "/usr/bin/systemctl"
JOURNALCTL = "/usr/bin/journalctl"
SUDO = "/usr/bin/sudo"
NVIDIA_SMI = "/usr/bin/nvidia-smi"
DISKS = ("/", "/ai", "/ai/datasets")

# The units titan may look at and restart for itself, with why each is safe to bounce. Not here, on purpose:
# titan-agent-api (the caller: restarting it ends the turn asking), titan-homeassistant and titan-matter-server (the
# house's locks, alarm and cameras ride on them; #413), the firewall, backups, the deploy and gate machinery, and
# every timer job or static unit (they run and exit: a "restart" would run them; titan-mail-server is one).
UNITS: dict[str, str] = {
    "titan-embeddings.service": "the embeddings server (llama-server :8015)",
    "titan-decider.service": "the local Decider (:8031)",
    "titan-vision.service": "the vision model server",
    "titan-kokoro.service": "speech (Kokoro TTS)",
    "titan-code-model.service": "the code model server",
    "titan-local-agent.service": "the local chat model (llama-server :8010)",
    "fetch-runtime.service": "the fetch runtime",
    "record-runtime.service": "the record runtime",
    "decision-broker.service": "the decision broker (:8030)",
}
# Health checks (slice 2): what titan may read about each, and the one it may start on request. preflight runs as root
# from the checkout, so titan only ever reads its last result; main-health takes a gate lane for up to an hour, so it's
# read too (a gate run on request belongs to the code slice, through the gate queue).
HEALTH: dict[str, tuple[str, bool]] = {    # name → (unit, may titan start it)
    "canary": ("titan-agent-canary.service", True),
    "preflight": ("titan-preflight.service", False),
    "main": ("titan-main-health.service", False),
}
CANARY_EVERY_S = 600      # at most one canary started on request per 10 minutes (the timer's own runs aside)
_last_started: dict[str, float] = {}
_start_lock = __import__("threading").Lock()


def _started_file(name: str) -> Path:
    return SOCKET.parent / f"started-{name}"     # /run/titan-exec: survives the executor's restarts, not a reboot
NEVER_RESTART = {"titan-agent-api.service": "it's titan's own API: restarting it would end this turn"}

_UNIT_RX = re.compile(r"^[a-z0-9][a-z0-9.@_-]{0,80}\.service$")


class Refused(Exception):
    """An operation that isn't done; its message is titan's own words for the person."""


# ── the allowlist, generated ─────────────────────────────────────────────────────────────────────────────────────

def _journal_argv(unit: str) -> list[str]:
    return [JOURNALCTL, "-u", unit, "-n", str(JOURNAL_LINES), "--no-pager", "-o", "short-iso", "--no-hostname"]


def _restart_argv(unit: str) -> list[str]:
    return [SYSTEMCTL, "restart", unit]


def _start_argv(unit: str) -> list[str]:
    return [SYSTEMCTL, "start", "--no-block", unit]


def _privileged() -> list[list[str]]:
    """Every command titan-exec may run as root, in order: the one table both the code and sudoers come from."""
    out = [argv for unit in UNITS for argv in (_journal_argv(unit), _restart_argv(unit))]
    for unit, startable in HEALTH.values():
        out.append(_journal_argv(unit))
        if startable:
            out.append(_start_argv(unit))
    out += [_du_argv(a) for a in AREAS]
    out += [_start_argv(u) for u, _, _ in SWEEPS.values()]
    out += [[SYSTEMCTL, verb, u] for u in STOPPABLE for verb in ("stop", "start")]
    return out


def expected_lines(user: str = "titan-exec") -> list[str]:
    """The NOPASSWD lines, exactly: what the installer compares `sudo -l -U titan-exec` against."""
    return [" ".join(argv) for argv in _privileged()]


def sudoers(user: str = "titan-exec") -> str:
    """The sudoers file: one exact command per line, no wildcard, no shell, no environment kept."""
    lines = [f"# titan-exec (#1647): generated by scripts/titan_exec.py sudoers. Don't edit by hand.",
             f"Defaults:{user} !requiretty, env_reset, !setenv, secure_path=/usr/sbin:/usr/bin:/sbin:/bin"]
    lines += [f"{user} ALL=(root) NOPASSWD: " + " ".join(argv) for argv in _privileged()]
    return "\n".join(lines) + "\n"


# ── the rules ────────────────────────────────────────────────────────────────────────────────────────────────────

def _unit(args: dict[str, Any]) -> str:
    unit = str(args.get("unit") or "").strip().lower()
    if unit and not unit.endswith(".service") and "." not in unit:
        unit += ".service"
    if not _UNIT_RX.match(unit):
        raise Refused("Not done: say which service, by its unit name.")
    if unit in NEVER_RESTART:
        raise Refused(f"Not done: {unit} isn't one titan changes for itself ({NEVER_RESTART[unit]}).")
    if unit not in UNITS:
        raise Refused(f"Not done: {unit} isn't on the list of services titan may touch (titan_exec.UNITS).")
    return unit


def _hold_reason(now: float) -> str | None:
    """An unexpired deploy hold. No file: none. A file titan can't read or understand: treated as held (closed)."""
    try:
        raw = DEPLOY_HOLD.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return "titan can't read the deploy hold, so it won't change anything now"
    try:
        until = float(raw or 0)
    except ValueError:
        return "the deploy hold says something titan doesn't understand"
    if until > now:
        return f"a deploy hold is set until {time.strftime('%H:%MZ', time.gmtime(until))}"
    return None


def _window_reason(now: float) -> str | None:
    """Inside an eval window, or its lead before it (LEAD_MIN), today's or tomorrow's (a lead can cross midnight)."""
    try:
        import eval_windows
        text = EVAL_CONF.read_text()
    except (ImportError, OSError):
        return "titan can't read its eval windows, so it won't restart anything now"
    from datetime import datetime, timedelta, timezone
    lead = next((int(m.group(1)) for m in (re.match(r"\s*LEAD_MIN\s+(\d+)", ln) for ln in text.splitlines()) if m), 0)
    at = datetime.fromtimestamp(now, timezone.utc)
    mins = at.hour * 60 + at.minute + at.second / 60
    rows = eval_windows.parse(text)
    for offset, day in ((0, at.date()), (1440, at.date() + timedelta(days=1))):
        for start, end, name in eval_windows.utc_ranges(rows, day):
            if start + offset - lead <= mins < end + offset:
                hhmm = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"   # noqa: E731
                return f"the {name} eval window ({hhmm(start)}–{hhmm(end)}Z, with {lead} minutes' lead) is on"
    return None


def _may_change(now: float) -> None:
    why = _hold_reason(now) or _window_reason(now)
    if why:
        raise Refused(f"Not done: {why}. Ask again after it.")


# ── scrubbing what leaves ────────────────────────────────────────────────────────────────────────────────────────

# A run of name characters holding one of the words. The word is found in a lookahead, which is never re-entered, then
# the run is taken whole: written as `[…]*word[…]*`, a run repeating the word ("token-token-…") was rescanned once per
# repeat, and one long line took seconds (titan's Python 3.10 has no possessive quantifiers).
_KEY_NAME = (r"(?=[A-Za-z0-9_-]*?(?:authorization|api[_-]?key|token|secret|password|passwd|cookie|credential))"
             r"[A-Za-z0-9_-]+")
_SECRET_RX = [
    # Bearer / Basic credentials, wherever they appear
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # key=value, key: value, "key": "value", 'key': 'value', key="two words"
    # (a name starts only where a run of name characters starts, never inside one: `\b` matched before every letter of
    # "a-a-a-…", and each start rescanned the rest, so one long line took minutes)
    re.compile(r"(?i)([\"']?(?<![A-Za-z0-9_-])" + _KEY_NAME + r"[\"']?)(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s\"',;}]{4,})"),
    # --token value, --api-key=value
    re.compile(r"(?i)((?<![A-Za-z0-9_-])--" + _KEY_NAME + r")(=|\s+)\S+"),
    # well-known key shapes
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{10,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abp]-[A-Za-z0-9-]{10,}"
               r"|\bAIza[0-9A-Za-z_-]{20,}|\bhf_[A-Za-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\bglpat-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),            # a JWT
    re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@"),             # user:password@ in a URL
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"),
]


def scrub(text: str) -> str:
    """Credentials out of a log line before it leaves the executor."""
    out = str(text or "")
    out = _SECRET_RX[0].sub(lambda m: f"{m.group(1)} [removed]", out)
    out = _SECRET_RX[1].sub(lambda m: m.group(0) if m.group(0).rstrip("\"'").endswith("[removed]")
                            else f"{m.group(1)}{m.group(2)}[removed]", out)
    out = _SECRET_RX[2].sub(lambda m: f"{m.group(1)}{m.group(2)}[removed]", out)
    out = _SECRET_RX[3].sub("[key removed]", out)
    out = _SECRET_RX[4].sub("[token removed]", out)
    out = _SECRET_RX[5].sub(lambda m: f"{m.group(1)}[removed]@", out)
    out = _SECRET_RX[6].sub("[private key removed]", out)
    return out


# ── operations ───────────────────────────────────────────────────────────────────────────────────────────────────

UNIT_FILE_OWNER = 0      # root (a test points this at itself)
_EXEC_PRIV_RX = re.compile(r"^\s*Exec[A-Za-z]*\s*=\s*[-@:]*[+!]")


def _not_root(unit: str, run: "Run") -> None:
    """Refuse a unit whose code would run as root: User= empty or root; any Exec*= line with a "+" or "!" prefix (run
    with full privileges); or a unit file or drop-in that isn't root's, or lives under /ai (a checkout zach can
    write, where such a line could be added)."""
    who = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "User", "--value"])
    if who.returncode != 0 or (who.stdout or "").strip() in ("", "root", "0"):
        raise Refused(f"Not done: {unit} would run as root, and titan doesn't start or restart those for itself.")
    cat = run([SYSTEMCTL, "cat", "--no-pager", unit])
    if cat.returncode != 0:
        raise Refused(f"Not done: titan couldn't read {unit}'s unit file, so it won't touch it.")
    for line in (cat.stdout or "").splitlines():
        if line.startswith("# /"):
            path = line[2:].strip()
            try:
                st = os.stat(path)
            except OSError:
                raise Refused(f"Not done: titan couldn't check {path}, so it won't touch {unit}.") from None
            if st.st_uid != UNIT_FILE_OWNER or path.startswith("/ai/") or st.st_mode & 0o022:
                raise Refused(f"Not done: {unit}'s unit file {path} isn't root's alone, so titan won't touch it.")
        elif _EXEC_PRIV_RX.match(line):
            raise Refused(f"Not done: {unit} runs a step with full privileges, and titan doesn't start or restart those.")

Run = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                          env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "SYSTEMD_PAGER": "",
                               "PAGER": "cat"})


def op_service_status(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    unit = _unit(args)
    r = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState,SubState,ActiveEnterTimestamp,NRestarts,"
             "MemoryCurrent,MainPID"])
    facts = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
    mem = facts.get("MemoryCurrent", "")
    mem_mb = f"{int(mem) // (1 << 20)} MB" if mem.isdigit() else "unknown"
    text = (f"{unit} ({UNITS[unit]}): {facts.get('ActiveState', '?')} ({facts.get('SubState', '?')}) since "
            f"{facts.get('ActiveEnterTimestamp') or '?'}; restarted {facts.get('NRestarts', '?')} times; memory {mem_mb}.")
    return {"ok": r.returncode == 0, "text": text, "unit": unit, "active": facts.get("ActiveState")}


def op_service_journal(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    unit = _unit(args)
    r = run([SUDO, "-n"] + _journal_argv(unit))
    if r.returncode != 0:
        return {"ok": False, "text": f"Couldn't read {unit}'s log (exit {r.returncode}).", "unit": unit}
    text = scrub(r.stdout or "")[-REPLY_CHARS:]
    return {"ok": True, "text": text or "(no log lines)", "unit": unit, "lines": text.count("\n")}


_ERROR_RX = re.compile(r"\b(error|fatal|panic(?:ked)?|traceback|exception|segfault|core dumped|oom|out of memory|killed)\b",
                       re.IGNORECASE)
_EXEC_PATH_RX = re.compile(r"\bpath=(/[^\s;]+)")   # ExecStart's program only: never its argv, which can carry a secret
_INTERPRETERS = ("python", "bash", "sh", "dash", "zsh", "node", "env", "perl", "ruby")
_FAILED_RX = re.compile(r"Failed with result '([a-z-]{1,20})'")   # systemd's own line when a unit's process stops badly
CHECK_ERROR_LINES = 3
_BAD_RESULT = {"exit-code": "exited with an error", "signal": "was killed by a signal", "core-dump": "crashed (core dump)",
               "timeout": "timed out", "oom-kill": "was killed for using too much memory", "watchdog": "stopped answering"}


def _built(program: str) -> str | None:
    """When a service's program was last replaced on disk, or None for an interpreter (its code lives elsewhere)."""
    name = Path(program).name.lower()
    if not program.startswith("/") or name in _INTERPRETERS or name.rstrip("0123456789.") in _INTERPRETERS \
            or name.endswith((".sh", ".py", ".pl", ".rb", ".js")):
        return None   # an interpreter or a script: its date isn't the service's program
    try:
        return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(Path(program).stat().st_mtime))
    except OSError:
        return None


def op_service_check(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    """#1638 "check an application", for one of titan's services: running or not and since when, restarts, how it last
    stopped (a crash, a signal, the OOM killer), memory and CPU time, when its program was built, and its recent error
    lines. Read-only, from the commands the allowlist already has: no new sudo line."""
    unit = _unit(args)
    r = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState,SubState,ActiveEnterTimestamp,NRestarts,"
             "MemoryCurrent,CPUUsageNSec,Result,ExecMainStatus,ExecMainExitTimestamp,ExecStart"])
    if r.returncode != 0:
        return {"ok": False, "text": f"Couldn't read {unit}'s state (exit {r.returncode}).", "unit": unit}
    facts = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
    active = facts.get("ActiveState") or "?"
    parts = [f"{unit} ({UNITS[unit]}): {active} ({facts.get('SubState') or '?'}) since "
             f"{facts.get('ActiveEnterTimestamp') or '?'}; restarted {facts.get('NRestarts') or '?'} times"]
    result = facts.get("Result") or ""
    stopped = active not in ("active", "reloading", "activating")
    if stopped and result and result != "success":   # systemd resets Result when a unit starts again
        how = _BAD_RESULT.get(result, f"stopped badly ({result[:20]})")
        status = facts.get("ExecMainStatus") or "?"
        parts.append(f"when it stopped it {how}, status {status}, at {facts.get('ExecMainExitTimestamp') or '?'}")
    mem, cpu = facts.get("MemoryCurrent", ""), facts.get("CPUUsageNSec", "")
    parts.append(f"memory {int(mem) // (1 << 20)} MB" if mem.isdigit() else "memory unknown")
    parts.append(f"CPU time {int(cpu) // 10**9} s since it started" if cpu.isdigit() else "CPU time unknown")
    m = _EXEC_PATH_RX.search(facts.get("ExecStart", ""))
    built = _built(m.group(1)) if m else None
    if built:
        parts.append(f"its program ({Path(m.group(1)).name}) was built {built}")
    j = run([SUDO, "-n"] + _journal_argv(unit))
    if j.returncode != 0:
        return {"ok": True, "text": "; ".join(parts) + f". Couldn't read its log (exit {j.returncode}).", "unit": unit,
                "active": active, "errors": "", "error_count": None}
    lines = scrub(j.stdout or "").splitlines()
    if not stopped:   # running again: how it last stopped is only in systemd's line in its log
        last = next((m.group(1) for ln in reversed(lines) for m in [_FAILED_RX.search(ln)] if m), None)
        if last in _BAD_RESULT:   # one of systemd's own words, never the line itself
            parts.insert(1, f"the last time it stopped (its log says) it {_BAD_RESULT[last]}")
    bad = [ln[:240] for ln in lines if _ERROR_RX.search(ln)]
    parts.append(f"{len(bad)} error lines in its last {JOURNAL_LINES} log lines" if bad else
                 f"no error lines in its last {JOURNAL_LINES} log lines")
    return {"ok": True, "text": "; ".join(parts) + ".", "unit": unit, "active": active,
            "errors": "\n".join(bad[-CHECK_ERROR_LINES:]), "error_count": len(bad)}


def op_service_restart(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    unit = _unit(args)
    _may_change(time.time() if now is None else now)
    _not_root(unit, run)
    en = run([SYSTEMCTL, "is-enabled", unit])
    if (en.stdout or "").strip() != "enabled":
        raise Refused(f"Not done: {unit} is disabled on purpose; turning it back on is a decision, not a restart.")
    with _start_lock:   # the same lock as a model's room check and start: nothing takes the memory in between
        if unit in STOPPABLE:
            st = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState", "--value"])
            if st.returncode != 0 or (st.stdout or "").strip() != "active":
                raise Refused(f"Not done: {unit} isn't running, and a restart would start it without the room check; "
                              "use models.start.")
        r = run([SUDO, "-n"] + _restart_argv(unit), timeout=120)
    if r.returncode != 0:
        return {"ok": False, "text": f"The restart of {unit} failed (exit {r.returncode}).", "unit": unit}
    after = op_service_status({"unit": unit}, run=run)
    if after["active"] == "activating":
        return {"ok": True, "text": f"Restarted {unit}; it's still starting. Now: {after['text']}", "unit": unit}
    return {"ok": after["active"] == "active", "text": f"Restarted {unit}. Now: {after['text']}", "unit": unit}


PROC = Path("/proc")
_CGROUP_UNIT_RX = re.compile(r"^0::/system\.slice/([A-Za-z0-9@._:-]+\.service)(?:/|$)")   # system units only
_PROG_RX = re.compile(r"^[A-Za-z0-9._@:+-]{1,40}$")


def _prog(name: str) -> str:
    """A GPU process's program name as shown: its basename if it's plain, else "?" (a crafted name never reaches the model)."""
    base = os.path.basename(str(name or ""))
    return base if _PROG_RX.match(base) else "?"


def _unit_of(pid: str) -> str | None:
    """The systemd unit a process runs in, from its cgroup (world-readable; never its arguments or environment)."""
    if not str(pid).isdigit():
        return None
    try:
        text = (PROC / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    units = [m.group(1) for line in text.splitlines() for m in [_CGROUP_UNIT_RX.match(line.strip())] if m]
    return units[0] if units else None


def _gpu_rows(run: Run) -> tuple[list[tuple[str, str, str, str, str]], dict[str, list[tuple[str, str, str, str | None]]]] | None:
    """[(index, name, used, total, util)], {index: [(program, pid, MiB, unit)]}, or None when nvidia-smi fails."""
    g = run([NVIDIA_SMI, "--query-gpu=index,name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"])
    p = run([NVIDIA_SMI, "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    u = run([NVIDIA_SMI, "--query-gpu=index,uuid", "--format=csv,noheader"])
    if g.returncode != 0:
        return {"failed": g.returncode}  # type: ignore[return-value]
    by_uuid = {}
    for line in (u.stdout or "").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 2:
            by_uuid[parts[1]] = parts[0]
    procs: dict[str, list[tuple[str, str, str, str | None]]] = {}
    for line in (p.stdout or "").splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) == 4:
            procs.setdefault(by_uuid.get(parts[0], "?"), []).append(
                (_prog(parts[2]), parts[1], parts[3], _unit_of(parts[1])))   # the program's plain name only
    gpus = [tuple(x.strip() for x in line.split(",")) for line in (g.stdout or "").splitlines()]
    return [r for r in gpus if len(r) == 5], procs


def op_gpu_status(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    if not shutil.which(NVIDIA_SMI) and run is _run:
        return {"ok": False, "text": "No GPUs here (nvidia-smi isn't installed)."}
    rows = _gpu_rows(run)
    if isinstance(rows, dict):
        return {"ok": False, "text": f"nvidia-smi failed (exit {rows['failed']})."}
    gpus, procs = rows
    out = []
    for idx, name, used, total, util in gpus:
        who = "; ".join(f"{unit + ': ' + prog if unit else prog} (PID {pid}, {mib} MiB)"
                        for prog, pid, mib, unit in procs.get(idx, [])) or "no processes"
        out.append(f"GPU{idx} {name}: {used}/{total} MiB used, {util}% busy: {who}")
    return {"ok": True, "text": "\n".join(out) or "No GPUs reported."}


# Models (slice 4, read-only): titan's model servers, what each is doing and which GPUs it holds. Loading, swapping or
# stopping one comes next, within the runbook's GPU budget.
MODELS: dict[str, str] = {
    "titan-local-agent.service": "the local chat model (Qwen3-8B, :8010)",
    "titan-code-model.service": "the code model (Qwen3.6-35B-A3B, :8198)",
    "titan-vision.service": "the vision model (:8042)",
    "titan-decider.service": "the local Decider (:8031)",
    "titan-embeddings.service": "the embeddings model (:8015)",
    "titan-kokoro.service": "speech (Kokoro TTS)",
}


def op_models_status(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    """Each model server: running or not, since when, and the GPUs and memory its processes hold."""
    rows = _gpu_rows(run) if (shutil.which(NVIDIA_SMI) or run is not _run) else None
    if isinstance(rows, dict):
        rows = None
    held: dict[str, list[str]] = {}
    for idx, plist in (rows[1].items() if rows else []):
        for prog, pid, mib, unit in plist:
            if unit:
                held.setdefault(unit, []).append(f"GPU{idx} {mib} MiB")
    out = []
    for unit, what in MODELS.items():
        r = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState,SubState,ActiveEnterTimestamp"])
        f = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
        gpus = ", ".join(held.get(unit, [])) or ("no GPU" if rows else "GPUs unknown")
        out.append(f"{unit} ({what}): {f.get('ActiveState', '?')} ({f.get('SubState', '?')}) since "
                   f"{f.get('ActiveEnterTimestamp') or '?'}; {gpus}.")
    others = sorted({unit or prog for plist in (rows[1].values() if rows else []) for prog, _, _, unit in plist
                     if (unit or prog) not in MODELS})
    if others:
        out.append("Also holding a GPU: " + ", ".join(others) + ".")
    return {"ok": True, "text": "\n".join(out)}


# Models, slice 5 (Zach 10-14): titan may stop and start only these, on request (always asking), outside eval windows
# and their lead, never under a deploy hold. A start is refused unless the place it runs has its measured memory plus
# 2 GiB free: for a GPU model, on each GPU it held when measured; for a CPU model (vision), the host's available memory.
# Never the local chat model (the Gemini fallback), the Decider or embeddings. Swaps are #408's
# eval-gated card, never the executor.
STOPPABLE = ("titan-code-model.service", "titan-vision.service")
MODEL_KIND = {"titan-code-model.service": "gpu", "titan-vision.service": "cpu"}   # fixed here, never inferred
HEADROOM_MIB = 2048
MODEL_CHANGE_EVERY_S = 600
STEADY_S = 120                    # a model is measured only once it has been up this long (not while it loads)
MEASURE_MAX_AGE_S = 30 * 86400    # an older measurement isn't trusted for a start


def _measured_file(unit: str) -> Path:
    return SOCKET.parent / f"measured-{unit}.json"


def _fingerprint(unit: str, run: Run) -> str | None:
    """The unit's file and drop-ins with their modification times: a measurement is for this configuration only."""
    r = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "FragmentPath,DropInPaths"])
    if r.returncode != 0:
        return None
    f = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
    paths = [f.get("FragmentPath", "")] + (f.get("DropInPaths", "") or "").split()
    parts = []
    for path in [p for p in paths if p]:
        try:
            parts.append(f"{path}@{int(os.stat(path).st_mtime)}")
        except OSError:
            return None
    return "|".join(parts) or None


def _measure(unit: str, run: Run, now: float) -> dict[str, Any] | None:
    """What a running model holds now, once it has been up STEADY_S: {gpus: {index: MiB}, host_mib, fingerprint, at}.
    Kept beside the socket for a later start; a GPU model with no GPU numbers is never kept (it would read as no need),
    and a measurement is written whole (temp file, then rename)."""
    st = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState,SubState,MemoryCurrent,ActiveEnterTimestampMonotonic"])
    f = dict(line.split("=", 1) for line in (st.stdout or "").splitlines() if "=" in line)
    if f.get("ActiveState") != "active":
        return None
    since = f.get("ActiveEnterTimestampMonotonic", "")
    if since.isdigit() and int(since) and time.monotonic() - int(since) / 1e6 < STEADY_S:
        return None   # still loading: what it holds now is less than it will
    rows = _gpu_rows(run) if (shutil.which(NVIDIA_SMI) or run is not _run) else None
    gpus: dict[str, int] = {}
    if rows and not isinstance(rows, dict):
        for idx, plist in rows[1].items():
            for _prog_name, _pid, mib, u in plist:
                if u == unit and str(mib).isdigit():
                    gpus[idx] = gpus.get(idx, 0) + int(mib)
    if MODEL_KIND.get(unit) == "gpu" and not gpus:
        return None
    mem = f.get("MemoryCurrent", "")
    m = {"gpus": gpus, "host_mib": int(mem) // (1 << 20) if mem.isdigit() else 0, "at": now,
         "fingerprint": _fingerprint(unit, run)}
    try:
        tmp = _measured_file(unit).with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as fh:
            json.dump(m, fh)
        os.replace(tmp, _measured_file(unit))
    except OSError:
        pass
    return m


def _model_unit(args: dict[str, Any]) -> str:
    unit = str(args.get("unit") or "").strip().lower()
    if unit and not unit.endswith(".service"):
        unit += ".service"
    if unit not in STOPPABLE:
        raise Refused("Not done: titan stops and starts only " + " and ".join(STOPPABLE) + " (Zach 10-14); never the "
                      "local chat model, the Decider or embeddings. A swap is the eval card (#408).")
    return unit


def _host_available_mib() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def op_models_stop(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    unit = _model_unit(args)
    now = time.time() if now is None else now
    _may_change(now)
    _not_root(unit, run)
    st = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState", "--value"])
    if (st.stdout or "").strip() not in ("active", "activating", "reloading"):
        return {"ok": True, "text": f"{unit} isn't running; nothing to stop.", "unit": unit}
    with _start_lock:
        _rate(f"model-{unit}", now)
        m = _measure(unit, run, now)   # None while it's still loading: the last good measurement stays
        _stamp(f"model-{unit}", now)
        r = run([SUDO, "-n", SYSTEMCTL, "stop", unit], timeout=120)
        if r.returncode != 0:
            return {"ok": False, "text": f"Stopping {unit} failed (exit {r.returncode}).", "unit": unit}
    held = (", ".join(f"GPU{i} {mib} MiB" for i, mib in sorted(m["gpus"].items())) or f"{m['host_mib']} MiB of memory"
            if m else "an amount titan didn't measure (it wasn't up long enough); the last measurement stays")
    return {"ok": True, "text": f"Stopped {unit}; it held {held}. Start it again with models.start.", "unit": unit}


def op_models_start(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    unit = _model_unit(args)
    now = time.time() if now is None else now
    _may_change(now)
    _not_root(unit, run)
    st = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState", "--value"])
    if st.returncode != 0:
        raise Refused(f"Not done: titan couldn't tell whether {unit} is running.")
    if (st.stdout or "").strip() in ("active", "activating", "reloading"):
        return {"ok": True, "text": f"{unit} is already running.", "unit": unit}
    try:
        m = json.loads(_measured_file(unit).read_text())
        need = {str(k): int(v) for k, v in (m.get("gpus") or {}).items()}
        host_need = int(m.get("host_mib") or 0)
        at = float(m.get("at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        raise Refused(f"Not done: titan hasn't measured {unit} yet (it measures a model when it stops it, once it has "
                      "been up a while), so it can't tell whether there's room. Start it by hand once.") from None
    if now - at > MEASURE_MAX_AGE_S:
        raise Refused(f"Not done: titan's measurement of {unit} is more than 30 days old; start it by hand once.")
    fp = _fingerprint(unit, run)
    if not fp or fp != m.get("fingerprint"):
        raise Refused(f"Not done: {unit}'s unit file changed since titan measured it, so the old numbers may not hold; "
                      "start it by hand once.")
    if MODEL_KIND.get(unit) == "gpu" and not need:
        raise Refused(f"Not done: titan has no GPU numbers for {unit}, so it can't tell whether there's room.")
    with _start_lock:   # the check and the start under one lock (a restart takes it too)
        _rate(f"model-{unit}", now)
        short = []
        if need:
            rows = _gpu_rows(run)
            if not rows or isinstance(rows, dict):
                raise Refused("Not done: titan couldn't read the GPUs, so it can't tell whether there's room.")
            free = {idx: int(total) - int(used) for idx, _n, used, total, _u in rows[0] if used.isdigit() and total.isdigit()}
            short += [f"GPU{i} has {free.get(i, 0)} MiB free, needs {mib + HEADROOM_MIB}" for i, mib in sorted(need.items())
                      if free.get(i, 0) < mib + HEADROOM_MIB]
        avail = _host_available_mib()
        host_want = host_need + HEADROOM_MIB if MODEL_KIND.get(unit) == "cpu" else host_need
        if avail is None or avail < host_want:
            short.append(f"the host has {avail if avail is not None else '?'} MiB available, needs {host_want}")
        if short:
            raise Refused(f"Not done: not enough room for {unit}: " + "; ".join(short) + ".")
        _stamp(f"model-{unit}", now)   # every real attempt counts, a failed one too: a crashing unit can't be retried at once
        r = run([SUDO, "-n", SYSTEMCTL, "start", unit], timeout=180)
        if r.returncode != 0:
            return {"ok": False, "text": f"Starting {unit} failed (exit {r.returncode}).", "unit": unit}
    after = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState", "--value"])
    state = (after.stdout or "").strip()
    return {"ok": state in ("active", "activating"), "text": f"Started {unit}; it's {state or 'unknown'}.", "unit": unit}


def _rate(key: str, now: float) -> None:
    """At most one change per MODEL_CHANGE_EVERY_S for a model (stop or start), so it can't be flapped."""
    last = _last_started.get(key)
    if last is None:
        try:
            last = float(_started_file(key).read_text().strip())
        except FileNotFoundError:
            last = None
        except (OSError, ValueError):
            last = now
    if last is not None and now - last < MODEL_CHANGE_EVERY_S:
        raise Refused(f"Not done: that model was changed {int((now - last) // 60)} minutes ago; at most once per "
                      f"{MODEL_CHANGE_EVERY_S // 60} minutes.")


def _stamp(key: str, now: float) -> None:
    _last_started[key] = now
    try:
        fd = os.open(_started_file(key), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as fh:
            fh.write(str(now))
    except OSError:
        pass


def op_disk_free(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    out = []
    for d in DISKS:
        try:
            st = os.statvfs(d)
        except OSError:
            continue
        free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        out.append(f"{d}: {free / (1 << 30):.0f} GB free of {total / (1 << 30):.0f} GB")
    return {"ok": bool(out), "text": "\n".join(out) or "No disks found."}


def _health(args: dict[str, Any]) -> tuple[str, str, bool]:
    name = str(args.get("check") or "").strip().lower()
    if name not in HEALTH:
        raise Refused("Not done: say which check: " + ", ".join(HEALTH) + ".")
    unit, startable = HEALTH[name]
    return name, unit, startable


def op_health_last(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    """A health check's last run: how it came out, when, and its log's last lines (scrubbed)."""
    name, unit, _ = _health(args)
    r = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState,Result,ExecMainStatus,ExecMainExitTimestamp,"
             "ExecMainStartTimestamp"])
    f = dict(line.split("=", 1) for line in (r.stdout or "").splitlines() if "=" in line)
    state = "running now" if f.get("ActiveState") in ("activating", "active") else (
        f"{f.get('Result') or '?'} (exit {f.get('ExecMainStatus') or '?'})")
    j = run([SUDO, "-n"] + _journal_argv(unit))
    tail = "\n".join(scrub(j.stdout or "").splitlines()[-25:]) if j.returncode == 0 else "(its log couldn't be read)"
    text = (f"The {name} check: {state}; last started {f.get('ExecMainStartTimestamp') or '?'}, finished "
            f"{f.get('ExecMainExitTimestamp') or '?'}.\n{tail}")
    return {"ok": r.returncode == 0 and j.returncode == 0, "text": text, "check": name, "result": f.get("Result")}


def op_health_run(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    """Start a check titan may start (the canary), without waiting: it reports through health.last."""
    name, unit, startable = _health(args)
    if not startable:
        raise Refused(f"Not done: titan reads the {name} check's last result but doesn't start it"
                      + (" (it runs as root)." if name == "preflight" else " (it holds a gate lane for up to an hour)."))
    err = _start_oneshot(name, unit, CANARY_EVERY_S, f"the {name} check", run, time.time() if now is None else now)
    if err:
        return {"ok": False, "text": err, "check": name}
    return {"ok": True, "text": f"Started the {name} check. It takes a few minutes; ask for its result after.",
            "check": name}


def _start_oneshot(key: str, unit: str, every_s: int, what: str, run: Run, now: float) -> str | None:
    """Start a oneshot titan may start, without waiting. Refused (raises) in an eval window or its lead, under a deploy
    hold, while it runs, for root code, or within `every_s` of the last start titan made (kept beside the socket, under a
    lock). → None when started, else why it couldn't start."""
    _may_change(now)
    st = run([SYSTEMCTL, "show", unit, "--no-pager", "-p", "ActiveState", "--value"])
    if st.returncode != 0:
        raise Refused(f"Not done: titan couldn't tell whether {what} is running.")
    if (st.stdout or "").strip() in ("activating", "active", "deactivating", "reloading"):
        raise Refused(f"Not done: {what} is already running; ask again in a few minutes.")
    _not_root(unit, run)
    with _start_lock:
        last = _last_started.get(key)
        if last is None:
            try:
                last = float(_started_file(key).read_text().strip())
            except FileNotFoundError:
                last = None
            except (OSError, ValueError):
                last = now   # a record titan can't read or understand: as if just started (closed, not open)
        if last is not None and now - last < every_s:
            raise Refused(f"Not done: titan started {what} {int((now - last) // 60)} minutes ago; at most once "
                          f"per {every_s // 60} minutes.")
        r = run([SUDO, "-n"] + _start_argv(unit))
        if r.returncode != 0:
            return f"Couldn't start {what} (exit {r.returncode})."
        _last_started[key] = now
        try:
            fd = os.open(_started_file(key), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w") as f:
                f.write(str(now))
            os.chmod(_started_file(key), 0o640)
        except OSError:
            pass
    return None


# Resources (slice 3): the sizes of the named areas, and the one agreed sweep titan may start. Never a path someone
# names, never a glob: /ai/tmp is shared between sessions, so nothing here removes anything there.
AREAS = ("/tmp", "/ai/tmp", "/ai/logs")
SWEEPS: dict[str, tuple[str, int, str]] = {   # name → (unit, at most once per s, what it does)
    "tmp": ("titan-tmp-sweep.service", 3600,
            "removes zach's own /tmp entries untouched for 2 days that nothing has open (tmp_sweep.sh)"),
}
DU = "/usr/bin/du"
DU_LIMIT_S = 40                 # root's own timeout per area: a walk never outlives it (sudo can't pass SIGKILL on)
USAGE_CACHE_S = 600
_usage_lock = __import__("threading").Lock()
_usage_cache: dict[str, Any] = {"at": 0.0, "text": None}


def _du_argv(area: str) -> list[str]:
    return ["/usr/bin/timeout", str(DU_LIMIT_S), "/usr/bin/ionice", "-c3", "/usr/bin/nice", "-n", "19",
            DU, "-sx", "--block-size=1M", area]


def op_resources_usage(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    """Free disk, then each named area's size (a root read: other users' files count too). One measurement at a time,
    kept 10 minutes; a fresh one waits outside the eval windows (the walk is disk work)."""
    now = time.time() if now is None else now
    free = op_disk_free({}, run=run)["text"]
    cached = _usage_cache["text"]
    if cached and now - _usage_cache["at"] < USAGE_CACHE_S:
        return {"ok": True, "text": f"{free}\n{cached}\n(sizes measured {int((now - _usage_cache['at']) // 60)} min ago)"}
    if _window_reason(now) or _hold_reason(now):
        return {"ok": True, "text": f"{free}\n(The areas' sizes are measured outside eval windows and deploy holds.)"}
    if not _usage_lock.acquire(blocking=False):
        return {"ok": True, "text": f"{free}\n(The areas' sizes are being measured now; ask again in a minute.)"}
    try:
        out = []
        for area in AREAS:
            r = run([SUDO, "-n"] + _du_argv(area), timeout=DU_LIMIT_S + 5)
            first = (r.stdout or "").split()
            mb = first[0] if r.returncode in (0, 1) and first and first[0].isdigit() else None
            out.append(f"{area}: about {int(mb) / 1024:.1f} GB" if mb else f"{area}: couldn't be measured in time")
        out.append("Titan can sweep: " + "; ".join(f"{k} ({v[2]})" for k, v in SWEEPS.items()) + ".")
        text = "\n".join(out)
        _usage_cache.update(at=now, text=text)
    finally:
        _usage_lock.release()
    return {"ok": True, "text": f"{free}\n{text}"}


def op_resources_sweep(args: dict[str, Any], run: Run = _run, now: float | None = None) -> dict[str, Any]:
    name = str(args.get("area") or "").strip().lower()
    if name not in SWEEPS:
        raise Refused("Not done: titan sweeps only " + ", ".join(SWEEPS) + " (named areas, never a path or a glob).")
    unit, every_s, what = SWEEPS[name]
    err = _start_oneshot(f"sweep-{name}", unit, every_s, f"the {name} sweep", run, time.time() if now is None else now)
    if err:
        return {"ok": False, "text": err, "area": name}
    return {"ok": True, "text": f"Started the {name} sweep: it {what}. Ask for the sizes again in a few minutes.",
            "area": name}


OPS: dict[str, Callable[..., dict[str, Any]]] = {
    "service.status": op_service_status,
    "service.journal": op_service_journal,
    "service.check": op_service_check,
    "service.restart": op_service_restart,
    "gpu.status": op_gpu_status,
    "disk.free": op_disk_free,
    "health.last": op_health_last,
    "health.run": op_health_run,
    "resources.usage": op_resources_usage,
    "resources.sweep": op_resources_sweep,
    "models.status": op_models_status,
    "models.stop": op_models_stop,
    "models.start": op_models_start,
}
CHANGES = frozenset({"service.restart", "health.run", "resources.sweep", "models.stop", "models.start"})   # what changes the host (the agent's tool holds these for the person's OK)

# The agent's tool for each operation, and how risky each is (#1631 slice 3): the executor's own capability list, in
# the shape the Mac's device_tools take (docs/design/executor-protocol.md). test_titan_exec keeps it equal to OPS and to
# each tool's descriptor, so the list, the code and governance never disagree.
TOOL_OF = {"service.status": "titan_service", "service.journal": "titan_service", "service.check": "titan_service",
           "service.restart": "titan_service_restart", "gpu.status": "titan_gpu", "disk.free": "titan_resources",
           "health.last": "titan_health", "health.run": "titan_health_run", "resources.usage": "titan_resources",
           "resources.sweep": "titan_resources_sweep", "models.status": "titan_models", "models.stop": "titan_model_stop",
           "models.start": "titan_model_start"}
DANGEROUS = frozenset({"resources.sweep"})   # removes files: it can't be undone


def capabilities() -> list[dict[str, Any]]:
    """What this executor runs: [{op, tool, risk, changes}], one per operation, in OPS's order."""
    return [{"op": op, "tool": TOOL_OF[op], "risk": "dangerous" if op in DANGEROUS else "write" if op in CHANGES else "read",
             "changes": op in CHANGES} for op in OPS]


def handle(request: Any, run: Run = _run, now: float | None = None) -> dict[str, Any]:
    """One request → one answer. Never raises."""
    if not isinstance(request, dict) or not isinstance(request.get("op"), str) or request["op"] not in OPS:
        return {"ok": False, "text": "Not done: that isn't an operation titan has (" + ", ".join(OPS) + ")."}
    args = request.get("args") if isinstance(request.get("args"), dict) else {}
    try:
        out = OPS[request["op"]](args, run=run, now=now)
    except Refused as exc:
        return {"ok": False, "refused": True, "text": str(exc)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "It took too long and was stopped."}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": f"It failed ({type(exc).__name__})."}
    out["text"] = str(out.get("text", ""))[:REPLY_CHARS]
    return out


# ── the service and its client ───────────────────────────────────────────────────────────────────────────────────

def _allowed_uids() -> set[int]:
    raw = os.environ.get("TITAN_EXEC_ALLOWED_UIDS", "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def _peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except (AttributeError, OSError):
        return None


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(10)   # a caller that connects and says nothing can't hold a thread
        uid = _peer_uid(self.request)
        if uid is None or uid not in _allowed_uids():
            self.wfile.write(json.dumps({"ok": False, "text": "Not done: this caller may not use titan's executor."}).encode() + b"\n")
            return
        try:
            line = self.rfile.readline(65536)
        except OSError:
            return
        try:
            req = json.loads(line or b"{}")
        except ValueError:
            req = None
        out = handle(req)
        _audit(uid, req, out)
        self.wfile.write(json.dumps(out).encode() + b"\n")


def _audit(uid: int, req: Any, out: dict[str, Any]) -> None:
    """One line per request to the journal (titan-exec.service's own): who, which op and unit, and how it came out."""
    op = req.get("op") if isinstance(req, dict) else None
    a = req.get("args") if isinstance(req, dict) and isinstance(req.get("args"), dict) else {}
    print(json.dumps({"uid": uid, "op": op, "unit": a.get("unit"), "check": a.get("check"), "ok": out.get("ok"),
                      "refused": out.get("refused", False)}), flush=True)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve() -> int:
    if not _allowed_uids():
        print("TITAN_EXEC_ALLOWED_UIDS is empty: nobody could call; not starting", file=sys.stderr)
        return 2
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    try:
        SOCKET.unlink()
    except FileNotFoundError:
        pass
    import hashlib
    for f in (Path(__file__), EVAL_CONF):
        try:
            print(json.dumps({"start": str(f), "sha256": hashlib.sha256(f.read_bytes()).hexdigest()[:16]}), flush=True)
        except OSError:
            print(json.dumps({"start": str(f), "sha256": None}), flush=True)
    with _Server(str(SOCKET), _Handler) as srv:
        os.chmod(SOCKET, 0o660)
        srv.serve_forever()
    return 0


def call(op: str, args: dict[str, Any] | None = None, *, path: Path | None = None, timeout: float = 150) -> dict[str, Any]:
    """One request to the running executor. Raises OSError when it isn't there."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(path or SOCKET))
        s.sendall(json.dumps({"op": op, "args": args or {}}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


def _main(argv: list[str]) -> int:
    if argv[:1] == ["sudoers"]:
        sys.stdout.write(sudoers(argv[1] if len(argv) > 1 else "titan-exec"))
        return 0
    if argv[:1] == ["serve"]:
        return serve()
    if argv[:1] == ["call"] and len(argv) in (2, 3):
        out = call(argv[1], json.loads(argv[2]) if len(argv) == 3 else {})
        print(out.get("text", ""))
        return 0 if out.get("ok") else 1
    print("usage: titan_exec.py sudoers [user] | serve | call OP [JSON]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
