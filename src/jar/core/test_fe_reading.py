#!/usr/bin/env python3
"""fe_reading.py matches the Fundamental engine (#1399 P3): every golden vector produced by FE's own
built core (fe_golden/vectors.json, from fe_golden/generate.mjs) gives the same number here, to 1e-12.
Plus the properties the graph relies on: monotone decay, never NaN, a conserved budget, and a
deterministic top-k. Pure: no stores, no network, no clock.

Run: python3 scripts/test_fe_reading.py
"""
# gate: hermetic
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fe_reading as F  # noqa: E402

V = json.loads((Path(__file__).resolve().parent / "fe_golden" / "vectors.json").read_text())
NOW = V["now_ms"]
passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": {detail}"))


def close(a, b):
    return a == b or (math.isfinite(a) and math.isfinite(b) and abs(a - b) <= 1e-12)


def golden(name, fn):
    bad = [(c, fn(c)) for c in V[name] if not close(fn(c), c["want"])]
    check(f"{name}: {len(V[name])} vectors from FE {V['fe_commit']} match", not bad,
          f"{len(bad)} differ, first {bad[0] if bad else ''}")


print("── golden vectors (FE's own core) ──")
golden("imminence", lambda c: F.imminence(c["at"], NOW, c["horizon"]))
golden("freshness", lambda c: F.freshness(c["at"], NOW, c["half_life"]))
golden("retention", lambda c: F.retention(c["anchor"], c["since"]))
golden("phase", lambda c: F.phase(c["now"], c["period"], c["offset"]))
golden("transfer", lambda c: F.attention_transfer(F.Relationship(strength=c["strength"]), c["attention"], c["rate"]))
r = F.Relationship()
bad = []
for c in V["relationship"]:
    F.update_relationship(r, c["active"], c["tension"], c["dt"])
    if not all(close(getattr(r, k), c["want"][k]) for k in ("strength", "tension", "memory")):
        bad.append(c)
check(f"relationship: {len(V['relationship'])} steps match FE's updateRelationship", not bad, str(bad[:1]))

print("── properties the graph relies on ──")
D, H = F.DAY_MS, F.HOUR_MS
fr = [F.freshness(NOW - s * D, NOW, 7 * D) for s in range(0, 60)]
check("freshness never rises with age", all(a >= b for a, b in zip(fr, fr[1:])))
check("freshness is exactly 0.5 one half-life later", F.freshness(NOW - 7 * D, NOW, 7 * D) == 0.5)
im = [F.imminence(NOW + h * H, NOW, 7 * D) for h in range(0, 24 * 8)]
check("imminence never rises as the moment recedes; 0 at the horizon",
      all(a >= b for a, b in zip(im, im[1:])) and F.imminence(NOW + 7 * D, NOW, 7 * D) == 0.0)
check("a deeper anchor keeps a larger fraction of itself over the same time",
      F.retention(0.9, 30 * D) / 0.9 > F.retention(0.3, 30 * D) / 0.3)
odd = [float("nan"), float("inf"), -float("inf")]
vals = [F.imminence(x, NOW, D) for x in odd] + [F.freshness(x, NOW, D) for x in odd] + \
       [F.retention(x, D) for x in odd] + [F.retention(0.5, x) for x in odd] + [F.phase(x, D) for x in odd]
check("degenerate inputs never give NaN", all(math.isfinite(v) for v in vals))
check("coherence/entropy are None with no relations (FE keeps the previous value)",
      F.coherence(0, 0, 0) is None and F.entropy(0, 0, 0) is None)
check("a fully resolved set has entropy 0 and coherence 1", (F.entropy(4, 0, 4), F.coherence(4, 0, 4)) == (0.0, 1.0))
check("conflict raises entropy and pressure", F.entropy(2, 2, 4) > F.entropy(4, 0, 4)
      and F.pressure(F.entropy(2, 2, 4), 1.0) > F.pressure(F.entropy(4, 0, 4), 1.0))
shares = F.conserved_share([3, 1, 0, -2, float("nan")], 10)
check("conserved attention spends exactly the budget, in proportion; bad demands get 0",
      close(sum(shares), 10.0) and shares[:2] == [7.5, 2.5] and shares[2:] == [0.0, 0.0, 0.0])
check("no demand: all zero", F.conserved_share([0, 0], 5) == [0.0, 0.0])
check("top_k is deterministic: strongest first, ties by key, zero shares dropped",
      F.top_k({"b": 1, "a": 1, "c": 2, "z": 0}, 1.0, 3) == [("c", 0.5), ("a", 0.25), ("b", 0.25)])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
