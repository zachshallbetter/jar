#!/usr/bin/env python3
"""redact.py: identifiers that never need storing are removed; ordinary text, amounts and last-four
digits stay; money is recognised (09-30 review, F-18/F-19/F-20).

Run: python3 scripts/test_redact.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import os as _os_acl, tempfile as _tf_acl  # noqa: E402
_os_acl.environ.setdefault("KB_ACL_DB", _os_acl.path.join(_tf_acl.mkdtemp(prefix="titan-acl-"), "kb_acl.db"))  # #1273: ingest writes sharing rows; never titan's own
import redact  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


r = redact.redact_personal
print("\n── removed ──")
check("a one-time code", r("If this was you, your verification code is: 812345"), "If this was you, your verification code is: [removed]")
check("a meeting passcode", r("Meeting ID: 272 813 389  Passcode: f3Kx9q"), "Meeting ID: 272 813 389  Passcode: [removed]")
check("an account number", r("Bill issue date Sep 08, 2026 Account 979912345 Page 1"), "Bill issue date Sep 08, 2026 Account [removed] Page 1")
check("an account # with dashes", r("Account #1234-5678-90 payment"), "Account #[removed] payment")
check("a card number", r("Card 4111 1111 1111 1111 on file"), "Card [card number removed] on file")
check("a routing number", r("Routing number: 123456789"), "Routing number: [removed]")
check("an SSN", r("SSN 123-45-6789"), "SSN [removed]")
print("\n── kept ──")
for text in ["AutoPay with MasterCard ending in 2311 restores your $30 discount.", "Total due $254.45 by Oct 27, 2026.",
             "Call (509) 555-0100 about the 2:15 PM pickup.", "Meeting ID: 272 813 389 261 43", "The account is in Zach's name."]:
    check(f"unchanged: {text[:40]!r}", r(text), text)
print("\n── money ──")
check("about money", [redact.about_money(t) for t in ("T-Mobile subscription bill amount: $400.00", "Paid the water bill",
      "The October rent is due", "Ava has ballet on Monday", "Leftover chili for dinner")], [True, True, True, False, False])

print("\n── wired in ──")
src = open(os.path.join(os.path.dirname(__file__), "agent_api_server.py")).read()
check("a turn's question is redacted before either brain sees it", "question = redact.redact_personal(question)" in src, True)
check("… and the kept history too", src.count("redact.redact_personal(") >= 3, True)
check("synced text is redacted before indexing",
      "redact.redact_personal(str(raw.get(\"text\")" in open(os.path.join(os.path.dirname(__file__), "connectors_runner.py")).read(), True)

print("\n── money goes to the adults, not the household (09-30: a bill reached the kids twice) ──")
import tempfile  # noqa: E402
os.environ["AGENT_TURN_LOG"] = os.path.join(tempfile.mkdtemp(), "turns.jsonl")
os.environ["DAILY_LOG_DIR"] = tempfile.mkdtemp()
os.environ["AGENT_TURN_SOURCE"] = "user"
import agent_runtime_lib as lib  # noqa: E402
posted, indexed = [], []
orig = (lib._runtime_request, lib._kb_index)
lib._runtime_request = lambda svc, path, method="GET", payload=None, *a, **k: posted.append(payload) or {}
lib._kb_index = lambda rid, title, text, kind, meta, owner=None, audience=None, pin=None: indexed.append(audience)
try:
    out = lib.execute_tool("log_event", {"event": "T-Mobile subscription bill amount: $400.00", "for": "household"})
    check("a bill asked for the household: adults, and it says so",
          (posted[-1]["payload"]["audience"], out.endswith("(kept for the adults: it's about money)")), ("adults", True))
    lib.execute_tool("log_event", {"event": "Plumber fixed the kitchen sink", "for": "household"})
    check("not money: the household, as asked", posted[-1]["payload"]["audience"], "household")
    lib._TURN_IO.asker_words = "save this for the family"   # #1397: the asker named the audience
    out = lib.execute_tool("ingest_text", {"title": "October T-Mobile Bill Estimate", "content": "Total $254.45, due Oct 27.",
                                            "share": "household"})
    check("a money note shared with the household: adults", (indexed[-1], "Kept for the adults" in out), ("adults", True))
    lib.execute_tool("ingest_text", {"title": "Trash night", "content": "Trash goes out Tuesday nights.", "share": "household"})
    check("a household note: the household", indexed[-1], "household")
finally:
    lib._runtime_request, lib._kb_index = orig

print("\n── what tools read, and the history the apps resend (the owner's review) ──")
orig_x = lib._execute_tool
lib._execute_tool = lambda name, args: "Bill issue date Sep 08, 2026 Account 979912345 Total due $254.45"
try:
    out = lib.execute_tool("file_read", {"path": "/ai/tmp/uploads/bill.pdf"})
    check("a document read never carries the account number", ("979912345" in out, "$254.45" in out), (False, True))
    out = lib.execute_tool("get_datetime", {})
    check("other tools are untouched", "979912345" in out, True)
finally:
    lib._execute_tool = orig_x
print("── device serials and IMEIs (10-05) ──")
R = redact.redact_personal
check("a labelled serial number, IMEI or MEID", [R(x) for x in ("serial number `356354100017855`", "IMEI: 990000862471854",
      "S/N C02XG2JHJG5J", "MEID 35209900176148")], ["serial number `[removed]`", "IMEI: [removed]", "S/N [removed]",
                                                    "MEID [removed]"])
check("a bare 15-digit number (an IMEI's shape)", R("device 356354100017855 received"), "device [device id removed] received")
check("…amounts, dates, phones, a 16-digit card rule and order numbers of other lengths are untouched",
      R("$250.00 on 10/05/2026, call 509-555-0142, order 12345678901"), "$250.00 on 10/05/2026, call 509-555-0142, order 12345678901")
real_x = lib._execute_tool
lib._execute_tool = lambda name, args: "T-Mobile: we received your Apple Watch (serial number 356354100017855)."
try:
    check("search_mail's results reach no model with a device id", "356354100017855" in lib.execute_tool("search_mail", {"query": "trade-in"}),
          False)
finally:
    lib._execute_tool = real_x
check("history is redacted where the turn starts", "redact.redact_personal(m[\"content\"])" in src, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
