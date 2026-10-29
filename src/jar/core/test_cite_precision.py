#!/usr/bin/env python3
# gate: hermetic
"""cite_precision's report (#912): a 95% lower bound on precision, recall as cites per answer that had evidence by
surface, several passes combined, and the scorecard's line and miss. No turns, no judge.
Run: python3 scripts/test_cite_precision.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import cite_precision as cp  # noqa: E402
import scorecard  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


check("all right: the rule of three", (cp.lower_bound(30, 30), cp.lower_bound(60, 60)), (0.9, 0.95))
check("two wrong of 60: Wilson's bound, under 90%", cp.lower_bound(58, 60), 0.886)
check("nothing emitted: no bound", cp.lower_bound(0, 0), None)
check("recall by surface", cp.recall([("kb", 2), ("kb", 0), ("runbook", 1), ("runbook", 3)]),
      {"kb": {"answers": 2, "cited": 1, "per_answer": 1.0}, "runbook": {"answers": 2, "cited": 2, "per_answer": 2.0}})

tmp = Path(tempfile.mkdtemp(prefix="titan-citeprec-"))
for i, (n, ok, rk) in enumerate([(30, 30, {"kb": {"answers": 10, "cited": 6, "per_answer": 0.8}}),
                                 (32, 31, {"kb": {"answers": 12, "cited": 7, "per_answer": 0.75}})]):
    (tmp / f"p{i}.json").write_text(json.dumps({"emitted": n, "agreed": ok, "by_surface": {"kb": {"emitted": n, "agreed": ok}},
                                                "recall": rk}))
out = subprocess.run([sys.executable, str(HERE / "cite_precision.py"), "--combine", str(tmp / "p0.json"), str(tmp / "p1.json")],
                     capture_output=True, text=True, env={**os.environ, "CITE_PRECISION_LOG": str(tmp / "log.jsonl")}).stdout
r = json.loads(out)
check("passes combined", (r["emitted"], r["agreed"], r["precision"], r["by_surface"]["kb"]), (62, 61, 0.984, "61/62"))
check("…with the lower bound over the total", r["lower_bound"], cp.lower_bound(61, 62))
check("…and recall summed", r["recall"]["kb"], {"answers": 22, "cited": 13, "per_answer": 0.77})

good = {"emitted": 40, "agreed": 39, "precision": 0.975, "lower_bound": 0.87, "recall": {}}
bad = {"emitted": 40, "agreed": 36, "precision": 0.9, "lower_bound": 0.77, "recall": {}}
check("the scorecard: the last logged run, no miss at >= 95%", scorecard.cite_status([bad, good])[1], [])
check("below 95% over 10+ cites: a miss that says to turn it off",
      "CITE_INLINE off" in " ".join(scorecard.cite_status([good, bad])[1]), True)
check("a tiny run doesn't alert", scorecard.cite_status([{"emitted": 4, "agreed": 3, "precision": 0.75}])[1], [])
check("no runs: nothing", scorecard.cite_status([]), (None, []))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
