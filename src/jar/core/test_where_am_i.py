#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""where_am_i (jellybean#387; Zach, 10-02: "an on demand item… check where I am"): the asker's own iPhone, an address
and how precise, now. Offered only on a phone turn whose app reports it; never on a Mac turn or routed to a Mac; every
person's own, not a shared device's; never stored: no facts, nothing in the KB, not on the timeline, and the kept
conversation has a placeholder instead of the address. "Where am I" prefers it over Find My.
Run: python3 scripts/test_where_am_i.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-whereami-"))
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
T = "where_am_i"


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── who's offered it ──")
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
check("every person's own; not a shared household device",
      [hh.role_allows_tool(r, T) for r in ("admin", "adult", "kid", "household")], [True, True, True, False])
check("a read, with no parameters", (lib._DEVICE_TOOLS[T]["risk"], lib._DEVICE_TOOLS[T]["parameters"]), ("read", {}))
d = lib._DEVICE_TOOLS[T]["description"]
check("it says it's for the asker, not Find My, and isn't saved",
      ("'where am I?'" in d, "not Find My" in d, "isn't saved" in d), (True, True, True))
check("Find My points the asker's own location at it", "where_am_i is the one to use" in
      lib._DEVICE_TOOLS["find_my_locate"]["description"], True)
check("step labels", (bool(lib._TOOL_STATUS.get(T)), T in lib._TOOL_PLAIN), (True, True))
check("sensitive: no facts, nothing to the KB", T in lib.SENSITIVE_TOOLS, True)

print("── the address is never kept ──")
ADDR = "1234 W Main Ave, Spokane, WA 99201"
lib.begin_turn_info()
lib._note_where(f"You're at {ADDR} (within 15 m, 2:41 PM).")
check("the turn notes the address it was given (in memory, this turn)", lib._TURN_INFO.info.get("_where_said"), [ADDR])
check("…and the turn log never gets it", "_where_said" in Path(lib.__file__).read_text().split("def _log_turn")[1][:3000], False)
check("the kept reply has a placeholder, for the full address and the street line alone",
      lib.without_location(f"You're at {ADDR}, about 15 m. 1234 W Main Ave is near the park.", [ADDR]),
      "You're at [your location at the time], about 15 m. [your location at the time] is near the park.")
check("no fix: nothing noted", (lib.begin_turn_info(), lib._note_where("Couldn't get a location: no signal."),
                                lib._TURN_INFO.info.get("_where_said"))[2], None)


class FakeMem:
    def __init__(self):
        self.msgs, self.extracted = [], 0

    def add_message(self, conv, role, text, **where):
        self.msgs.append((conv, role, text))
        return None

    def maybe_extract_facts(self, conv, person_id=None, on_facts=None):
        self.extracted += 1


mem = FakeMem()
api._get_memory = lambda: mem
api._MEMORY_AVAILABLE = True
zach = hh.owner_identity()
saved_run, saved_build, saved_info, saved_fact = (api._lib._run_agent_turn, api._build_messages, api._lib.last_turn_info,
                                                  api._lib._maybe_save_fact)
facts_saved = []
api._lib._maybe_save_fact = lambda q, t: facts_saved.append(q)


def turn(messages):
    lib.begin_turn_info()
    lib._TURN_INFO.info["tool_names"] = [T]
    lib._note_where(f"You're at {ADDR} (within 15 m, 2:41 PM).")
    return f"You're at {ADDR} (within about 15 m)."


api._lib._run_agent_turn = turn
api._build_messages = lambda *a, **k: ([{"role": "user", "content": "q"}], 0)
api._turns["t-where"] = {"source": None, "remember": True, "identity": zach}
reply = api._run_turn("t-where", "Where am I?", [], True, None)
check("the person sees the address", ADDR in reply, True)
check("the kept conversation doesn't", (mem.msgs[-1][2], ADDR in str(mem.msgs)),
      ("You're at [your location at the time] (within about 15 m).", False))
check("…no facts extracted, nothing saved to the KB", (mem.extracted, facts_saved), (0, []))
check("…and it's gone from the turn record", "_where_said" in api._turns["t-where"], False)
api._lib._run_agent_turn, api._build_messages, api._lib.last_turn_info, api._lib._maybe_save_fact = (
    saved_run, saved_build, saved_info, saved_fact)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
