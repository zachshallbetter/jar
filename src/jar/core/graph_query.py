#!/usr/bin/env python3
"""Multi-hop queries over the relational FE graph, time-weighted with Fundamental's kernels (#1403; spec
docs/relational-fe-graph.md §6, §8; paper docs/papers/32-relational-fe-graph.md §5.1).

It reads ONLY through graph_sync.visible_nodes / visible_edges, so every query sees exactly the viewer's subgraph:
a hidden node can't appear in a result or bridge two visible ones (privacy by construction, as in situation()).

An edge's weight is what the world model's claims and time make of it:
  w(e) = trust(state) · conf · time(e)
  - trust: verified 1.0, accepted 0.7 (candidate and rejected edges are never read: graph_sync's default states)
  - conf: the producer's supplied confidence (never computed here)
  - time: standing relations (parent_of, member_of, knows, sees, same-person facts) don't age: 1.0;
    dated evidence ages by freshness (fe_reading.freshness, a half-life per edge type), and an edge whose
    valid_to has passed is 0; an upcoming event's edges are lifted by imminence: max(freshness, imminence)
A path's weight is the product of its edges' weights (a chain is as strong as its links together), so a long or
stale chain ranks below a short fresh one. Relationship strength between two nodes is fe_reading.retention over how
often and how recently they were tied by dated edges (anchor = recurrences / 5, capped at 1).

Pure reads: no store is written, no model is called. Times are the caller's `now` (household-zone ISO).
"""
from __future__ import annotations

import heapq
from datetime import datetime
from pathlib import Path
from typing import Any

import fe_reading as F
import graph_sync as G

DAY_MS = F.DAY_MS
TRUST = {"verified": 1.0, "accepted": 0.7}
STANDING = frozenset({"parent_of", "member_of", "knows", "sees", "located_at_home"})
# how fast dated evidence fades, by edge type (days): conversation fades fast, appointments and bills slower
HALF_LIFE_D = {"from": 14, "to": 14, "mentions": 14, "replies_to": 14, "related": 30, "about": 30,
               "attends": 30, "organises": 30, "at": 30, "located_at": 2, "decided": 60, "depends_on": 60}
HORIZON_MS = 14 * DAY_MS


def _ms(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso))
    except ValueError:
        try:
            d = datetime.fromisoformat(str(iso)[:10])
        except ValueError:
            return None
    if d.tzinfo is None:   # a bare day: read at noon, so a date-only fact sits mid-day whatever the zone
        d = d.replace(hour=12)
        return d.timestamp() * 1000
    return d.timestamp() * 1000


def edge_time(e: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> float | None:
    """The newest time the edge stands on: its sources' observed_at, its valid_from, and an event end's start."""
    ts = [_ms(p.get("observed_at")) for p in e.get("prov") or []] + [_ms(e.get("valid_from"))]
    for end in (e["a"], e["b"]):
        n = nodes.get(end) or {}
        a = n.get("attrs") or {}
        ts.append(_ms(a.get("start") or a.get("observed_at") or a.get("due")))
    ts = [t for t in ts if t is not None]
    return max(ts) if ts else None


def edge_weight(e: dict[str, Any], nodes: dict[str, dict[str, Any]], now_ms: float) -> float:
    trust = TRUST.get(e.get("state"), 0.0)
    if not trust:
        return 0.0
    conf = float(e.get("conf") if e.get("conf") is not None else 1.0)
    vt = _ms(e.get("valid_to"))
    if vt is not None and vt < now_ms:
        return 0.0
    if e["type"] in STANDING or (e["type"] == "located_at" and not e.get("valid_from")):
        return trust * conf                                     # a standing fact: home, parents, membership
    t = edge_time(e, nodes)
    if t is None:
        return trust * conf * 0.5                                # undated evidence: half weight, never zero
    hl = HALF_LIFE_D.get(e["type"], 30) * DAY_MS
    # the past fades (freshness); the future approaches (imminence): an event a day out outweighs one twelve out
    timing = F.imminence(t, now_ms, HORIZON_MS) if t > now_ms else F.freshness(t, now_ms, hl)
    return trust * conf * timing


def _graph(viewer: str | None, role: str | None, path: Path | None, now_ms: float, types: set[str] | None):
    nodes = G.visible_nodes(viewer, role, path)
    adj: dict[str, list[tuple[str, float, dict[str, Any]]]] = {}
    for e in G.visible_edges(viewer, role, path, types=types):
        w = edge_weight(e, nodes, now_ms)
        if w <= 0:
            continue
        adj.setdefault(e["a"], []).append((e["b"], w, e))
        adj.setdefault(e["b"], []).append((e["a"], w, e))     # traversal is undirected; each hop keeps its edge
    return nodes, adj


def _hop(e: dict[str, Any], to: str) -> dict[str, Any]:
    """One step of a walk: the edge as stored (a, type, b) plus `to`, the node the walk arrived at."""
    return {"a": e["a"], "type": e["type"], "b": e["b"], "to": to, "state": e["state"], "rung": e["rung"],
            "why": e["why"]}


def neighborhood(viewer: str | None, role: str | None, node: str, *, now: str, hops: int = 2, k: int = 10,
                 types: set[str] | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """The k strongest nodes within `hops` of `node` for this viewer, each with its best path (the product of edge
    weights) and that path's why-chain. Empty when the viewer can't see `node`."""
    now_ms = _ms(now)
    nodes, adj = _graph(viewer, role, path, now_ms, types)
    if node not in nodes:
        return []
    best: dict[str, tuple[float, list[dict[str, Any]]]] = {node: (1.0, [])}
    heap = [(-1.0, 0, node)]
    while heap:
        negw, depth, cur = heapq.heappop(heap)
        w = -negw
        if w < best.get(cur, (0.0,))[0] or depth >= hops:
            continue
        for nxt, ew, e in adj.get(cur, ()):
            nw = w * ew
            if nw > best.get(nxt, (0.0,))[0] and nxt in nodes:
                best[nxt] = (nw, best[cur][1] + [_hop(e, nxt)])
                heapq.heappush(heap, (-nw, depth + 1, nxt))
    out = [{"id": n, "kind": nodes[n]["kind"], "label": nodes[n].get("label") or "", "weight": round(w, 6),
            "path": p, "rung": _weakest(p)} for n, (w, p) in best.items() if n != node]
    out.sort(key=lambda r: (-r["weight"], r["id"]))
    return out[:k]


def paths(viewer: str | None, role: str | None, a: str, b: str, *, now: str, max_hops: int = 3, k: int = 3,
          types: set[str] | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Up to k simple paths from a to b within max_hops through the viewer's subgraph, strongest first. Each path is
    its hops (with why and rung), its weight and its weakest rung. Empty when either end is hidden or unreachable."""
    now_ms = _ms(now)
    nodes, adj = _graph(viewer, role, path, now_ms, types)
    if a not in nodes or b not in nodes or a == b:
        return []
    found: list[tuple[float, list[dict[str, Any]]]] = []

    def walk(cur: str, w: float, seen: set[str], trail: list[dict[str, Any]]) -> None:
        if len(trail) > max_hops:
            return
        if cur == b:
            found.append((w, list(trail)))
            return
        for nxt, ew, e in adj.get(cur, ()):
            if nxt in seen or nxt not in nodes:
                continue
            seen.add(nxt)
            trail.append(_hop(e, nxt))
            walk(nxt, w * ew, seen, trail)
            trail.pop()
            seen.discard(nxt)

    walk(a, 1.0, {a}, [])
    found.sort(key=lambda f: (-f[0], len(f[1]), [h["b"] for h in f[1]]))
    return [{"weight": round(w, 6), "hops": t, "rung": _weakest(t)} for w, t in found[:k]]


def relationship(viewer: str | None, role: str | None, a: str, b: str, *, now: str,
                 path: Path | None = None) -> dict[str, Any]:
    """How alive the tie between a and b is for this viewer: fe_reading.retention over the dated edges that join
    them directly or through one shared item/event (co-occurrence). anchor = min(1, n/5); since = the newest one.
    {strength 0..1, ties n, last ISO|None}; strength 0 when either end is hidden."""
    now_ms = _ms(now)
    nodes = G.visible_nodes(viewer, role, path)
    if a not in nodes or b not in nodes:
        return {"strength": 0.0, "ties": 0, "last": None}
    edges = G.visible_edges(viewer, role, path)
    near: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        near.setdefault(e["a"], []).append(e)
        near.setdefault(e["b"], []).append(e)
    times: list[float] = []
    for e in near.get(a, []):
        other = e["b"] if e["a"] == a else e["a"]
        if other == b:
            t = edge_time(e, nodes)
            if t is not None:
                times.append(t)
            continue
        if (nodes.get(other) or {}).get("kind") in ("item", "event") and any(
                (f["a"] == b or f["b"] == b) for f in near.get(other, [])):
            t = edge_time(e, nodes)
            if t is not None:
                times.append(t)
    times = sorted({t for t in times if t <= now_ms})
    if not times:
        return {"strength": 0.0, "ties": 0, "last": None}
    anchor = min(1.0, len(times) / 5)
    last = times[-1]
    return {"strength": round(F.retention(anchor, now_ms - last), 6), "ties": len(times),
            "last": datetime.fromtimestamp(last / 1000).astimezone().isoformat(timespec="minutes")}


def _weakest(hops: list[dict[str, Any]]) -> str | None:
    order = ["observed", "attributed", "explained", "predicted"]
    rungs = [h.get("rung") for h in hops if h.get("rung") in order]
    return max(rungs, key=order.index) if rungs else None
