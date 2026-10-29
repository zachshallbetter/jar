#!/usr/bin/env python3
# gate: hermetic
"""#363 (decided 10-02): public facts are allowed for organisations, labelled "general knowledge, not on record",
and never about people.

Run: python3 scripts/test_public_facts.py
"""
import os
import sys
import tempfile
from datetime import date

os.environ["WORLD_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import world as W  # noqa: E402
import world_eval as E  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


org = W.answer_line("Amazon", "org", "phone", None, date(2026, 10, 2))
check("an organisation: a public fact may be given, labelled and not saved",
      ("general knowledge, not on record" in org, "not saving it" in org), (True, True))
person = W.answer_line("Robin", "person", "phone", None, date(2026, 10, 2))
check("a person: never from general knowledge", ("never fill it in from general knowledge" in person,
                                                 "general knowledge, not on record, and not saving" in person), (True, False))
check("a pet: never either", "never fill it in" in W.answer_line("Biscuit", "pet", "vet", None, date(2026, 10, 2)), True)

ok_org = E._check_for("unknown", "phone", [], "org")
check("Amazon's public number, labelled: passes",
      ok_org("That isn't on record. As general knowledge (not saved), Amazon's support line is 1-888-280-4331."), [])
check("…unlabelled: fails", ok_org("Not on record, but it's 1-888-280-4331."),
      ["gave a public number without saying it's general knowledge, not on record"])
check("a person's number from anywhere: fails",
      E._check_for("unknown", "phone", [], "person")("Not on record. As general knowledge it's 509-555-0101."),
      ["gave a phone number that isn't on record"])
check("still has to say it isn't on record", ok_org("Amazon's support line is 1-888-280-4331, generally known.")[:1],
      ["didn't say it isn't on record"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
