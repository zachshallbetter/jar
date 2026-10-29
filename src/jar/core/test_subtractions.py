#!/usr/bin/env python3
# gate: hermetic
"""What titan no longer carries stays gone (#1624, #1626). The only workspace driver is worktree: e2b or cube is a 400
and nothing is created. The connectors don't take a "datasets" source, since nothing produced one. No E2B import or
sandbox state is left in the API server. Temporary stores; no network. Run: python3 scripts/test_subtractions.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-subtractions-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "CONNECTORS_LEDGER": "ledger.db",
             "SPACES_DB": "spaces.db", "KB_ACL_DB": "kb_acl.db", "PROACTIVE_DB": "proactive.db",
             "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import connectors_runner as CR  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.200} expected={expected!r:.200}"))


check("no 'datasets' source (#1626)", "datasets" in CR.SOURCES, False)
src = (HERE / "agent_api_server.py").read_text()
check("no E2B import or sandbox state left (#1624)", [w for w in ("from e2b", "_E2B_AVAILABLE", "_e2b_sandboxes", "_is_e2b_like",
                                                                  "_create_cube_workspace") if w in src], [])

from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
c = TestClient(api.app)
for driver in ("e2b", "cube", "anything"):
    r = c.post("/v1/workspaces", json={"driver": driver, "id": f"ws{driver}"}, headers={"X-Agent-Token": MASTER})
    check(f"driver {driver!r}: 400, nothing created", (r.status_code, f"ws{driver}" in api._workspaces), (400, False))
check("/health no longer reports e2b", "e2b_available" in c.get("/health").json(), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
