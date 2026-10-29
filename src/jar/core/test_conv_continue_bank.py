#!/usr/bin/env python3
# gate: hermetic
"""The conversation carry-over eval bank (titan-node#1016 step 5): fictional follow-ups ("she never got it", "do the
same for Sunday") that only make sense with the previous chat, answered with and without CONV_CONTINUE's block. Test
data only. This checks the bank's shape: unique ids, 15-20 cases, a previous chat and both arms per case, controls
that don't continue, privacy cases whose earlier chat is someone else's and never carries, and fictional names only
(every capitalised word is on an allowlist, so a real name can't slip in).
Run: python3 scripts/test_conv_continue_bank.py
"""
import json
import re
import sys
from pathlib import Path

BANK = Path(__file__).parent / "evalsets" / "conv_continue.jsonl"
passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


rows = [json.loads(line) for line in BANK.read_text().splitlines() if line.strip()]
ids = [r["id"] for r in rows]
check("15-20 cases", 15 <= len(rows) <= 20, True)
check("unique ids", len(ids) == len(set(ids)), True)
check("each has a previous chat (owner, gap, name, messages) and a follow-up",
      [r["id"] for r in rows if not (r.get("ask") and r["prior"].get("owner") in ("self", "other", "kid")
                                     and isinstance(r["prior"].get("gap_min"), int) and r["prior"].get("name")
                                     and r["prior"].get("messages"))], [])
check("each says whether it continues, and both arms",
      [r["id"] for r in rows if not (isinstance(r["truth"].get("continues"), bool)
                                     and isinstance(r["truth"].get("with"), dict) and isinstance(r["truth"].get("without"), dict))], [])
KEYS = {"expect_all", "expect_any", "expect_any_also", "expect_tools_any", "forbid", "forbid_tools"}
check("arms use only the known check keys",
      sorted({k for r in rows for arm in ("with", "without") for k in r["truth"][arm]} - KEYS), [])
cont = [r for r in rows if r["truth"]["continues"]]
ctrl = [r for r in rows if not r["truth"]["continues"]]
check("most cases continue; at least 3 controls don't", (len(cont) >= 10, len(ctrl) >= 3), (True, True))
check("a continuing case says what it resolves", [r["id"] for r in cont if not r["truth"].get("resolves")], [])
check("a control's two arms expect the same answer (the block must not change it)",
      [r["id"] for r in ctrl if r["prior"]["owner"] == "self"
       and r["truth"]["with"].get("expect_any") != r["truth"]["without"].get("expect_any")], [])
priv = [r for r in rows if r["prior"]["owner"] != "self"]
check("someone else's earlier chat never continues, and both arms forbid its content",
      [r["id"] for r in priv if r["truth"]["continues"] or not r["truth"]["with"].get("forbid")
       or r["truth"]["with"].get("forbid") != r["truth"]["without"].get("forbid")], [])
check("a kid's chat is among them (never surfaces to a parent)", any(r["prior"]["owner"] == "kid" for r in priv), True)
ALLOWED = set("""Actually Add Added And Asked Ava Can Cobalt Created Did Do Done Fern Friday Gift Grocery Hall Hardware
Help Homework Jordan Leo Lumen Make No Order Over Packing Pike Porch Power Put Quarry Remind Road Robin Saturday Sent Set
She Slate Street Sunday Tell Text The Thursday Trade Tuesday Turn Valley What When Wireless Your Alex""".split())
words = {w for r in rows for w in re.findall(r"\b[A-Z][a-z]+\b", json.dumps(r, ensure_ascii=False))}
check("fictional names only: every capitalised word is on the allowlist", sorted(words - ALLOWED), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
