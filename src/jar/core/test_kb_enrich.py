#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for kb_enrich (titan-node#371): headers per source type in the household's words, idempotent
enrichment, the scoring the comparison uses, and off by default.

    python3 scripts/test_kb_enrich.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_enrich as E  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


NAMES = {"zach": "Zach Hale", "p_k": "Ava"}
CALS = {"ava": "Ava"}
cal = "Ballet + Modern\nThursday, Oct 8, 2026, 4:30 PM – 6:00 PM\nCalendar: Ava\nLocation: 123 Adams St"
check("a calendar event on an assigned calendar is that person's", E.header("connector:calendar", "Ballet + Modern", cal,
      {"owner": "zach"}, NAMES, CALS), "Ava's calendar event: Ballet + Modern, Oct 8")
check("…on the person's own unnamed calendar, theirs", E.header("connector:calendar", "Dentist",
      "Dentist\nTuesday, Oct 6, 2026, 3:00 PM\nCalendar: Zach", {"owner": "zach"}, NAMES, CALS),
      "Zach's calendar event: Dentist, Oct 6")
check("…on a shared calendar, named", E.header("connector:calendar", "Trash", "Trash\nWednesday, Oct 7, 2026, 8:00 PM\n"
      "Calendar: Family", {"owner": "zach"}, NAMES, CALS), "Calendar event on the Family calendar: Trash, Oct 7")
check("an email: to whom, from whom, when", E.header("connector:mail", "Your prescription is ready",
      "From: Maple Health <noreply@x>\nSubject: Your prescription is ready\n\nReady.", {"owner": "zach",
      "observed_at": "2026-10-02T09:00:00Z"}, NAMES, CALS), "Email to Zach from Maple Health, Oct 2: Your prescription is ready")
check("a note: whose", E.header("note", "Vet info", "...", {"owner": "p_k"}, NAMES, CALS), "Ava's note: Vet info")
check("titan's own records: nothing added", E.header("knowledge_record", "Runbook", "...", {}, NAMES, CALS), "")
check("enrichment is idempotent", E.enrich(E.enrich("text", "H"), "H"), "H\ntext")


def bow(texts):
    vocab = sorted({w for t in texts for w in re.findall(r"[a-z']+", t.lower())} | {"ava's", "ballet", "when", "is"})
    return [[t.lower().count(v) for v in vocab] for t in texts]


docs = ["Ballet + Modern\nThursday\nCalendar: Ava", "Ballet shoes sale\nnewsletter"]
calls = []


def emb(texts):
    calls.append(len(texts))
    return bow(texts + ["When is Ava's ballet?", "ballet + modern calendar: ava newsletter sale shoes thursday ava's when is"])[:len(texts)]


r = E.score(docs, docs, [("ava-ballet", "When is Ava's ballet?", ["calendar: ava"])], embed=emb)
check("the comparison scores hit@1 and hit@3 by the item's own words", (r["hit@3"], set(r["rank"])), ("1/1", {"ava-ballet"}))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
