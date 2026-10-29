#!/usr/bin/env python3
# gate: hermetic
"""Bills and timeline correctness (10-02 knowledge and conversation audits):
  - a payment within a week of a failed one closes it, and "Amazon" matches "Amazon.com"
  - a connector's timeline entry is placed when the thing happened (observed_at), never its sync stamp
  - titan's own log_event lines are marked as its summaries, in the timeline and the daily log
  - a person's phone "not on record" says a number in a bill isn't theirs

Run: python3 scripts/test_bills_timeline_truth.py
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-bills-truth-"))
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["AGENT_TURN_LOG"] = str(_TMP / "turns.jsonl")
os.environ["AGENT_TURN_SOURCE"] = "test"
os.environ["DAILY_LOG_DIR"] = str(_TMP / "daily")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_runtime_lib as lib  # noqa: E402
import reflection  # noqa: E402
import world as W  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


prov = lambda n: ("zach", "private")   # noqa: E731
P = {"provenance": {"owner": "zach", "audience": "private"}}

print("\n── a payment closes a failed bill too, and vendors match as merged ──")
nodes = [
    {"node_id": "f1", "node_type": "bill", "data": {"vendor": "Amazon", "amount": 54.1, "payment_status": "failed",
                                                     "mail_date": "2026-09-20", **P}},
    {"node_id": "p1", "node_type": "receipt", "data": {"vendor": "Amazon.com", "amount": 54.1, "paid_date": "2026-09-23", **P}},
    {"node_id": "f2", "node_type": "bill", "data": {"vendor": "T-Mobile", "amount": 254.45, "payment_status": "failed",
                                                     "mail_date": "2026-09-01", **P}},
    {"node_id": "p2", "node_type": "bill", "data": {"vendor": "T-Mobile", "amount": 254.45, "payment_status": "paid",
                                                     "paid_date": "2026-09-20", **P}},
    {"node_id": "d3", "node_type": "bill", "data": {"vendor": "Amazon.com", "amount": 12, "due_date": "2026-10-05",
                                                    "mail_date": "2026-09-25", **P}},
    {"node_id": "p3", "node_type": "receipt", "data": {"vendor": "Amazon", "amount": 12, "paid_date": "2026-09-30", **P}},
]
vals = sorted((o.name, o.value) for o in W.bill_observations(nodes, prov))
check("paid 3 days after failing: the failure is closed (Amazon / Amazon.com)",
      any(v.endswith("failed Sep 20") for _, v in vals), False)
check("paid 19 days after: a separate payment, the failure stands", ("T-Mobile", "$254.45 payment failed Sep 1") in vals, True)
check("a due bill paid under the other spelling is closed", any("due Oct 5" in v for _, v in vals), False)

print("\n── a connector entry is placed when it happened, not when it synced ──")
dl = {"timestamp": "2026-09-30T18:24:00Z", "event_type": "household.note",
      "payload": {"text": "Downloaded tax-return.pdf", "source": "downloads", "synced_at": "2026-09-30T18:24:00Z",
                  "observed_at": "2026-09-12T16:05:00Z", "person": "zach", "audience": "private"}}
check("observed_at, not the sync stamp", reflection._event_ts(dl), reflection._parse_ts("2026-09-12T16:05:00Z"))
plain = {"timestamp": "2026-09-30T18:24:00Z", "payload": {"text": "x", "observed_at": "2026-09-12T16:05:00Z"}}
check("a non-connector entry keeps its own stamp", reflection._event_ts(plain), reflection._parse_ts("2026-09-30T18:24:00Z"))

print("\n── titan's own log lines are marked as its summaries ──")
NOW = datetime(2026, 10, 2, 11, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
ev = {"timestamp": "2026-10-02T17:00:00Z", "payload": {"text": "The T-Mobile bill is $400", "person": "zach",
                                                         "audience": "private", "by": "titan"}}
check("the listing says whose words they are", lib._timeline_listing([ev], now=NOW).split("\n")[1],
      "- Fri Oct 2 10:00 (1 h ago) (titan's summary) The T-Mobile bill is $400")
posted = []
saved = (lib._runtime_request, lib.turn_source, lib._kb_index)
lib._runtime_request = lambda svc, path, method="POST", payload=None, **k: posted.append(payload) or {}
lib.turn_source = lambda: "chat"
lib._kb_index = lambda *a, **k: None
try:
    lib.execute_tool("log_event", {"event": "Paid the water bill"})
finally:
    lib._runtime_request, lib.turn_source, lib._kb_index = saved
check("the timeline entry carries by: titan", posted and posted[-1]["payload"].get("by"), "titan")
day = next((_TMP / "daily").glob("*.md"), None)
check("the daily log line says it's titan's summary", day is not None and "(titan's summary) Paid the water bill" in day.read_text(),
      True)

print("\n── a person's phone not on record: a number in a bill isn't theirs ──")
line = W.answer_line("Robin", "person", "phone", None, NOW.date(), NOW)
check("says so", "not Robin's: don't give it as theirs" in line, True)
check("not for an org's hours", "not Maple's" in W.answer_line("Maple", "org", "hours", None, NOW.date(), NOW), False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
