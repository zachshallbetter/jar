#!/usr/bin/env python3
# gate: hermetic
""""All fixed except X" never changes X (#952; 10-04: titan completed the one reminder Zach had excluded). The
person's excluded phrases are read from their latest message in code; a tool that changes or removes an existing item
whose target names X is refused with "Not run". Notes, new items and reads that mention X still run. Fictional names.
Run: python3 scripts/test_exclusions.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-exclusions-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "AGENT_MEMORY_DIR": "memory",
             "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json", "DAILY_LOG_DIR": "daily",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import exclusions as E  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── what the person set aside ──")
for msg, want in (("I think all of those have been fixed except for lakeside", ["lakeside"]),
                  ("Mark everything done but the Harbor Gas bill, it's still overdue", ["the harbor gas bill"]),
                  ("Delete all of them other than Robin's", ["robin's"]),
                  ("Done, except the dentist one and the car", ["the dentist one", "the car"]),
                  ("Everything except the Amazon orders is paid", ["the amazon orders"]),
                  ("Can you check the emails, excluding Lakeside", ["lakeside"]),
                  ("except I forgot to say thanks", []), ("All good but that's fine", []),
                  ("but I think so", []), ("Mark them all done", [])):
    check(f"{msg!r}", E.excluded(msg), want)

print("── which targets that names ──")
check("every content word of the phrase", E.hit(["the harbor gas bill"], "Pay Harbor Gas bill ($61.20)"), "the harbor gas bill")
check("…not just one of them", E.hit(["the harbor gas bill"], "Pay Lakeside Clinic bill"), None)
check("a plural or a possessive still names it", (E.hit(["the amazon order"], "Amazon orders declined"),
      E.hit(["robin's"], "Robin's packing list")), ("the amazon order", "robin's"))
check("a word inside another word doesn't", E.hit(["car"], "Card payment failed"), None)

print("── the write is refused; reads, notes and the others run ──")
import agent_runtime_lib as lib  # noqa: E402
ran = []
real_exec, real_node = lib._execute_tool, lib._get_node
lib._execute_tool = lambda name, args: ran.append((name, dict(args))) or f"Done: {name}"
lib._get_node = lambda rid: {"node_id": rid, "data": {"title": "An auto payment failed", "vendor": "Lakeside Clinic"}} \
    if rid == "bill-mail-aa" else {"node_id": rid, "data": {"title": "Your Harbor Gas bill", "vendor": "Harbor Gas"}}
try:
    lib.begin_turn_info()
    lib._TURN_IO.excluded, lib._TURN_IO.tools_used = E.excluded("all of those are fixed except for lakeside"), set()
    lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls = [], {}
    out = lib.execute_tool("complete_reminder", {"title": "Pay Lakeside Clinic bill ($38.85 failed)"})
    check("completing the excluded reminder: not run, and said why", (out.startswith("Not run: they set this aside"),
          "“lakeside”" in out, ran), (True, True, []))
    out = lib.execute_tool("update_record", {"record_id": "bill-mail-aa", "updates": "payment_status=paid"})
    check("updating the excluded record (named by its vendor): not run", (out.startswith("Not run"), ran), (True, []))
    check("…its tool_fails head never carries the name", lib._TURN_INFO.info["tool_fails"][-1][1].count("lakeside"), 0)
    out = lib.execute_tool("complete_reminder", {"title": "Pay Harbor Gas bill"})
    out2 = lib.execute_tool("update_record", {"record_id": "bill-mail-bb", "updates": "payment_status=paid"})
    check("the others still change", [n for n, _ in ran], ["complete_reminder", "update_record"])
    ran.clear()
    lib.execute_tool("log_event", {"description": "Left the Lakeside Clinic bill open"})
    lib.execute_tool("find_records", {"type": "bill", "field": "vendor"})
    lib.execute_tool("create_reminder", {"title": "Call Lakeside Clinic about the failed payment"})
    check("a note, a read and a new reminder that mention it run", [n for n, _ in ran],
          ["log_event", "find_records", "create_reminder"])
    ran.clear()
    # Coordination 10-05: a change named only by an id (a candidate line's) has no title to match.
    lib._execute_tool = lambda name, args: (ran.append((name, dict(args))) or
        ("Incomplete reminders:\n• Pay Harbor Gas bill [id: R-1001]\n• Pay Lakeside Clinic bill [id: R-2002]"
         if name == "list_reminders" else f"Done: {name}"))
    lib._TURN_IO.listed = None
    lib.execute_tool("list_reminders", {})
    out = lib.execute_tool("complete_reminder", {"reminder_id": "R-2002"})
    check("an id whose line in this turn's listing names the excluded item: not run",
          (out.startswith("Not run: they set this aside"), [n for n, _ in ran]), (True, ["list_reminders"]))
    lib.execute_tool("complete_reminder", {"reminder_id": "R-1001"})
    check("…another id on the same listing still runs", [a for n, a in ran if n == "complete_reminder"],
          [{"reminder_id": "R-1001"}])
    out = lib.execute_tool("delete_reminder", {"reminder_id": "R-9999"})
    check("an id no listing explains, with something excluded: not run, asks for the title",
          (out.startswith("Not run: they set something aside"), "title" in out), (True, True))
    ran.clear()
    lib._TURN_IO.excluded = None
    lib.execute_tool("complete_reminder", {"title": "Pay Lakeside Clinic bill ($38.85 failed)"})
    check("nothing excluded: it runs", [n for n, _ in ran], ["complete_reminder"])
finally:
    lib._execute_tool, lib._get_node = real_exec, real_node
    lib._TURN_IO.excluded = lib._TURN_IO.tools_used = lib._TURN_IO.outcomes = lib._TURN_IO.failed_calls = None
    lib._TURN_IO.listed = None

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
