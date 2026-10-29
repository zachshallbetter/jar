#!/usr/bin/env python3
# gate: hermetic
"""The household's chores as structured rules (10-02: "Recycling goes out on the Wednesday nights when the kids are at
their dad's" was prose in a note): a schedule with a `when` condition, evaluated in code against custody, for Home,
the letter and the per-turn context. Never guessed: without custody to read, a conditional chore says it depends.

Run: python3 scripts/test_chores.py
"""
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-chores-test-"))
os.environ["WORLD_DIR"] = str(_TMP / "world")
os.environ["AGENT_TZ"] = "America/Los_Angeles"
os.environ["WORLD_SEED_FILE"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "world_seed.example.json")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import home_screen as H  # noqa: E402
import reflection  # noqa: E402
import world as W  # noqa: E402
import world_seed as S  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


PT = ZoneInfo("America/Los_Angeles")
PEOPLE = [{"id": "zach", "name": "Zach", "role": "admin"}, {"id": "p_mir", "name": "Robin", "role": "adult"},
          {"id": "p_ava", "name": "Ava", "role": "kid", "parents": ["zach", "p_mir"]},
          {"id": "p_leo", "name": "Leo", "role": "kid", "parents": ["zach", "p_mir"]}]
WED1, WED2, THU = date(2026, 10, 7), date(2026, 10, 14), date(2026, 10, 8)


def stays(kid, at):   # away Oct 1 9 AM → Oct 8 9 AM, here the week after
    away = datetime(2026, 10, 1, 9, tzinfo=PT) <= at < datetime(2026, 10, 8, 9, tzinfo=PT)
    return {"state": "away" if away else "here"}


print("\n── the condition is a qualifier, normalized in code ──")
check("kids at their dad's → kids away", W.norm_qual({"when": "kids at their dad's"}), {"when": "kids away"})
check("kids home → kids here", W.norm_qual({"when": "Kids home"}), {"when": "kids here"})
check("anything else is dropped", W.norm_qual({"when": "full moon"}), {})
check("said in words", W.qual_words({"when": "kids away"}), "when the kids are away")

print("\n── seeded as the household's ──")
w = W.Bubbles(_TMP / "world")
for name, when, qual in S.CHORES:
    ok, said, code = W.set_fact(w, name, "schedule", when, "zach", "admin", etype="activity", people=PEOPLE, qual=qual,
                                source=S.CHORE_NOTE, household=True)
    check(f"{name}: written", code, "ok")
check("in the household bubble", sorted(r["name"] for _, r in w.world("household").entities()), ["Garbage", "Recycling"])
check("a kid wanting to make a household chore can't",
      W.set_fact(w, "Compost", "schedule", "Fridays", "p_ava", "kid", etype="activity", people=PEOPLE, household=True)[2]
      in ("ok", "proposed") and "compost" in [r["name"].lower() for _, r in w.world("household").entities()], False)

print("\n── due on a day, by custody ──")
due = W.chores_due(w, WED1, PEOPLE, stays, zone=PT, person="zach", role="admin")
check("Wednesday, kids away: garbage, and recycling yes", [(c["name"], c["due"]) for c in due],
      [("Garbage", True), ("Recycling", True)])
check("at night", due[0]["at"].strftime("%H:%M"), "21:00")
due2 = W.chores_due(w, WED2, PEOPLE, stays, zone=PT, person="zach", role="admin")
check("the next Wednesday, kids here: recycling no", [(c["name"], c["due"], c["why"]) for c in due2][1],
      ("Recycling", False, "the kids are here"))
check("Thursday: nothing", W.chores_due(w, THU, PEOPLE, stays, zone=PT), [])
unk = W.chores_due(w, WED1, PEOPLE, None, zone=PT, person="household", role="household")
check("no custody to read (a shared screen): it depends, never a guess", [(c["name"], c["due"]) for c in unk],
      [("Garbage", True), ("Recycling", None)])
check("a kid sees the household's chores", len(W.chores_due(w, WED1, PEOPLE, stays, zone=PT, person="p_ava", role="kid")), 2)

print("\n── no stated time: none made up ──")
w2 = W.Bubbles(_TMP / "world2")
W.set_fact(w2, "Garbage", "schedule", "every Wednesday", "zach", "admin", etype="activity", people=PEOPLE, household=True)
g = W.chores_due(w2, WED1, PEOPLE, stays, zone=PT)
check("every Wednesday: due, untimed", [(c["name"], c["timed"]) for c in g], [("Garbage", False)])
check("the line says no time", W.chores_line(g, "Wed"), "Chores Wed: Garbage.")
check("Home says today", [c["text"] for c in H.merge_chores([], g)], ["Garbage today"])

print("\n── the line, Home, the letter ──")
check("the line", W.chores_line(due, "today (Wed Oct 7)"),
      "Chores today (Wed Oct 7): Garbage (9 PM); Recycling: yes (the kids are at their other parent's), 9 PM.")
check("without reasons (a shared screen)", W.chores_line(due2, "Wed", reasons=False),
      "Chores Wed: Garbage (9 PM); Recycling: no, 9 PM.")
merged = H.merge_chores([{"text": "Garbage night by 9 PM", "at": "9 PM"}, {"text": "Compost by 7 AM", "at": "7 AM"}], due)
check("Home: the rules first, a calendar chore not already named after",
      [c["text"] for c in merged], ["Garbage by 9 PM", "Recycling by 9 PM (the kids are at their other parent's)",
                                    "Compost by 7 AM"])
check("Home on a kids-here week", [c["text"] for c in H.merge_chores([], due2)], ["Garbage by 9 PM",
                                                                                 "No recycling tonight (the kids are here)"])
check("Home without custody", [c["text"] for c in H.merge_chores([], unk, reasons=False)],
      ["Garbage by 9 PM", "Recycling: depends on the kids' week"])
items = reflection._chores_items("2026-10-06", 1e9, chores=lambda d: W.chores_line(
    W.chores_due(w, d, PEOPLE, stays, zone=PT, person="zach", role="admin"), f"tomorrow ({d:%a %b} {d.day})"))
check("the letter: tomorrow's chores, computed", (items[0]["kind"], items[0]["title"].startswith("Chores tomorrow (Wed Oct 7): Garbage")),
      ("tomorrow", True))
check("no chores tomorrow: nothing in the letter", reflection._chores_items("2026-10-07", 1e9, chores=lambda d: None), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
