#!/usr/bin/env python3
# gate: hermetic
"""What a turn teaches titan about the house comes only from a person's own turns (#366, #372, #359): an eval, a
canary or a test turn records no mention, no gap and no alias, whether its source came from the API (per turn) or
the process (AGENT_TURN_SOURCE, how the CLI evals run). 10-03: 183 of 216 live mentions came from those.
Run: python3 scripts/test_real_turn.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-realturn-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "WORLD_DIR": "world", "WORLD_DB": "world.db",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily",
             "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_WORLD_MODEL"] = "1"
os.environ.pop("AGENT_TURN_SOURCE", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_runtime_lib as lib  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


print("── real_turn ──")
cases = []
for api, env in ((None, None), ("user", None), ("eval", None), ("canary", None), ("test", None),
                 (None, "eval"), (None, "canary"), (None, "test")):
    lib._TURN_IO.source = api
    if env:
        os.environ["AGENT_TURN_SOURCE"] = env
    else:
        os.environ.pop("AGENT_TURN_SOURCE", None)
    cases.append(lib.real_turn())
os.environ.pop("AGENT_TURN_SOURCE", None)
check("a person's turn only: not eval/canary/test, by the API's source or the process's",
      cases, [True, True, False, False, False, False, False, False])

print("── the world block's gaps (mentions, #372 gaps) follow it ──")
seen = []
saved = (W.context, W.get, W.maybe_sync)
W.context = lambda w, q, pid, role, **kw: seen.append(kw.get("gaps")) or ("", [])
W.get = lambda: object()
W.maybe_sync = lambda *a, **k: None
lib._TURN_IO.role, lib._TURN_IO.person = "admin", {"id": "zach", "name": "Zach"}
try:
    for api, env in ((None, None), (None, "eval"), ("canary", None), ("eval", None)):
        lib._TURN_IO.source = api
        if env:
            os.environ["AGENT_TURN_SOURCE"] = env
        else:
            os.environ.pop("AGENT_TURN_SOURCE", None)
        lib._situational_context_inner("What's the vet's number?")
finally:
    W.context, W.get, W.maybe_sync = saved
    os.environ.pop("AGENT_TURN_SOURCE", None)
    lib._TURN_IO.source = None
check("a person's turn records; a CLI eval, the canary and an API eval don't", seen, [True, False, False, False])

print("── aliases (#359) follow it too ──")
learned = []
W.get = lambda: learned.append("world") or (_ for _ in ()).throw(RuntimeError("stop"))
try:
    os.environ["AGENT_TURN_SOURCE"] = "eval"
    lib._learn_from("the vet", "zach", "admin")
    os.environ.pop("AGENT_TURN_SOURCE", None)
    lib._TURN_IO.source = "canary"
    lib._learn_from("the vet", "zach", "admin")
    check("an eval or canary turn never reaches the world store to learn", learned, [])
    lib._TURN_IO.source = None
    try:
        lib._learn_from("the vet", "zach", "admin")
    except Exception:  # noqa: BLE001
        pass
    check("a person's turn does", learned, ["world"])
finally:
    W.get = saved[1]
    os.environ.pop("AGENT_TURN_SOURCE", None)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
