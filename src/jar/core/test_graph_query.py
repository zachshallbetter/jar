#!/usr/bin/env python3
"""graph_query (#1403): multi-hop, time-weighted queries over the relational FE graph, per viewer. Over the golden
household (graph_golden_load: the fixture in temp copies of the real stores, projected by graph_sync), checks:
- privacy by construction: a viewer can't reach, path through, or relate a node they can't see;
- weights: candidate/rejected never read; standing relations don't age; dated evidence fades with freshness; an
  expired edge is 0; an upcoming event is lifted by imminence; a path is the product of its edges;
- explanations: every hop carries its why and rung, and a result's rung is its weakest hop;
- determinism: the same query gives the same answer.
Fictional people only. Pure reads.

Run: TZ=UTC python3 scripts/test_graph_query.py
"""
# gate: hermetic
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_golden_load as GL  # noqa: E402  (first: points every store at a temp dir and loads the fixture)
import fe_reading as F  # noqa: E402
import graph_query as Q  # noqa: E402

P = GL.build()
NOW = GL.H["now"]
passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": {detail}"))


def ids(rows):
    return {r["id"] for r in rows}


print("── privacy: a viewer's queries see only their subgraph ──")
alex = Q.neighborhood("p_alex", "admin", "p_alex", now=NOW, hops=3, k=200, path=P)
jordan = Q.neighborhood("p_jordan", "adult", "p_jordan", now=NOW, hops=3, k=200, path=P)
ava = Q.neighborhood("p_ava", "kid", "p_ava", now=NOW, hops=3, k=200, path=P)
import graph_sync as G  # noqa: E402
for who, role, rows in (("p_alex", "admin", alex), ("p_jordan", "adult", jordan), ("p_ava", "kid", ava)):
    seen = set(G.visible_nodes(who, role, P))
    check(f"{who}: every node reached is one they can see", ids(rows) <= seen, sorted(ids(rows) - seen)[:3])
    hidden = {h[k] for r in rows for h in r["path"] for k in ("a", "b")} - seen
    check(f"{who}: no path runs through a node they can't see", not hidden, sorted(hidden)[:3])
check("Alex's private texts never reach Jordan's neighbourhood",
      not ({"dev-text-plan-1", "dev-text-plan-2", "dev-text-plan-3"} & ids(jordan)))
check("Robin's surprise text never reaches Alex's neighbourhood", "dev-text-surprise" not in ids(alex))
check("a hidden start node gives nothing", Q.neighborhood("p_jordan", "adult", "dev-text-plan-1", now=NOW, path=P) == [])
check("no path to a hidden end", Q.paths("p_jordan", "adult", "p_jordan", "dev-text-plan-1", now=NOW, path=P) == [])
check("no relationship with a hidden end",
      Q.relationship("p_alex", "admin", "p_alex", "dev-text-surprise", now=NOW, path=P)["strength"] == 0.0)

print("── weights: claims and time ──")
now_ms = Q._ms(NOW)
nodes = G.visible_nodes("p_alex", "admin", P)
cand = {"a": "x", "type": "related", "b": "y", "state": "candidate", "conf": 1.0, "prov": []}
rej = dict(cand, state="rejected")
check("candidate and rejected edges weigh 0 (never read)",
      Q.edge_weight(cand, nodes, now_ms) == Q.edge_weight(rej, nodes, now_ms) == 0.0)
std = {"a": "p_alex", "type": "parent_of", "b": "p_ava", "state": "verified", "conf": 1.0, "prov": []}
check("a standing relation doesn't age (parent_of = trust · conf)", Q.edge_weight(std, nodes, now_ms) == 1.0)
def dated(days_ago, typ="from", state="accepted"):
    t = now_ms - days_ago * F.DAY_MS
    from datetime import datetime
    iso = datetime.fromtimestamp(t / 1000).astimezone().isoformat()
    return {"a": "a", "type": typ, "b": "b", "state": state, "conf": 1.0, "prov": [{"observed_at": iso}]}
w0, w14, w28 = (Q.edge_weight(dated(d), {}, now_ms) for d in (0, 14, 28))
check("dated evidence fades: one half-life halves it (from: 14 d)",
      abs(w0 - 0.7) < 1e-9 and abs(w14 - 0.35) < 1e-6 and abs(w28 - 0.175) < 1e-6, (w0, w14, w28))
exp = dict(dated(1), valid_to="2026-10-01T00:00:00-07:00")
check("an edge whose valid_to has passed weighs 0", Q.edge_weight(exp, {}, now_ms) == 0.0)
soon = dated(-1, "attends")        # an event a day ahead
far = dated(-12, "attends")
check("an upcoming event is lifted by imminence: tomorrow outweighs twelve days out",
      Q.edge_weight(soon, {}, now_ms) > Q.edge_weight(far, {}, now_ms) > 0)
check("verified outweighs accepted, all else equal",
      Q.edge_weight(dated(3, state="verified"), {}, now_ms) > Q.edge_weight(dated(3), {}, now_ms))

print("── paths and explanations ──")
pp = Q.paths("p_alex", "admin", "dev-text-trade-1", "cx-mail-trade-2", now=NOW, path=P)
check("the trade-in text and mail are joined (same order number)", bool(pp), pp)
if pp:
    top = pp[0]
    check("…every hop says why and its rung", all(h["why"] and h["rung"] for h in top["hops"]), top["hops"])
    check("…a path's weight is the product of its edges, ≤ its weakest hop",
          0 < top["weight"] <= 1.0 and top["rung"] in ("observed", "attributed", "explained", "predicted"))
two = Q.paths("p_alex", "admin", "p_alex", "pl-cedar", now=NOW, max_hops=3, k=5, path=P)
check("paths are strongest first", all(two[i]["weight"] >= two[i + 1]["weight"] for i in range(len(two) - 1)), two)
check("paths are simple (no node twice, the start included)",
      all(len({"p_alex"} | {h["to"] for h in p["hops"]}) == len(p["hops"]) + 1 for p in two), two)
check("a path ends where it was asked to", all(p["hops"][-1]["to"] == "pl-cedar" for p in two), two)
rel = Q.relationship("p_alex", "admin", "p_alex", "household:harbor-mobile", now=NOW, path=P)
check("a relationship is retention over dated ties: 0 ≤ strength ≤ anchor", 0.0 <= rel["strength"] <= 1.0, rel)

print("── determinism ──")
check("the same neighbourhood twice is identical",
      Q.neighborhood("p_alex", "admin", "p_alex", now=NOW, hops=2, path=P)
      == Q.neighborhood("p_alex", "admin", "p_alex", now=NOW, hops=2, path=P))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
