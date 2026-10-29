#!/usr/bin/env python3
# gate: hermetic
"""world_seed: the household's stated facts written as the person who said them, with where they said it, under
set_fact's rules (a kid's health to the kid and parents). Nothing else; the unstated ones are questions.
Fictional fixture only (fixtures/world_seed.example.json); the real facts live on titan, never in this repo (#869).

Run: python3 scripts/test_world_seed.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-seed-test-"))
os.environ["WORLD_DIR"] = str(_TMP / "world")
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["WORLD_SEED_FILE"] = str(Path(__file__).resolve().parent / "fixtures" / "world_seed.example.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import world as W  # noqa: E402
import world_seed as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PEOPLE = [{"id": "zach", "name": "Alex Hale", "role": "admin", "parents": []},
          {"id": "p_robin", "name": "Robin", "role": "adult", "parents": []},
          {"id": "p_jordan", "name": "Jordan", "role": "adult", "parents": []},
          {"id": "p_ava", "name": "Ava", "role": "kid", "parents": ["zach", "p_robin"]},
          {"id": "p_leo", "name": "Leo", "role": "kid", "parents": ["zach", "p_robin"]}]
w = W.Bubbles(_TMP / "world")
w.sync("people", [W.Obs(entity=W.entity_id(p["name"].split()[0]), etype="person", name=p["name"].split()[0], attr="relation",
                        value=p["role"], observed_at="2026-09-30", kind="people", ref=f"people:{p['id']}",
                        origin="people", owner=None, audience="household", person_id=p["id"], bubble="household",
                        facet="home") for p in PEOPLE])
out = S.seed(w, PEOPLE)
check("every seed written", {o["outcome"] for o in out}, {"ok"})
check("as many claims as seeds and chores", len(out), len(S.SEEDS) + len(S.CHORES))

rows = []
for b in w.bubbles():
    con = w.world(b).con
    rows += [dict(r, bubble=b) for r in con.execute(
        "SELECT e.name, c.attr, c.qual, ev.label, ev.ref, ev.observed_at, ev.audience, ev.by FROM claims c "
        "JOIN entities e ON e.id=c.entity_id JOIN evidence ev ON ev.claim_id=c.id WHERE ev.kind='user'")]
pharm = next(r for r in rows if r["attr"] == "address" and "pharmacy" in (r["qual"] or ""))
check("where it was said: label, ref (per claim), when", (pharm["label"], pharm["ref"], pharm["observed_at"][:16]),
      ("Alex, chat 10-01 23:44", "chat:zach:2026-10-01T23:44#maple-health:address", "2026-10-01T23:44"))
check("the admin's statement", pharm["by"], "zach")
check("an address per purpose, each on its own place", sorted((r["name"], json.loads(r["qual"] or "{}").get("purpose", ""))
                                                             for r in rows if r["attr"] == "address"),
      [("Cedar Dental", "dental"), ("Maple Health", "pharmacy")])
ava = next(r for r in rows if r["name"] == "Ava" and r["attr"] == "medical")
check("a kid's therapy: the kid and her parents only", sorted(json.loads(ava["audience"])), ["p_ava", "p_robin", "zach"])
check("one chat line, several claims: each kept (a store's role and both who it serves)",
      sorted(r["attr"] for r in rows if r["name"] == "Corner Grocery"), ["role", "serves", "serves"])
check("the chores, in the household bubble", sorted(r["name"] for r in rows if r["attr"] == "schedule" and r["bubble"] == "household"),
      ["Garbage", "Recycling"])
ad = next(r for r in rows if r["name"] == "Alex" and r["attr"] == "dentist")
check("the admin's dentist: theirs only, not the household's", json.loads(ad["audience"]), ["zach"])
check("nothing about another adult's own details was written", [r for r in rows if r["name"] == "Robin"], [])
check("the unstated ones are questions", len(S.QUESTIONS), 6)
check("the fixture's people are the fictional ones (fixtures use Alex, Robin, Jordan, Ava, Leo)",
      {e for e, t, *_ in S.SEEDS if t == "person"} <= {"Alex", "Robin", "Jordan", "Ava", "Leo"}, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
