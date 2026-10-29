#!/usr/bin/env python3
# gate: hermetic
"""Bills in Now (Zach 10-10: "57 things wait on you", nearly all bills, many identical): one row per account, its
latest actionable cycle with "+N earlier"; only actionable cycles (failed in the last 30 days, due from 14 days ago to
14 days ahead); human words in the status slot, never state names; the owner's own facet on the row. A shared bill's
row never counts someone's private cycles. Fictional vendors and people.
Run: python3 scripts/test_items_bills.py"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import items as I  # noqa: E402
import obligations as O  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


TODAY = date(2026, 10, 10)


def th(acct, cycle_days_ago, state, *, due_in=None, owner="alex", audience="private", failed_days_ago=None, amount=25.0):
    cyc = TODAY - timedelta(days=cycle_days_ago)
    t = O.Thread(acct, acct.capitalize(), cyc, amount, state=state, owner=owner, audience=audience,
                 due=(TODAY + timedelta(days=due_in)) if due_in is not None else None)
    if failed_days_ago is not None:
        t.pieces.append(O.Piece(TODAY - timedelta(days=failed_days_ago), "failed", amount, "r1"))
    return t


print("── one row per account, the latest actionable cycle, \"+N earlier\" ──")
ts = [th("harborcloud", 300, "stale"), th("harborcloud", 200, "overdue_unconfirmed", due_in=-190),
      th("harborcloud", 40, "overdue_unconfirmed", due_in=-30), th("harborcloud", 8, "due", due_in=5)]
rows = I.bills_now(ts, TODAY)
check("four cycles of one account: one row", len(rows), 1)
check("…the latest actionable one (due in 5 days)", rows[0]["id"], f"bill:harborcloud:{(TODAY - timedelta(days=8)).isoformat()}")
check("…with the other three unpaid cycles as \"+3 earlier\"", rows[0].get("earlier"), 3)
m = I._member(rows[0], "r", [], "declared", 1)
check("…its subtitle says so", m["slots"].get("subtitle"), "+3 earlier")

print("── only actionable cycles reach Now ──")
for label, t, want in (("failed 10 days ago", th("dock", 12, "failed", failed_days_ago=10), True),
                       ("failed 40 days ago", th("dock", 45, "failed", failed_days_ago=40), False),
                       ("due in 14 days", th("dock", 1, "due", due_in=14), True),
                       ("due in 20 days", th("dock", 1, "due", due_in=20), False),
                       ("overdue by 10 days, unconfirmed", th("dock", 30, "overdue_unconfirmed", due_in=-10), True),
                       ("overdue by 30 days, unconfirmed", th("dock", 50, "overdue_unconfirmed", due_in=-30), False),
                       ("stale", th("dock", 80, "stale"), False), ("paid", th("dock", 5, "resolved", due_in=2), False)):
    check(f"{label}: {'in Now' if want else 'not in Now'}", bool(I.bills_now([t], TODAY)), want)
old = [th("lantern", 400 - 30 * k, "stale") for k in range(5)]
check("an account with only old cycles has no row (its page keeps the history)", I.bills_now(old, TODAY), [])

print("── human words, never state names ──")
for state, want in (("failed", "Payment failed"), ("overdue_unconfirmed", "Overdue?"), ("due", "Due"),
                    ("partly_paid", "Partly paid"), ("contested", "Check this")):
    t = th("pier", 2, state, due_in=3, failed_days_ago=1 if state == "failed" else None)
    it = I.bills_now([t], TODAY)[0]
    check(f"{state}: \"{want}\"", I._member(it, "r", [], "declared", 1)["slots"]["status"]["text"], want)
check("no word anywhere is a state key", any("_" in w for w in I.BILL_WORDS.values()), False)

print("── the owner's own facet on the row; nobody else's cycles in a shared row ──")
fac = {"alex": {"vendor:harborcloud": "work"}}
r = I.bills_now([th("harborcloud", 3, "due", due_in=4), th("harborcloud", 60, "stale")], TODAY, fac)[0]
check("the facet map's vendor key files the row as work", (r.get("facet"), I._member(r, "r", [], "declared", 1)["slots"]["subtitle"]),
      ("work", "Work · +1 earlier"))
check("another person's map never reaches Alex's row",
      I.bills_now([th("harborcloud", 3, "due", due_in=4)], TODAY, {"robin": {"vendor:harborcloud": "work"}})[0].get("facet"), None)
mix = [th("tidewater", 3, "due", due_in=4, owner="alex", audience="adults"),
       th("tidewater", 40, "overdue_unconfirmed", due_in=-30, owner="robin", audience="private"),
       th("tidewater", 70, "stale", owner="robin", audience="private")]
rows = I.bills_now(mix, TODAY)
check("a shared bill and someone's private cycles of the same vendor stay apart: the shared row counts none of them",
      [(x["owner"], x["audience"], x.get("earlier")) for x in rows], [("alex", "adults", None)])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
