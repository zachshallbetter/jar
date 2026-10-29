#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for consolidate (titan-node#377): proposals to the right person from corrected turns and gaps,
only notes they may see, delivered through the daily slot, closed by "It's fine".

    python3 scripts/test_consolidate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_TMP = Path(tempfile.mkdtemp())
os.environ.update({"RETRIEVAL_TRACE_DB": str(_TMP / "t.db"), "HOUSEHOLD_DB": str(_TMP / "h.db"),
                   "KB_ACL_DB": str(_TMP / "a.db"), "AGENT_FENCES": "0"})
import consolidate as C  # noqa: E402
import retrieval_trace as T  # noqa: E402
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


class Clock:
    """Times in the household's zone (AGENT_TZ): titan runs on UTC, where a bare "09:00" is 2 AM in Spokane and the
    daily slot hasn't opened (the 10-02 deploy gate failed on that; the Mac, on Pacific time, passed)."""
    def __init__(self, iso):
        self.set(iso)

    def __call__(self):
        return self.t

    def set(self, iso):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("AGENT_TZ", "America/Los_Angeles"))
        self.t = datetime.fromisoformat(iso).replace(tzinfo=tz).timestamp()


clock = Clock("2026-10-03T12:00")
B = W.Bubbles(_TMP / "world", clock=clock)
B.ensure("maple-animal-clinic", "org", "Maple Animal Clinic", "household", aliases=[("vet", "")])
for q in ("vet hours?", "when is the vet open?"):
    W.context(B, q, "zach", "admin", gaps=True)
W.context(B, "vet hours?", "jordan", "adult", gaps=True)
W.context(B, "is the vet open?", "jordan", "adult", gaps=True)
T.begin()
T.query("search", "x", [{"source_id": "n-old"}], [{"source_id": "n-old"}])
T.mark("shown", [{"source_id": "n-old"}])
T.end("t1", "zach", ["n-old"], corrected=2)
SOURCES = [{"source_id": "n-old", "title": "Old vet info", "source_type": "note"},
           {"source_id": "n-vet", "title": "Maple Animal Clinic", "source_type": "note"},
           {"source_id": "cx-mail-1", "title": "Maple Animal Clinic newsletter", "source_type": "connector:mail"}]
ACL = {"n-old": ("zach", "private"), "n-vet": ("zach", "private"), "cx-mail-1": ("zach", "private")}
ROLES = {"zach": "admin", "jordan": "adult"}
props = C.proposals(B, SOURCES, ACL, ROLES, C.suspects(7), 7)
check("a cited note in a corrected turn: a check for its owner; a gap with a note naming it: a gapnote for the asker",
      sorted((p["person"], p["kind"], p["source_id"]) for p in props),
      [("zach", "check", "n-old"), ("zach", "gapnote", "n-vet")])
check("…never a note the asker can't see (Jordan asked too; the note is Zach's)", any(p["person"] == "jordan" for p in props),
      False)
check("…mail isn't a note to fix", any(p["source_id"] == "cx-mail-1" for p in props), False)
check("stored in each person's own file, once", (C.store(B, props), C.store(B, props)), (2, 0))
cards = []
send = lambda title, detail, **kw: cards.append((kw["to"], title, kw["actions"]))
clock.set("2026-10-04T09:00")
W.daily_questions(B, [{"id": "zach", "role": "admin"}], send=send)
check("the daily slot: the gap card first (it outranks a note review)", [t for _, t, _ in cards][0].startswith("You've asked"),
      True)
W.answer_ask(B, cards[0][2][0]["value"], "zach", "admin", [])
cards.clear()
clock.set("2026-10-05T09:00")
W.daily_questions(B, [{"id": "zach", "role": "admin"}], send=send)
check("next day, a note review, to Zach alone", [(to, t) for to, t, _ in cards],
      [(["zach"], "A reply that used your note “Old vet info” was corrected")])
check("…its first button starts a turn he sees, marked so memory never learns from it",
      cards[0][2][0]["value"].startswith("(from a suggestion card)"), True)
check("…It's fine closes it", W.answer_ask(B, cards[0][2][1]["value"], "zach", "admin", []), (True, "OK."))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
