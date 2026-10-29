#!/usr/bin/env python3
"""Bless a box's boot after an A/B update (#407 item 1's last step; docs/box-appliance-ops.md §1).

systemd-boot counts tries on a freshly updated partition; a oneshot unit ordered after the agent API runs this, and
only exit 0 lets it call `systemd-bless-boot good`. Three un-blessed boots and the box falls back to the old partition
by itself. So this FAILS CLOSED: a check that errors, times out or can't be reached is a failure, never a pass, and a
boot that can't reach its own health check is not blessed.

The checks are the ones deploy_titan_node.sh runs after a deploy:
  health   GET /health on the agent API answers 200 (waits up to HEALTH_WAIT_S for the service to come up)
  canary   scripts/agent_canary.py --no-alert exits 0 (a real turn through the agent; CANARY_TIMEOUT_S)
  stores   every store that exists opens read-only and passes PRAGMA quick_check (a power cut mid-update, #1409)

  python3 scripts/box_bless.py          exit 0 = bless, 1 = don't; one line of check names and outcomes, nothing else

The unit (written in the doc, installed by the image, never on titan):
  [Unit]   Description=Bless this boot  After=titan-agent-api.service  Requires=boot-complete.target
  [Service] Type=oneshot  ExecStart=/usr/bin/python3 /opt/titan/scripts/box_bless.py
            ExecStartPost=/usr/lib/systemd/systemd-bless-boot good
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable
from ops_ports import AGENT_API_URL  # noqa: E402

API = os.environ.get("BOX_BLESS_API", AGENT_API_URL)
HEALTH_WAIT_S = float(os.environ.get("BOX_BLESS_HEALTH_WAIT_S", "120"))
CANARY_TIMEOUT_S = float(os.environ.get("BOX_BLESS_CANARY_TIMEOUT_S", "180"))
STORE_VARS = ("HOUSEHOLD_DB", "CONNECTORS_LEDGER", "PROACTIVE_DB", "MAIL_SERVER_DB", "LINKS_DB", "GRAPH_DB")


def health(wait_s: float = HEALTH_WAIT_S, sleep: Callable[[float], None] = time.sleep) -> bool:
    """The agent API answers /health with 200 within wait_s. Unreachable the whole time: False."""
    end = time.monotonic() + wait_s
    while True:
        try:
            with urllib.request.urlopen(f"{API}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - not up yet, refused, timed out: keep waiting, then fail
            pass
        if time.monotonic() >= end:
            return False
        sleep(2)


def canary(timeout_s: float = CANARY_TIMEOUT_S) -> bool:
    """The agent canary passes (exit 0), within timeout_s."""
    script = Path(__file__).resolve().parent / "agent_canary.py"
    try:
        return subprocess.run([sys.executable, str(script), "--no-alert"], capture_output=True,
                              timeout=timeout_s).returncode == 0
    except Exception:  # noqa: BLE001 - missing, crashed, timed out: not a pass
        return False


def store_paths() -> list[Path]:
    return [Path(os.environ[v]) for v in STORE_VARS if os.environ.get(v)]


def stores(paths: list[Path] | None = None) -> bool:
    """Every store that exists opens read-only and passes quick_check. A store that's absent is skipped (a box that
    hasn't made it yet); one that exists and can't be read fails."""
    for p in paths if paths is not None else store_paths():
        if not p.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
            try:
                if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    return False
            finally:
                con.close()
        except Exception:  # noqa: BLE001 - unreadable is a failure
            return False
    return True


CHECKS: dict[str, Callable[[], bool]] = {"health": health, "canary": canary, "stores": stores}


def bless(checks: dict[str, Callable[[], bool]] | None = None) -> tuple[bool, list[str]]:
    """Run every check in order; stop at the first failure. → (bless?, ["name ok"/"name failed", …]). A check that
    raises counts as failed (closed), and no checks at all is no bless."""
    checks = CHECKS if checks is None else checks
    if not checks:
        return False, ["no checks"]
    said = []
    for name, fn in checks.items():
        try:
            ok = fn() is True
        except Exception:  # noqa: BLE001
            ok = False
        said.append(f"{name} {'ok' if ok else 'failed'}")
        if not ok:
            return False, said
    return True, said


def main() -> int:
    ok, said = bless()
    print(("bless: " if ok else "don't bless: ") + ", ".join(said))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
