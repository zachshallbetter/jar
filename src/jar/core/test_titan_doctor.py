#!/usr/bin/env python3
# gate: hermetic
"""titan diagnosed from the Mac (#1632, titan_doctor.py). It works down the chain (tailnet, ports, health, SSH, units)
and stops where a link is down. Each failure says the runbook's first step. What runs on titan is the fixed text only,
and comes back as unit names, never arguments. Nothing is changed anywhere. Every subprocess, socket and fetch is a
spy. Run: python3 scripts/test_titan_doctor.py"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import titan_doctor as D  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def tailscale(state="Running", online=True, present=True):
    peers = {"x": {"DNSName": D.HOST + ".", "HostName": D.HOST.split(".")[0], "Online": online,
                   "LastSeen": "2026-10-15T04:00:00Z"}} if present else {}
    return json.dumps({"BackendState": state, "Peer": peers})


class Run:
    def __init__(self, ts=None, ssh_rc=0, ssh_out="running\n"):
        self.ts = ts if ts is not None else tailscale()
        self.ssh_rc, self.ssh_out = ssh_rc, ssh_out
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=15):
        self.calls.append(list(argv))
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(argv, self.ssh_rc, self.ssh_out, "")
        return subprocess.CompletedProcess(argv, 0, self.ts, "")


class Sock:
    def close(self):
        pass


def ports(closed=()):
    def connect(addr, timeout=None):
        if addr[1] in closed:
            raise OSError("refused")
        return Sock()
    return connect


healthy = lambda url: {"status": "ok", "uptime_seconds": 3600}   # noqa: E731

print("── healthy ──")
run = Run()
checks = D.diagnose(run=run, connect=ports(), fetch=healthy)
check("every link ok", [(c.name, c.ok) for c in checks],
      [("tailnet", True), ("ports", True), ("api", True), ("ssh", True), ("services", True)])
ssh = next(c for c in run.calls if c[0] == "ssh")
check("SSH is key-only, no prompt, the host key checked, and runs the fixed text only",
      (ssh[1:7], ssh[-1]), (["-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(D.TIMEOUT)}", "-o", "StrictHostKeyChecking=yes"],
                            D.REMOTE))
check("the remote text reads states and unit names only", ("systemctl is-system-running" in D.REMOTE,
      any(w in D.REMOTE for w in ("cat ", "env", "ps ", "journalctl", "restart", "start ", "kill"))), (True, False))
check("the report says so", D.report(checks).endswith("Everything titan-side looks healthy from here."), True)

print("── down the chain ──")
for label, ts, said in (("Tailscale off on the Mac", tailscale(state="Stopped"), "Connect Tailscale"),
                        ("titan offline", tailscale(online=False), "power"),
                        ("titan not in the tailnet", tailscale(present=False), "Sign this Mac")):
    run = Run(ts=ts)
    checks = D.diagnose(run=run, connect=ports(), fetch=healthy)
    check(f"{label}: stops at the tailnet, with the first step", ([c.name for c in checks], said in checks[0].first_step,
          any(c[0] == "ssh" for c in run.calls)), (["tailnet"], True, False))
checks = D.diagnose(run=Run(), connect=ports(closed=(22, D.AGENT_API_PORT)), fetch=lambda u: (_ for _ in ()).throw(OSError()))
check("neither port answers: the firewall or titan's network, and no SSH tried",
      ([c.name for c in checks], "firewall" in checks[1].first_step), (["tailnet", "ports", "api"], True))
checks = D.diagnose(run=Run(), connect=ports(closed=(D.AGENT_API_PORT,)), fetch=lambda u: (_ for _ in ()).throw(OSError()))
check("only the API is down: look at titan-agent-api, and SSH still runs",
      (checks[1].ok, "titan-agent-api" in checks[1].first_step, [c.name for c in checks][-2:]), (False, True, ["ssh", "services"]))
checks = D.diagnose(run=Run(), connect=ports(), fetch=lambda u: {"status": "degraded"})
check("the API answers but not ok", (checks[2].ok, "log" in checks[2].first_step), (False, True))
checks = D.diagnose(run=Run(ssh_rc=255, ssh_out=""), connect=ports(), fetch=healthy)
check("SSH refused: try it by hand, check a changed host key before accepting it",
      (checks[-1].name, checks[-1].ok, "host key" in checks[-1].first_step), ("ssh", False, True))
checks = D.diagnose(run=Run(ssh_out="degraded\ntitan-reranker.service\ntitan-embeddings.service\n"), connect=ports(), fetch=healthy)
svc = checks[-1]
check("failed units named, with the runbook's first step (logs first, windows, holds)",
      (svc.ok, svc.facts["failed"], "eval window" in svc.first_step), (False, ["titan-reranker.service", "titan-embeddings.service"], True))
checks = D.diagnose(run=Run(ssh_out="degraded\nGEMINI_API_KEY=abc xyz\n" + "x" * 200 + "\n"), connect=ports(), fetch=healthy)
check("anything that isn't a unit name is dropped (no value, nothing long)", checks[-1].facts["failed"], [])
checks = D.diagnose(run=Run(ssh_out="starting\n"), connect=ports(), fetch=healthy)
check("still starting after a boot: wait, then look again", ("few minutes" in checks[-1].first_step), True)

print("── the report and JSON ──")
checks = D.diagnose(run=Run(ts=tailscale(online=False)), connect=ports(), fetch=healthy)
rep = D.report(checks)
check("the report: one line per check, then the first step", (rep.splitlines()[0].startswith("NO  tailnet"),
      rep.splitlines()[-1].startswith("First step: ")), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
