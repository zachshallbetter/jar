#!/usr/bin/env python3
"""
Unit tests for EdgeAlert: alerts fire on change only, `confirm` holds back a one-run blip,
recovery is announced only for an announced outage, and the old state format still reads.
Hermetic: a temporary state dir; delivery is captured, nothing is sent.

Run: python3 scripts/test_titan_alert.py
"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(__file__))

import titan_alert


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


def run():
    ok = True
    sent = []
    titan_alert.STATE_DIR = tempfile.mkdtemp(prefix="titan-alert-test-")
    titan_alert.alert = lambda title, msg, **kw: sent.append(title) or []

    print("\n── confirm=2: a one-run blip is silent ──")
    a = titan_alert.EdgeAlert("t1", "T", confirm=2)
    ok &= check("first failure", a.update({"gemini": "timeout"}), None)
    ok &= check("recovers next run", a.update({}), None)
    ok &= check("nothing sent", sent, [])

    print("\n── confirm=2: two failing runs page once, then recover ──")
    ok &= check("run 1", a.update({"gemini": "timeout"}), None)
    ok &= check("run 2", a.update({"gemini": "403"}), "down")
    ok &= check("run 3 same problem", a.update({"gemini": "403"}), None)
    ok &= check("problem set changes", a.update({"gemini": "x", "kb": "y"}), "changed")
    ok &= check("recovered", a.update({}), "recovered")
    ok &= check("sent", sent, ["T — DOWN", "T — still failing, changed", "T — RECOVERED"])

    print("\n── confirm=1 (default) alerts on the first failure ──")
    sent.clear()
    b = titan_alert.EdgeAlert("t2", "E")
    ok &= check("first failure", b.update({"eval": "8/10"}), "down")
    ok &= check("recovered", b.update({}), "recovered")

    print("\n── old state file (just the alerted key) still reads ──")
    sent.clear()
    with open(os.path.join(titan_alert.STATE_DIR, ".t3-state"), "w") as f:
        f.write("gemini")
    c = titan_alert.EdgeAlert("t3", "C", confirm=2)
    ok &= check("same problem stays quiet", c.update({"gemini": "timeout"}), None)
    ok &= check("recovery is announced", c.update({}), "recovered")
    with open(os.path.join(titan_alert.STATE_DIR, ".t4-state"), "w") as f:
        f.write("")
    ok &= check("empty old state", titan_alert.EdgeAlert("t4", "D", confirm=2).update({}), None)

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
