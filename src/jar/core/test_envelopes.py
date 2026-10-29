#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Item envelopes (titan-node#390): a fake local model, fake fences. The record carries provenance unchanged and
the original's digest, never its text; the summary's numbers must be in the original; it's redacted; the fences the
original touches are carried even when the summary drops the word; the flag is off by default.
Run: python3 scripts/test_envelopes.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.pop("ITEM_ENVELOPES", None)
sys.path.insert(0, str(Path(__file__).parent))

import envelopes as E  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


TEXT = ("From: Lincoln Elementary\nSubject: Fall recital\n\nThe fall recital is Thursday, October 8 at 6 PM in the gym. "
        "Ava's class sings second. Tickets are $5 at the door. Account 4417-2290-1188.")


def fake(summary):
    return lambda prompt, url: ({"summary": summary}, "qwen3-8b")


class Fences:
    def touches(self, text):
        return [{"id": 7}] if "ava" in text.lower() else []

    def subject_in(self, text):
        return "ava" if "ava" in text.lower() else None


print("── the summary, checked in code ──")
check("off by default", E.enabled(), False)
s, ev, how = E.summarise("Fall recital", TEXT, "connector:mail",
                         chat=fake("Lincoln Elementary's fall recital is Thursday, October 8 at 6 PM in the gym; $5 tickets."))
check("a faithful summary is kept, with its evaluator", (how, ev, "October 8 at 6 PM" in s), ("model", "model:qwen3-8b", True))
s, ev, how = E.summarise("Fall recital", TEXT, "connector:mail", chat=fake("The recital is Thursday, October 9 at 7 PM."))
check("a number not in the original (a made-up date): refused, the title stands in", (how, s), ("title", "Fall recital"))
s, _, how = E.summarise("Fall recital", TEXT, "connector:mail", chat=fake("Recital Oct 8; pay with account 4417-2290-1188."))
check("an account number is redacted even when it's in the original", "4417" in s, False)
s, ev, how = E.summarise("Fall recital", TEXT, "connector:mail", chat=lambda p, u: (_ for _ in ()).throw(OSError("busy")))
check("the model unreachable: the title, by code", (how, ev, s), ("title", "code:title", "Fall recital"))
check("numbers_ok: formats aside", (E.numbers_ok("$1,234.50 due 10/9", "Amount: 1234.50, due 10/9"),
                                    E.numbers_ok("due 10/19", "due 10/9")), (True, False))

print("── the envelope ──")
item = {"source_id": "cx-mail-1", "source_type": "connector:mail", "title": "Fall recital", "text": TEXT,
        "metadata": {"owner": "zach", "audience": "private", "bubble": "zach", "facet": "kids",
                     "observed_at": "2026-10-02T09:00:00Z", "upstream_id": "<m1@x>", "secret": "no"}}
env = E.build(item, chat=fake("Lincoln Elementary's fall recital is Thursday, October 8 at 6 PM."), fences=Fences(), now=0)
check("provenance carried unchanged; nothing else from the metadata",
      {k: env["record"].get(k) for k in ("owner", "audience", "bubble", "facet", "observed_at", "upstream_id", "secret")},
      {"owner": "zach", "audience": "private", "bubble": "zach", "facet": "kids", "observed_at": "2026-10-02T09:00:00Z",
       "upstream_id": "<m1@x>", "secret": None})
check("the original's digest, never its text", (env["record"]["digest"].startswith("sha256:"), "Tickets" in json.dumps(env)),
      (True, False))
check("the summary is an interpretation, labelled lossy", (env["interpretation"]["label"], env["kind"]),
      ("lossy-disclosed", "item_envelope"))
check("the original's fence is carried though the summary doesn't name Ava",
      (env["fences"], "ava" in env["interpretation"]["summary"].lower()), ({"touched": [7], "subject": "ava"}, False))
check("what the KB would index: title, summary, where from and when",
      E.index_text(env), "Fall recital\nLincoln Elementary's fall recital is Thursday, October 8 at 6 PM.\n(mail, 2026-10-02)")
same = E.build(item, chat=fake("x"), fences=Fences(), now=0)
check("the digest is the original's: the same item, the same digest", same["record"]["digest"], env["record"]["digest"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
