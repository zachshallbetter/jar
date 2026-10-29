#!/usr/bin/env python3
# gate: hermetic
"""The reranker A/B harness (#1622, kb_rerank_ab.py): both arms are scored over the same cases with kb_bank's own score.
The cases B gains and loses, and the ones its floor empties, are named by id. The verdict line carries counts and
timings, never a question. Fake arms and made-up cases; no KB, no reranker. Run: python3 scripts/test_kb_rerank_ab.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-rerank-ab-"))
os.environ["KB_BANK"] = str(_TMP / "bank.jsonl")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_rerank_ab as AB  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


CASES = [{"id": f"c{i}", "q": f"When is the Hale piano recital number {i}?", "kind": "facts", "zone": "facts",
          "expect": [f"note-{i}"], "owner": None} for i in range(6)]


def arm_a(q, c):   # today: finds c0–c3
    i = int(c["id"][1:])
    return [{"source_id": f"note-{i}"}] if i < 4 else [{"source_id": "x"}]


def arm_b(q, c):   # reranked: finds c0, c1, c4; loses c2, c3; the floor empties c5
    i = int(c["id"][1:])
    if i == 5:
        return []
    return [{"source_id": f"note-{i}"}] if i in (0, 1, 4) else [{"source_id": "y"}]


ticks = iter([0.0, 6.0, 18.0])
r = AB.compare(CASES, arm_a, arm_b, lambda o: "admin", clock=lambda: next(ticks))
check("both arms over the same cases", (r["cases"], r["a"]["found"], r["b"]["found"]), (6, 4, 3))
check("what B gains, loses, and empties, by case id", (r["b_gains"], r["b_losses"], r["b_empty_cases"], r["b"]["empty"]),
      (["c4"], ["c2", "c3"], ["c5"], 1))
check("each arm's time", (r["a"]["seconds"], r["b"]["seconds"]), (6.0, 12.0))
v = AB.verdict(r)
check("the verdict line: counts and timings", v, "found@3: A 4/6 (67%), B 3/6 (50%); B gains 1, loses 2, empty 1; 2.00 s vs 1.00 s a case")
check("…never a question", any(c["q"] in v or "piano" in v for c in CASES), False)
check("…nor in the whole report", "piano" in str(r), False)

import kb_rank_sim as SIM  # noqa: E402
cands = [{"chunk_id": f"k{i}", "source_id": f"s{i}", "score": 0.9 - i / 10, "metadata": {"title": f"T{i}"}} for i in range(5)]
fake = lambda q, hits: sorted([dict(h, rerank_score={"k0": -3.5, "k1": -1.0, "k2": -2.0}.get(h["chunk_id"], -9)) for h in hits],  # noqa: E731
                              key=lambda h: -h["rerank_score"])
out = SIM.rerank_reorder("q", cands, rerank_fn=fake, floor=-2.8, fetch=3)
check("the sim's reranker variant: the top 3 reranked, under the floor dropped, nothing past the top 3 (as kb_retrieve)",
      [c["chunk_id"] for c in out], ["k1", "k2"])
check("…its kept hits carry the reranker's score", out[0]["score"], -1.0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
