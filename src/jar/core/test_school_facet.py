#!/usr/bin/env python3
# gate: hermetic
"""#354 (decided 10-02): a minor's school facet goes to both parents, whatever brought it; a vendor that's both work
and personal is personal by default.

Run: python3 scripts/test_school_facet.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-school-test-"))
os.environ["WORLD_DIR"] = str(_TMP / "world")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import world as W  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


W._PARENTS.update(at=1e18, map={W.bubble_name("p_ava"): ("p_ava", ["zach", "p_mir"])})
w = W.Bubbles(_TMP / "world")
kb = W.bubble_name("p_ava")
school = W.Obs(entity="lincoln-middle", etype="org", name="Lincoln Middle", attr="hours", value="8:30 AM–3:10 PM",
               observed_at="2026-10-01", kind="note", ref="portal:1", origin="portal", owner="zach", audience="private",
               bubble=kb, facet="school")
personal = W.Obs(entity="kates-diary", etype="org", name="Diary club", attr="hours", value="Fri 4 PM",
                 observed_at="2026-10-01", kind="note", ref="note:2", origin="note", owner="p_ava", audience="private",
                 bubble=kb, facet="personal")
w.sync("portal", [school, personal])
rows = {r["name"]: r["audience"] for r in w.world(kb).con.execute(
    "SELECT e.name, ev.audience FROM evidence ev JOIN claims c ON c.id=ev.claim_id JOIN entities e ON e.id=c.entity_id")}
check("school facet: Ava and both parents", sorted(json.loads(rows["Lincoln Middle"])), ["p_ava", "p_mir", "zach"])
check("another facet keeps its own audience", rows["Diary club"], "private")
check("Robin sees the school hours", any(f.attr == "hours" for f in w.facts(f"{kb}:lincoln-middle", "p_mir", "adult")), True)
check("Leo doesn't", any(f.attr == "hours" for f in w.facts(f"{kb}:lincoln-middle", "p_leo", "kid")), False)
adult = W.Obs(**{**school.__dict__, "bubble": W.bubble_name("p_sab"), "entity": "gonzaga", "name": "Gonzaga", "ref": "x"})
check("an adult's school facet isn't widened", W.school_to_parents(adult).audience, "private")
check("a household item is left as it is", W.school_to_parents(W.Obs(**{**school.__dict__, "audience": "household"})).audience,
      "household")

print("\n── a vendor that's both is personal by default ──")
nodes = [{"node_id": "b1", "node_type": "bill", "data": {"vendor": "Amazon", "amount": 12, "due_date": "2026-10-20",
                                                        "mail_date": "2026-10-01", "provenance": {"owner": "zach", "audience": "private"}}}]
prov = lambda n: ("zach", "private")   # noqa: E731
check("unmapped: personal", W.bill_observations(nodes, prov)[0].facet, "personal")
check("mapped personal (the card's 'Personal or both'): personal",
      W.bill_observations(nodes, prov, facets={"zach": {"vendor:amazon": "personal"}})[0].facet, "personal")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
