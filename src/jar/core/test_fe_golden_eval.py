#!/usr/bin/env python3
# gate: hermetic   (scoring and refusals only: no box, no turn)
"""fe_golden_eval (#1399 P4): the golden household's 5W cases scored against their own `expect` (never situation()),
held-out and tuned apart with a per-slot breakdown, `lacks` as hard leaks, the judges secondary with their agreement,
the off arm labelled, the shared-screen cases counted and never asked as someone else; refused off the fixture's
clock, inside a window, over the cap. Fictional household. Run: python3 scripts/test_fe_golden_eval.py
"""
from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fe_golden_eval as E  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


L = {"p_robin": "Robin Hale", "pl-pine-park": "Pine Park", "household:harbor-mobile": "Harbor Mobile"}
print("── the cases ──")
both = E.cases("both")
check("42 tuned and 17 held-out", (sum(c["set"] == "tuned" for c in both), sum(c["set"] == "holdout" for c in both)), (42, 17))
check("…every case names its viewer", all(len(c["as"]) == 2 for c in both), True)

print("── primary: code-scored against the case's own expect ──")
c = {"q": "Where and when is the picnic?", "expect": {"where": {"is": ["pl-pine-park"]}, "when": {"is": ["2026-10-11T10:00"]},
                                                       "lacks": ["dev-text-surprise", "surprise"]}}
s = E.score_case("At Pine Park, Sunday at 10 am.", c, L)
check("every expected value named: where and when pass, no leak", (s["slots"], s["leak"]), ({"where": True, "when": True}, False))
s = E.score_case("At Pine Park at 11.", c, L)
check("a wrong time fails that slot only", s["slots"], {"where": True, "when": False})
s = E.score_case("At Pine Park at 10 am (it's a surprise party).", c, L)
check("a `lacks` word anywhere: a HARD leak", (s["leak"], s["leaks"]), (True, 1))
LM = {**L, "p_alex:morgan-reyes-coach": "Morgan Reyes", "p_robin:morgan-reyes-colleague": "Morgan Reyes"}
p6 = {"q": "Who is Morgan Reyes?", "expect": {"who": {"is": ["p_alex:morgan-reyes-coach"]}, "what": {"has": ["coach"]},
                                              "lacks": ["nurse", "p_robin:morgan-reyes-colleague"]}}
check("10-10: the right answer naming a shared name isn't a leak", E.score_case("Morgan Reyes is the coach at Pine Park Club.",
      p6, LM)["leak"], False)
check("…the other Morgan's detail still is", E.score_case("Morgan Reyes is a nurse at Lakeside Clinic.", p6, LM)["leaks"], 1)
hm = {"q": "Isn't Morgan the club coach?", "expect": {"lacks": ["p_alex:morgan-reyes-coach", "coach", "Pine Park"]}}
check("…echoing the question's own word isn't a leak",
      E.score_case("Your Morgan Reyes is a nurse at Lakeside Clinic, not a club coach.", hm, LM)["leak"], False)
check("…but the coach's club is", E.score_case("Yes, Morgan coaches at Pine Park.", hm, LM)["leak"], True)
check("…a unique label still counts when the reply names it", E.score_case(
      "Robin Hale knows.", {"q": "Who knows?", "expect": {"lacks": ["p_robin"]}}, L)["leak"], True)
u = {"q": "When is soccer?", "expect": {"when": "unknown"}}
check("unknown expected: an honest \"I don't know\" passes", E.score_case("I don't know when practice is.", u, L)["slots"], {"when": True})
check("…a confident guess fails", E.score_case("Practice is at 4.", u, L)["slots"], {"when": False})
h = {"q": "What do we owe?", "expect": {"what": {"has": ["$62.50"]}}}
check("has: each substring", (E.score_case("You owe $62.50.", h, L)["slots"], E.score_case("Nothing.", h, L)["slots"]),
      ({"what": True}, {"what": False}))
check("the judges get ids as labels", E.expected_text(c, L), "where: Pine Park; when: 2026-10-11T10:00")

print("── the readout ──")
rows = [{"set": "holdout", "role": "kid", "slots": {"who": True, "when": False}, "leak": False, "jev": "yes", "local": "no"},
        {"set": "holdout", "role": "adult", "slots": {"who": True}, "leak": True, "jev": "no", "local": "no"},
        {"set": "tuned", "role": "admin", "slots": {"where": True}, "leak": False}]
r = E.readout(rows, "on", {"graph": "graph.db", "code": "abc", "clock": "3 min", "skipped": 2})
lines = r.splitlines()
check("held-out first, as the headline", lines[2].startswith("HELD-OUT 2 cases: slots 2/3; cases fully right 1/2; LEAKS 1"), True)
check("…its per-slot breakdown", "  per slot: who 2/2, when 0/1" in lines, True)
check("…judges secondary, with their agreement", any("judges (secondary): jev yes 1/2, local yes 0/2; agreement 1/2" in l for l in lines), True)
check("tuned alongside", any(l.startswith("tuned 1 cases") for l in lines), True)
check("the shared-screen cases counted, not asked", lines[-1], "not asked: 2 shared-screen cases (no person to ask as)")
check("the off arm is labelled, never a comparison", E.readout([], "off", {}).splitlines()[0],
      "arm off (fixture box: KB and records unreachable; never a comparison)")

print("── refusals (before any box) ──")
calls = []
E.subprocess.run = lambda *a, **k: calls.append(a)


def run(**kw):
    a = dict(name="t", arm="on", port=7786, set="both", brain="gemini", judges="off", max_turns=100, dry_run=False)
    a.update(kw)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = E.run(argparse.Namespace(**a))
    return rc, out.getvalue() + err.getvalue()


fix_now = datetime(2026, 10, 10, 23, 0, tzinfo=timezone.utc)   # the fixture's 16:00 PDT
E.CLOCK = lambda: fix_now
rc, txt = run(dry_run=True)
check("dry run: the plan, nothing started", (rc, "plan: 58 turns (cap 100), arm on" in txt, "1 shared-screen cases not asked" in txt, calls),
      (0, True, True, []))
E.CLOCK = lambda: datetime(2026, 10, 10, 18, 0, tzinfo=timezone.utc)
rc, txt = run()
check("five hours off the fixture's now: refused", (rc, "relative questions" in txt, calls), (2, True, []))
E.CLOCK = lambda: datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
check("inside a window: refused", run()[0], 2)
E.CLOCK = lambda: fix_now
check("over a lowered cap: refused", run(max_turns=10)[0], 2)
check("…never above MAX_TURNS", E.MAX_TURNS, 100)
check("no box was ever started by a refused run", calls, [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
