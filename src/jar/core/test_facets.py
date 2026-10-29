#!/usr/bin/env python3
# gate: hermetic
"""Facet chips and held axes (#951, rules 7 and 9): free text becomes deterministic filters, and one axis of what was
found can be held while the rest varies. Fictional items only. No titan, no model.
Run: python3 scripts/test_facets.py"""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import facets as F  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


@dataclass
class It:
    item_id: str
    source: str
    at: date | None
    keys: dict = field(default_factory=lambda: defaultdict(set))


def it(i, src, at, **keys):
    x = It(i, src, date.fromisoformat(at) if at else None)
    for k, v in keys.items():
        x.keys[k.replace("_", ":", 1)] = set(v)
    return x


ITEMS = {i.item_id: i for i in (
    it("gas-sep", "record", "2026-09-05", party_vendor={"harbor gas"}),
    it("gas-oct", "record", "2026-10-04", party_vendor={"harbor gas"}),
    it("gas-mail", "mail", "2026-09-04", party_domain={"harborgas.com"}),
    it("hm-text", "text", "2026-09-27"),
    it("hm-mail", "mail", "2026-09-28", party_domain={"harbormobile.com"}, party_entity={"org:hm"}),
    it("clinic", "mail", "2026-09-29", party_domain={"lakesideclinic.org"}),
)}
TODAY = date(2026, 10, 5)
V = {"harbor gas": "Harbor Gas"}
D = {"harborgas.com", "harbormobile.com", "lakesideclinic.org"}
N = {"org:hm": "Harbor Mobile"}


def labels(cs):
    return [(c.axis, c.label) for c in cs]


print("── words become chips, in code ──")
check("a vendor as filed", labels(F.chips("harbor gas bills", TODAY, V, D, [], N)),
      [("party", "Harbor Gas"), ("kind", "bills and receipts")])
check("an organisation the world model found", labels(F.chips("what's going on with harbor mobile", TODAY, V, D, ["org:hm"], N)),
      [("party", "Harbor Mobile")])
check("a sender's domain by its name", labels(F.chips("anything from lakesideclinic", TODAY, {}, D, [], {})),
      [("party", "lakesideclinic.org")])
check("a month without a year: the latest one", labels(F.chips("september", TODAY, {}, set(), [], {})), [("time", "Sep 2026")])
check("…a later month is last year's", labels(F.chips("november", TODAY, {}, set(), [], {})), [("time", "Nov 2025")])
check("last month, this week, a date", [labels(F.chips(q, TODAY, {}, set(), [], {})) for q in
                                        ("last month", "this week", "2026-09-28")],
      [[("time", "Sep 2026")], [("time", "week of Oct 5")], [("time", "Sep 28, 2026")]])
check("texts and mail: either", labels(F.chips("my texts and mail", TODAY, {}, set(), [], {})),
      [("kind", "mail or texts")])
check("no chip words: no chips (the title match takes over)", F.chips("the trade-in thing", TODAY, V, D, [], N), [])

print("── chips narrow, together ──")
vis = set(ITEMS)
check("Harbor Gas bills in September", F.narrow(ITEMS, vis, F.chips("harbor gas bills in september", TODAY, V, D, [], N)),
      {"gas-sep"})
check("texts from September", F.narrow(ITEMS, vis, F.chips("texts in september", TODAY, V, D, [], N)), {"hm-text"})
check("only what the viewer may see", F.narrow(ITEMS, vis - {"gas-sep"}, F.chips("harbor gas", TODAY, V, D, [], N)),
      {"gas-oct"})

print("── hold an axis, vary the rest (rule 7) ──")
check("same vendor, other months", F.hold("party", {"gas-sep"}, ITEMS, vis), ("the same party", {"gas-oct"}))
check("the same weeks, other parties", F.hold("time", {"hm-mail"}, ITEMS, vis)[1], {"hm-text", "clinic"})
check("the same kind", F.hold("kind", {"hm-text"}, ITEMS, vis)[1], set())
check("…never beyond what the viewer may see", F.hold("party", {"gas-sep"}, ITEMS, vis - {"gas-oct"})[1], set())
try:
    F.hold("mood", {"gas-sep"}, ITEMS, vis)
    check("an unknown axis is refused", False, True)
except ValueError:
    check("an unknown axis is refused", True, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
