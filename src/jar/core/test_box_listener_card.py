#!/usr/bin/env python3
# gate: hermetic
"""device_health's listener card on a box (titan-node#407): with BOX_SETUP=1 it reads `ss` through box_firewall and cards
the admin when something listens beyond loopback that the allowlist doesn't expect, by port and process name only (no
command line, no address detail beyond the port); confirmed over two runs like every health problem, cleared with a
card when it stops. On titan (no BOX_SETUP) it never runs ss at all. Throwaway state; a fake ss.
Run: python3 scripts/test_box_listener_card.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-listener-card-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "DEVICE_HEALTH_STATE": "dh.json", "PROACTIVE_DB": "proactive.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("BOX_SETUP", None)
sys.path.insert(0, str(Path(__file__).parent))
import box_firewall as fw  # noqa: E402
import device_health as dh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


SS_OK = """tcp LISTEN 0 4096 127.0.0.1:8010 0.0.0.0:* users:(("llama-server",pid=11,fd=3))
tcp LISTEN 0 2048 0.0.0.0:7770 0.0.0.0:* users:(("python3",pid=12,fd=5))
udp UNCONN 0 0 0.0.0.0:5353 0.0.0.0:* users:(("python3",pid=12,fd=9))"""
STRAY = 'tcp LISTEN 0 128 0.0.0.0:4444 0.0.0.0:* users:(("nc",pid=99,fd=3))'
ADMIN = "zach"

print("── box_firewall.unexpected: port and process name ──")
u = fw.unexpected(fw.listeners(SS_OK + "\n" + STRAY))
check("the stray, with its process name", u, [{"proto": "tcp", "host": "0.0.0.0", "port": 4444, "name": "nc"}])
check("a clean box: nothing", fw.unexpected(fw.listeners(SS_OK)), [])

print("── the finding ──")
base = dict(people=[], macs=lambda pid: [], live=lambda a, b: False, sync={}, home=None, services={}, admin_id=ADMIN, now=0.0)
check("titan (listeners None): no listener finding at all",
      [f.key for f in dh.findings(**base, listeners=None) if "listeners" in f.key], [])
ok = [f for f in dh.findings(**base, listeners=[]) if "listeners" in f.key]
check("a clean box: one finding, not a problem", [(f.key, f.problem) for f in ok], [(f"health-{ADMIN}-listeners", False)])
bad = [f for f in dh.findings(**base, listeners=u) if "listeners" in f.key][0]
check("a stray: a problem, to the admin only, naming port and process", (bad.problem, bad.to, bad.title),
      (True, [ADMIN], "Something on the box is listening on port 4444 (nc) that shouldn't be"))
check("…never an address or a command line in the card", ("0.0.0.0" in bad.title + bad.detail, "pid" in bad.title + bad.detail),
      (False, False))

print("── carded like every health problem: two runs to confirm, a card when it clears ──")
sent = []


def send(title, detail, **kw):
    sent.append((title, kw.get("to"), kw.get("key")))


state: dict = {}
dh.reconcile(dh.findings(**base, listeners=u), state, 1000.0, False, send)
check("first run: not yet (no flapping)", sent, [])
dh.reconcile(dh.findings(**base, listeners=u), state, 1900.0, False, send)
check("second run: the admin's card", [(t[:40], to) for t, to, _ in sent], [("Something on the box is listening on por", [ADMIN])])
sent.clear()
dh.reconcile(dh.findings(**base, listeners=[]), state, 2800.0, False, send)
check("it stops: a clearing card", [t for t, _, _ in sent], ["The box listens only where it should again"])

print("── gather: only on a box ──")
calls = []
_run = subprocess.run


def fake_run(cmd, *a, **k):
    if cmd and cmd[0] == "ss":
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, SS_OK + "\n" + STRAY, "")
    return _run(cmd, *a, **k)


subprocess.run = fake_run
import capabilities as cap  # noqa: E402
cap.home_part = lambda *a, **k: None
cap.service_urls = lambda: {}
sig = dh.gather(0.0)
check("titan: ss never run, listeners None", (calls, sig["listeners"]), ([], None))
os.environ["BOX_SETUP"] = "1"
sig = dh.gather(0.0)
check("a box: ss read once, the stray found", (len(calls), [d["port"] for d in sig["listeners"]]), (1, [4444]))
os.environ.pop("BOX_SETUP", None)
subprocess.run = _run
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
