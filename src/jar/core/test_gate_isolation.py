#!/usr/bin/env python3
# gate: hermetic
"""Every store a script defaults to under /ai/ must be redirected in the deploy gate's export block, so the gate's
tests never write titan's real files. 10-03: sitter (#797), handoff (#791) and TV unlock (#789) added stores without
adding them there, so each gate run on titan wrote the live sitter.db, and off titan the gate failed. Read-only
inputs are listed in READ_ONLY with the reason.

Run: python3 scripts/test_gate_isolation.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Inputs the tests may read but nothing writes during a gate run.
READ_ONLY = {
    "PRIVACY_REDTEAM_BANK",   # privacy_eval.py's question bank, read only
    "AGENT_API_TOKEN_FILE",   # the API token, read only
    "AGENT_DEPLOYED_CHECKOUTS",   # which checkouts are deployed, read only
    "AGENT_ROOT",   # the agent's code root, read only
    "KOKORO_MODEL",    # kokoro_server.py's model file, read only (the server isn't started by any test)
    "KOKORO_VOICES",   # kokoro_server.py's voice pack, read only
    "ROOMS_FILE",      # room_state.py's per-room quiet hours (/ai/config/rooms.yaml), read only; missing = defaults
}

deploy = (HERE / "deploy_titan_node.sh").read_text()
m = re.search(r"^export DAILY_LOG_DIR=.*?[^\\]\n", deploy, re.S | re.M)
block = m.group(0) if m else ""
redirected = set(re.findall(r"\b([A-Z][A-Z0-9_]+)=", block))

defaults: dict[str, str] = {}
for f in sorted(HERE.glob("*.py")):
    if f.name.startswith("test_"):
        continue
    for var in re.findall(r"""(?:environ\.get|getenv)\(\s*["']([A-Z][A-Z0-9_]+)["']\s*,\s*["']/ai/""", f.read_text()):
        defaults.setdefault(var, f.name)

missing = sorted(v for v in defaults if v not in redirected and v not in READ_ONLY)
fails = 0


def check(label, ok, detail=""):
    global fails
    fails += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")


check("found the deploy's export block", bool(block) and "TEST_STATE" in block)
check("found the scripts' /ai/ defaults (over 20)", len(defaults) > 20, str(len(defaults)))
check("every /ai/ default is redirected in the gate (or listed read-only)", not missing,
      ", ".join(f"{v} ({defaults[v]})" for v in missing))
check("the sitter store is redirected (10-03)", "SITTER_DB" in redirected)
check("the turn log is redirected", "AGENT_TURN_LOG" in redirected)

print(f"\n{5 - fails} passed, {fails} failed")
sys.exit(1 if fails else 0)
