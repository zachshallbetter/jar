#!/usr/bin/env python3
# gate: hermetic
"""Chronic eval cases become issues (#1517): streaks, owners, the last reasons, create/update/close through a fake gh. Fictional cases and temp files; nothing reaches GitHub. Run: python3 scripts/test_eval_chronic_report.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))

import eval_chronic_report as R  # noqa: E402

H = [
    {"ts": "2030-01-01T03:50", "real": ["pie-bake", "lamp-on"], "reasons": {"pie-bake": "oven left off", "lamp-on": "no lamp"}},
    {"ts": "2030-01-02T03:50", "real": ["pie-bake", "lamp-on"], "reasons": {"pie-bake": "oven still off"}},
    {"ts": "2030-01-03T03:50", "real": ["pie-bake"], "reasons": {"pie-bake": "oven cold"}},
    {"ts": "2030-01-04T03:50", "real": ["pie-bake", "kite-fly"], "reasons": {"pie-bake": "oven cold again", "kite-fly": "no wind"}},
]
OWN = {"default": "coordination", "prefixes": {"pie": "kb", "pie-b": "box"}}

print("── streaks ──")
check("pie-bake failed 4 nights running", R.streak(H, "pie-bake"), 4)
check("lamp-on's streak was broken", R.streak(H, "lamp-on"), 0)
check("only a case failing tonight with 3+ nights is chronic", R.chronic_cases(H), ["pie-bake"])
check("legacy 'failing' lines count", R.chronic_cases([{"failing": ["a"]}] * 3), ["a"])
check("no history, nothing chronic", R.chronic_cases([]), [])

print("\n── the issue ──")
iss = R.build_issue(H, "pie-bake", OWN)
check("title and labels", (iss["title"], iss["labels"]), ("Eval chronic: pie-bake", ["eval-chronic"]))
check("body: nights, first, last", all(s in iss["body"] for s in ("nights failing: 4", "first night: 2030-01-01", "last night: 2030-01-04")), True)
check("body: owner by longest prefix", "owner: box" in iss["body"], True)
check("body: the last 3 reasons, newest first",
      ("oven left off" not in iss["body"], iss["body"].index("oven cold again") < iss["body"].index("oven still off")), (True, True))
check("owner default", R.owner_of("zzz-1", OWN), "coordination")
check("shipped owners file: long-chat is box, unknown is coordination", (R.owner_of("long-chat-x"), R.owner_of("zzz")),
      ("box", "coordination"))

print("\n── gh: create, update, close ──")
calls = []
open_issues = []


def fake_gh(args):
    calls.append(args)
    return json.dumps(open_issues) if args[:2] == ["issue", "list"] else ""


R.sync(H, gh=fake_gh, owners=OWN)
check("a new chronic case is created, titled exactly, with the label",
      [(c[1], c[3], c[-1]) for c in calls if c[1] == "create"], [("create", "Eval chronic: pie-bake", "eval-chronic")])
calls.clear()
open_issues[:] = [{"number": 7, "title": "Eval chronic: pie-bake"}, {"number": 8, "title": "Eval chronic: lamp-on"},
                  {"number": 9, "title": "Something else"}]
R.sync(H, gh=fake_gh, owners=OWN)
check("an existing issue is edited, not duplicated", [c[2] for c in calls if c[1] == "edit"], ["7"])
check("lamp-on has not passed 3 nights: stays open", [c for c in calls if c[1] == "close"], [])
calls.clear()
R.sync(H + [{"ts": "5", "real": []}, {"ts": "6", "real": []}, {"ts": "7", "real": []}], gh=fake_gh, owners=OWN)
check("3 passing nights close both, and never the unrelated issue", sorted(c[2] for c in calls if c[1] == "close"), ["7", "8"])
check("recovered needs 3 passing nights", (R.recovered(H[:2], "x"), R.recovered(H[-3:], "zzz"), R.recovered(H, "pie-bake")),
      (False, True, False))

print("\n── CLI reads HOUSEHOLD_EVAL_HISTORY ──")
import io  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "h.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in H) + "\nnot json\n")
    os.environ["HOUSEHOLD_EVAL_HISTORY"] = str(p)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = R.main([])
    check("prints JSON for the chronic case", (rc, [i["title"] for i in json.loads(buf.getvalue())]), (0, ["Eval chronic: pie-bake"]))
    os.environ["HOUSEHOLD_EVAL_HISTORY"] = str(Path(d) / "missing.jsonl")
    buf = io.StringIO()
    with redirect_stdout(buf):
        R.main([])
    check("a missing history is an empty list", json.loads(buf.getvalue()), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
