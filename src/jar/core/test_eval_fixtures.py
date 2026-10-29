#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Eval turns' fixtures and simulated writes (10-05): `eval_fixtures` answers a titan tool with fictional data on an eval
turn only, never on a person's; the 10-05 write tools (mark_bills_paid, the mail moves) are simulated on an eval turn,
so the nightly never writes the household's records.
Run: python3 scripts/test_eval_fixtures.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-evalfx-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl",
             "MAIL_BILLS_DB": "bills.db", "PROACTIVE_DB": "proactive.db", "KB_ACL_DB": "kb_acl.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
sys.path.insert(0, str(Path(__file__).parent))

import agent_api_server as api  # noqa: E402
import agent_runtime_lib as lib  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


check("fixtures only on an eval turn's request", (api._parse_eval_fixtures({"eval_fixtures": {"threads": "x"}}, "eval"),
                                                  api._parse_eval_fixtures({"eval_fixtures": {"threads": "x"}}, "user")),
      ({"threads": "x"}, None))
check("…strings only, capped", api._parse_eval_fixtures({"eval_fixtures": {"a": 1, "b": "y" * 5000}}, "eval"),
      {"b": "y" * 4000})
lib._TURN_IO.role = "admin"
lib._TURN_IO.source = "eval"
lib._TURN_IO.eval_fixtures = {"open_obligations": "FIXTURE: Quarry Water due Oct 8"}
check("an eval turn: the tool answers with the fixture", lib._execute_tool("open_obligations", {}),
      "FIXTURE: Quarry Water due Oct 8")
lib._TURN_IO.source = "user"
lib._TURN_IO.role = "admin"
real = lib.record_nodes
lib.record_nodes = lambda t: ([], True)
try:
    out = lib._execute_tool("open_obligations", {})
finally:
    lib.record_nodes = real
check("a person's turn never sees a fixture", "FIXTURE" in out, False)
lib._TURN_IO.source = "eval"
for tool in ("mark_bills_paid", "mail_move_preview", "mail_move", "mail_move_undo"):
    check(f"{tool} is simulated on an eval turn", tool in lib._EVAL_SIMULATED, True)
os.environ["MAIL_SERVER_WRITES"] = "1"
out = lib._execute_tool("mark_bills_paid", {"all": "true"})
import bill_due  # noqa: E402
check("…and an eval turn's mark_bills_paid writes nothing", (out.startswith("Done (eval: not sent)"),
                                                             bill_due.statements("zach") + bill_due.statements("admin")),
      (True, []))
lib._TURN_IO.source = lib._TURN_IO.eval_fixtures = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
