#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""photos_find (jellybean#188): the Photos library as counts and dates only, offered on a person's own iPhone turn,
never on a Mac or a shared household device, never routed to the Mac. Run: python3 scripts/test_photos_find.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-photosfind-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ["MEMORY_HYBRID"] = "0"
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))




T = "photos_find"
phone_now = lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS))
older = lib.device_tools_for_client("iphone", sorted(lib.PHONE_DEVICE_TOOLS - {T}))
check("phone only", T in lib.PHONE_ONLY_TOOLS, True)
check("a phone that reports it gets it; an older one doesn't", (T in phone_now, T in older), (True, False))
check("never routed to the Mac", T in lib.mac_tools_for_phone(older), False)
io = lib._TURN_IO
io.client, io.device_tools, io.remote, io.role, io.device = None, None, None, None, object()
check("not on a Mac turn", T in lib._turn_device_tools(), False)
io.client, io.device_tools = "iphone", phone_now
check("…but on a phone turn that reports it", T in lib._turn_device_tools(), True)
io.client = io.device_tools = io.device = None
check("every person's own library; not a shared household device",
      [hh.role_allows_tool(r, T) for r in ("admin", "adult", "kid", "household")], [True, True, True, False])
spec = lib._DEVICE_TOOLS[T]
check("a read, with album, from, to and place", (spec["risk"], sorted(spec["parameters"])), ("read", ["album", "from", "place", "to"]))
check("it says no photo is ever seen or sent", "never returns, describes or sends a photo" in spec["description"], True)
import turn_eval as te  # noqa: E402
check("an eval never reads a real library", te.fake_mac(T, {}), "Albums: Lake trip (48), Soccer (120).")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
