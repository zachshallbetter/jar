#!/usr/bin/env python3
# gate: hermetic
"""'File it' suggestions for Unsorted (titan-node#1087), on #951's edges, scored by the real linker:
  - at most one project per unsorted item, with a templated reason; auto only for a verified link;
  - accepted and candidate stay suggestions ("Possibly: …" for a candidate); a rejected pair never suggests;
  - two projects holding an item equally: no suggestion; an item already in a project: none;
  - privacy, the mock's leak shape: a kid's mail in a project never carries a suggestion to a parent, never counts in
    the reason, never appears in `via`; kids and shared screens get none.
Fictional people and items; nothing leaves the process.
Run: python3 scripts/test_file_suggest.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import file_suggest as fs  # noqa: E402
import linker  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))


def item(iid, source, day, owner, audience, **keys):
    it = linker.Item(iid, source, date(2026, 10, day), owner, audience, defaultdict(set))
    for k, v in keys.items():
        it.keys[k.replace("__", ":")] |= set(v if isinstance(v, (list, set, tuple)) else [v])
    return it


ITEMS = {i.item_id: i for i in (
    # Kitchen remodel: two of Alex's receipts from one store, one with an order number and a mail id
    item("r1", "scan", 1, "alex", "owner", party__vendor="harbor hardware", id__order="HH-4471", id__message="m-r1"),
    item("r2", "scan", 3, "alex", "owner", party__vendor="harbor hardware"),
    # Ava's science fair: Ava's own mail from her teacher (a kid's mail: never Alex's to see), and Alex's note
    item("k1", "mail", 2, "ava", "owner", id__message="m-k1", party__addr="ms.leo@school.example"),
    item("s1", "note", 2, "alex", "owner", party__entity="ent-fair"),
    # unsorted, Alex's
    item("u1", "mail", 4, "alex", "owner", id__order="HH-4471"),                        # same order: accepted
    item("u2", "scan", 5, "alex", "owner", id__source="m-r1"),                          # filed from r1's mail? see below
    item("u3", "mail", 6, "alex", "owner", party__vendor="harbor hardware"),            # same store, nothing else
    item("u4", "mail", 3, "alex", "owner", id__message="m-k1"),                         # only the kid's mail ties it
    item("u5", "mail", 2, "alex", "owner", id__order="HH-4471", party__entity="ent-fair", id__case="C-77"),
    item("u6", "scan", 2, "alex", "owner", id__invoice="INV-9"),                       # Alex said: not related to r1
    item("u7", "mail", 3, "alex", "owner", id__message="m-k1", party__entity="ent-fair"),  # kid's mail + a weak tie
    # Robin's own, never Alex's
    item("p1", "mail", 4, "robin", "owner", id__order="HH-4471"),
)}
# u2 is a record filed from r1's mail: the linker's id:source key pairs it with the mail that carries it
ITEMS["r1"].keys["id:source"].add("m-r1")
# s1 shares a case number with u5 so u5 ties to both projects equally (accepted, one member each)
ITEMS["s1"].keys["id:case"].add("C-77")
ITEMS["r1"].keys["id:invoice"].add("INV-9")
SAID = {linker._pair("u6", "r1"): False}
EDGES = linker.score(ITEMS, SAID)
PROJECTS = [{"id": "pj-kitchen", "name": "Kitchen remodel", "members": ["r1", "r2"]},
            {"id": "pj-fair", "name": "Ava's science fair", "members": ["k1", "s1"]}]


def vis_for(pid):
    return lambda it: it.owner == pid or it.audience in ("household", "adults")


S = fs.suggest(ITEMS, EDGES, vis_for("alex"), PROJECTS, role="adult")

print("── one project each, a templated reason, auto only when verified ──")
check("u1 (same order number as r1): Kitchen, accepted, not auto",
      (S["u1"]["project"], S["u1"]["state"], S["u1"]["auto"], S["u1"]["via"]), ("pj-kitchen", "accepted", False, ["r1"]))
check("…its reason", S["u1"]["reason"], "Same order number as a scan in Kitchen remodel")
check("u2 (filed from r1's mail): verified, auto", (S["u2"]["project"], S["u2"]["state"], S["u2"]["auto"]),
      ("pj-kitchen", "verified", True))
check("…its reason", S["u2"]["reason"], "Filed from the same mail as a scan in Kitchen remodel")
check("u3 (the same store, days apart): a candidate, a 'Possibly' over both receipts",
      (S["u3"]["state"], S["u3"]["auto"], S["u3"]["reason"]),
      ("candidate", False, "Possibly: same vendor as 2 scans in Kitchen remodel"))

print("\n── never ──")
check("u5 holds Kitchen and the fair equally: no suggestion", "u5" in S, False)
check("u6 shares an invoice with r1 but Alex said they're not related: none", "u6" in S, False)
check("…without his word it would be Kitchen (the check can fail)",
      fs.suggest(ITEMS, linker.score(ITEMS, {}), vis_for("alex"), PROJECTS, role="adult")["u6"]["project"], "pj-kitchen")
check("an item already in a project isn't suggested", any(i in S for i in ("r1", "r2", "s1")), False)
check("Robin's item is never in Alex's suggestions", "p1" in S, False)

print("\n── privacy: the kid's mail (the mock's leak) ──")
check("u4 is tied only by Ava's mail: no suggestion to her project for Alex", "u4" in S, False)
check("no suggestion's via names the kid's mail", any("k1" in v["via"] for v in S.values()), False)
check("u7: a strong tie to the kid's mail, a weak one to Alex's note: only the note counts",
      (S["u7"]["state"], S["u7"]["via"], S["u7"]["reason"]),
      ("candidate", ["s1"], "Possibly: names the same person or place as a note in Ava's science fair"))
check("…had the kid's mail been visible, u4 would be suggested on it (the check can fail)",
      fs.suggest(ITEMS, EDGES, lambda it: True, PROJECTS, role="adult")["u4"]["via"], ["k1"])
SA = fs.suggest(ITEMS, EDGES, vis_for("ava"), PROJECTS, role="kid")
check("a kid gets none", SA, {})
check("a shared screen gets none", fs.suggest(ITEMS, EDGES, lambda it: True, PROJECTS, role="household"), {})
SR = fs.suggest(ITEMS, EDGES, vis_for("robin"), PROJECTS, role="adult")
check("Robin: her own p1 ties to r1, which she can't see: nothing", SR, {})

print("\n── a person's word ──")
said2 = {**SAID, linker._pair("u3", "s1"): True}
S2 = fs.suggest(ITEMS, linker.score(ITEMS, said2), vis_for("alex"), PROJECTS, role="adult")
check("Alex linked u3 to his fair note: verified beats the store candidate; auto",
      (S2["u3"]["project"], S2["u3"]["auto"], S2["u3"]["reason"]),
      ("pj-fair", True, "You linked it to a note in Ava's science fair"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
