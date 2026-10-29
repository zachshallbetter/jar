#!/usr/bin/env python3
"""titan diagnosed from the Mac: the outside hand's first, read-only slice (#1632; #1647 is titan's own executor).

When titan is the thing that's broken, its own tools can't say why. This runs on the Mac and works down the chain,
stopping at the first link that's down, and says what the runbook's first step is for it. It changes nothing on titan
or the Mac: every check is a read, and every fix is a sentence for a person (a confirmed action is a later slice).

  tailnet    is titan in this Mac's Tailscale, and online (`tailscale status --json`)
  ports      do titan's SSH (22) and agent API (7770) accept a connection
  api        does GET /health answer {"status": "ok"}
  ssh        does a key-only, no-prompt SSH login work (BatchMode)
  services   over that SSH: the system's state and the failed units, by name only (never a process's arguments, never
             an environment, never a file's contents: the runbook's no-secrets rule)

  python3 scripts/titan_doctor.py            the report, one line per check, then what to do first
  python3 scripts/titan_doctor.py --json     the same as JSON (what Jellybean reads)

TITAN_HOST (default titan-node.ladon-betelgeuse.ts.net), TITAN_SSH (default the `titan` ssh alias). Exit 0 when every
check passes, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ops_ports import AGENT_API_PORT  # noqa: E402

HOST = os.environ.get("TITAN_HOST", "titan-node.ladon-betelgeuse.ts.net")
SSH_ALIAS = os.environ.get("TITAN_SSH", "titan")
TIMEOUT = 5.0
_UNIT_RX = re.compile(r"^[A-Za-z0-9@._:-]{1,120}$")
# What titan runs, over SSH: fixed text, no arguments from anywhere. `systemctl --failed` prints unit names and states.
REMOTE = "systemctl is-system-running; systemctl --failed --no-legend --plain --no-pager | awk '{print $1}'"


@dataclass
class Check:
    name: str
    ok: bool
    said: str
    first_step: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


Run = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def check_tailnet(run: Run = _run) -> Check:
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        r = run([exe, "status", "--json"], timeout=10)
        status = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return Check("tailnet", False, "Tailscale on this Mac didn't answer.",
                     "Open Tailscale on this Mac and make sure it's connected.")
    if (status.get("BackendState") or "") != "Running":
        return Check("tailnet", False, f"Tailscale on this Mac is {status.get('BackendState') or 'not running'}.",
                     "Connect Tailscale on this Mac.")
    short = HOST.split(".")[0]
    for peer in (status.get("Peer") or {}).values():
        dns = str(peer.get("DNSName") or "").rstrip(".")
        if dns == HOST or str(peer.get("HostName") or "") == short:
            if peer.get("Online"):
                return Check("tailnet", True, "titan is online in the tailnet.", facts={"last_seen": peer.get("LastSeen")})
            return Check("tailnet", False, f"titan is offline in the tailnet (last seen {peer.get('LastSeen') or 'unknown'}).",
                         "titan may be off, asleep or without network: check its power and the house network, or its "
                         "console (runbook: the machine itself).", {"last_seen": peer.get("LastSeen")})
    return Check("tailnet", False, "titan isn't in this Mac's tailnet.", "Sign this Mac into the household tailnet.")


def check_ports(connect: Callable[..., Any] = socket.create_connection) -> Check:
    open_, shut = [], []
    for port, what in ((22, "SSH"), (AGENT_API_PORT, "agent API")):
        try:
            connect((HOST, port), timeout=TIMEOUT).close()
            open_.append(f"{what} ({port})")
        except OSError:
            shut.append(f"{what} ({port})")
    if not shut:
        return Check("ports", True, "SSH and the agent API accept connections.")
    if len(shut) == 2:
        return Check("ports", False, "Neither SSH nor the agent API answers.",
                     "titan is in the tailnet but not answering: its firewall or its network on titan's side "
                     "(runbook: nftables, tailscale0).", {"closed": shut})
    step = ("The agent API is down while SSH works: look at titan-agent-api (the services check below)."
            if "agent API" in shut[0] else "SSH is down while the API answers: check sshd on titan from its console.")
    return Check("ports", False, f"{shut[0]} doesn't answer; {open_[0]} does.", step, {"closed": shut})


def check_api(fetch: Callable[[str], Any] | None = None) -> Check:
    url = f"http://{HOST}:{AGENT_API_PORT}/health"
    try:
        if fetch is None:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:   # noqa: S310 (titan's own health route)
                body = json.loads(r.read(65536) or b"{}")
        else:
            body = fetch(url)
    except (urllib.error.URLError, OSError, ValueError):
        return Check("api", False, "The agent API's health check didn't answer.",
                     "Look at titan-agent-api's state and log (the services check, or `ssh titan systemctl status "
                     "titan-agent-api`).")
    if isinstance(body, dict) and body.get("status") == "ok":
        return Check("api", True, f"The agent API is up ({round(float(body.get('uptime_seconds') or 0) / 60)} min).",
                     facts={"uptime_seconds": body.get("uptime_seconds")})
    return Check("api", False, "The agent API answered, but not ok.", "Look at titan-agent-api's log.")


def check_ssh_and_services(run: Run = _run) -> list[Check]:
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(TIMEOUT)}", "-o", "StrictHostKeyChecking=yes",
            SSH_ALIAS, REMOTE]
    try:
        r = run(argv, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return [Check("ssh", False, "SSH to titan didn't finish.", "Try `ssh titan` by hand to see where it stops.")]
    if r.returncode == 255:
        return [Check("ssh", False, "SSH to titan was refused (key, host key or connection).",
                      "Try `ssh titan` by hand; if the host key changed, check why before accepting it.")]
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    state = lines[0] if lines else "unknown"
    failed = [u for u in lines[1:] if _UNIT_RX.match(u)][:40]
    out = [Check("ssh", True, "SSH to titan works.")]
    if state == "running" and not failed:
        out.append(Check("services", True, "titan says it's running, with no failed units."))
    else:
        step = ("Look at each failed unit's log first (`ssh titan journalctl -u <unit> -n 50`); restart only what the "
                "runbook says is safe, outside an eval window and with no deploy hold.") if failed else \
               "titan isn't fully up yet: give it a few minutes after a boot, then look again."
        out.append(Check("services", False, f"titan is {state}" + (f"; failed: {', '.join(failed)}." if failed else "."),
                         step, {"state": state, "failed": failed}))
    return out


def diagnose(*, run: Run = _run, connect: Callable[..., Any] = socket.create_connection,
             fetch: Callable[[str], Any] | None = None) -> list[Check]:
    """Down the chain; a link that's down stops the ones that need it (no SSH without the tailnet)."""
    checks = [check_tailnet(run)]
    if not checks[-1].ok:
        return checks
    checks.append(check_ports(connect))
    checks.append(check_api(fetch))
    if any(c.name == "ports" and "SSH (22)" in (c.facts.get("closed") or []) for c in checks):
        return checks
    checks += check_ssh_and_services(run)
    return checks


def report(checks: list[Check]) -> str:
    lines = [f"{'ok ' if c.ok else 'NO '} {c.name:9} {c.said}" for c in checks]
    first = next((c for c in checks if not c.ok and c.first_step), None)
    lines.append("")
    lines.append("First step: " + first.first_step if first else "Everything titan-side looks healthy from here.")
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    checks = diagnose()
    if "--json" in argv:
        print(json.dumps({"host": HOST, "checks": [asdict(c) for c in checks], "ok": all(c.ok for c in checks)}))
    else:
        print(report(checks))
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
