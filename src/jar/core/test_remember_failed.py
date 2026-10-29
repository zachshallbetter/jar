#!/usr/bin/env python3
"""A failed turn still reaches the chat history (agent_api_server._remember_turn). Hermetic.
Run: python3 scripts/test_remember_failed.py"""
import os, sys, tempfile
from pathlib import Path
_TMP = Path(tempfile.mkdtemp(prefix="titan-remember-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "ROUTINES_DB": "routines.db",
             "PROACTIVE_DB": "proactive.db", "CONNECTORS_LEDGER": "ledger.db", "REFLECTION_DB": "reflection.db",
             "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json", "AGENT_TURN_LOG": "turns.jsonl",
             "TODOS_DB": "todos.db", "KB_ACL_DB": "kb_acl.db", "PLACES_DB": "places.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["HA_TOKEN_FILE"] = str(_TMP / "no-ha")
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

passed = failed = 0
def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")

class FakeMem:
    def __init__(self): self.msgs, self.extracted = [], 0
    def add_message(self, conv, role, text, **where): self.msgs.append((conv, role, text))
    def maybe_extract_facts(self, conv, person_id=None, on_facts=None): self.extracted += 1

mem = FakeMem()
api._get_memory = lambda: mem
api._MEMORY_AVAILABLE = True
zach = hh.owner_identity()
api._turns["t-ok"] = {"source": None, "remember": True}
api._remember_turn("t-ok", zach, "hi", "hello", extract=True)
check("a normal turn is recorded and facts extracted", (mem.msgs[-2:], mem.extracted), ([("chat", "user", "hi"), ("chat", "assistant", "hello")], 1))

def boom(messages):
    raise BrokenPipeError("[Errno 32] Broken pipe")
api._lib._run_agent_turn, saved = boom, api._lib._run_agent_turn
api._build_messages, saved_b = (lambda *a, **k: ([{"role": "user", "content": "q"}], 0)), api._build_messages
api._turns["t-fail"] = {"source": None, "remember": True, "identity": zach}
try:
    api._run_turn("t-fail", "Reminders in the zip?", [], True, None)
except BrokenPipeError:
    pass
check("a failed turn still reaches the history, with what broke",
      (mem.msgs[-2][2], mem.msgs[-1][2].startswith("(This reply didn't go through: I lost the connection to the model")), ("Reminders in the zip?", True))
check("…and the turn is marked error", api._turns["t-fail"]["state"], "error")
n = len(mem.msgs)
api._turns["t-eval"] = {"source": "eval", "remember": True, "identity": zach}
try:
    api._run_turn("t-eval", "x", [], True, None)
except BrokenPipeError:
    pass
check("an eval turn is never recorded", len(mem.msgs), n)
api._turns["t-nomem"] = {"source": None, "remember": False}
api._remember_turn("t-nomem", zach, "x", "y")
check("remember: false records nothing", len(mem.msgs), n)
hhid = hh.Identity({"id": "household", "name": "Kitchen", "role": "household"}, None, "household", "device")
api._turns["t-hh"] = {"source": None, "remember": True}
api._remember_turn("t-hh", hhid, "x", "y")
check("a household device records nothing", len(mem.msgs), n)
api._lib._run_agent_turn, api._build_messages = saved, saved_b

saved_fact = api._lib._maybe_save_fact
facts_saved = []
api._lib._maybe_save_fact = lambda q, t, **k: facts_saved.append(q)
api._lib._run_agent_turn = lambda messages: "You slept 7 h 10 min last night."
api._build_messages = lambda *a, **k: ([{"role": "user", "content": "q"}], 0)
api._lib.last_turn_info = (lambda f: (lambda: {**f(), "tool_names": ["health_summary"]}))(api._lib.last_turn_info)
api._turns["t-health"] = {"source": None, "remember": True, "identity": zach}
before = mem.extracted
api._run_turn("t-health", "How did I sleep?", [], True, None)
check("a health turn: kept in the person's own conversation", mem.msgs[-1][2], "You slept 7 h 10 min last night.")
check("…but no facts extracted and nothing saved to the KB", (mem.extracted - before, facts_saved), (0, []))
check("the prompt keeps health private", "Health is private" in api._lib.AGENT_IDENTITY, True)
api._lib._maybe_save_fact = saved_fact
api._lib._run_agent_turn, api._build_messages = saved, saved_b
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
