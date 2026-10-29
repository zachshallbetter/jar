#!/usr/bin/env python3
"""A plan the graph can see completed becomes ONE event proposal (#1396; FE graph P4, spec §8 "Proactive use",
§10 P4). Behind AGENT_FE_PROACTIVE (off by default).

For one viewer (an admin or adult; a kid or a shared screen raises nothing), over their visible subgraph only (the
graph read contract: visible_nodes / visible_edges, verified + accepted edges, never candidates):

  1. Clusters  the viewer's own items joined by related / replies_to edges.
  2. Complete  a cluster is a plan when its edges give WHO (persons on from / to / mentions / attends, the viewer
               excluded), exactly one WHERE (a place on mentions / at) and a WHEN (an item's `when`: a date hook with a
               time of day, #1436). An org isn't a place; a day without a time isn't a when.
  3. Covered   no visible event (a calendar event or an existing proposal) within ±2 h whose place or people overlap,
               and no existing proposal of the viewer's with the same thread_ref or sig. The Mac's own proposal for the
               same plan covers it, so the graph never doubles a card.
  4. Raise     event_proposals.put(owner, body): the Mac's own body shape (title, start, place, participants, others,
               confidence, thread_ref, sig_id), body-free. The existing pass makes the owner-only card; Add → leave-by
               already works.
  5. Snapshot  the plan's context: a situation() bundle (who / where / when, each with the edges it stands on, the
               items cited, the weakest rung).

What the graph holds today (10-10, P2 on main): a plan in texts isn't complete in the graph (texts give no time, no
place and no direction until #1436), so on the golden household this raises nothing new: the Mac's proposal covers it.

  fe_proactive.run(viewer, role, graph, con, now=…, apply=False) → [plan]   (apply and the flag both needed to write)
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from typing import Any

from situation import Bundle, _local, _native

FLAG = "AGENT_FE_PROACTIVE"
ROLES = ("admin", "adult")
COVER_S = 2 * 3600
CONFIDENCE = 0.7          # a plan complete in code from typed/accepted edges; proposals need ≥ 0.6 to card
ITEM_LINKS = ("related", "replies_to")
WHO_EDGES = ("from", "to", "mentions", "attends")
WHERE_EDGES = ("mentions", "at")


def enabled() -> bool:
    return os.environ.get(FLAG, "0") == "1"


def _clusters(items: set[str], edges: list[dict[str, Any]]) -> list[list[str]]:
    parent = {i: i for i in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in edges:
        if e["type"] in ITEM_LINKS and e["a"] in items and e["b"] in items:
            parent[find(e["a"])] = find(e["b"])
    groups: dict[str, list[str]] = {}
    for i in items:
        groups.setdefault(find(i), []).append(i)
    return [sorted(g) for g in groups.values()]


def _hex16(*parts: str) -> str:
    return hashlib.sha1("\0".join(parts).encode()).hexdigest()[:16]


def detect(viewer: str, role: str, graph: Any, *, zone: Any, now: datetime,
           proposals: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The viewer's completed, uncovered plans: [{sig_id, thread_ref, body, items, snapshot}]."""
    if role not in ROLES:
        return []
    nodes = graph.visible_nodes(viewer, role)
    edges = graph.visible_edges(viewer, role)          # verified + accepted only: a candidate never completes a plan
    me = viewer if viewer in nodes else f"person:{viewer}"
    mine = {nid for nid, n in nodes.items() if n["kind"] == "item" and n.get("owner") == viewer}
    by_a: dict[str, list[dict]] = {}
    by_b: dict[str, list[dict]] = {}
    for e in edges:
        by_a.setdefault(e["a"], []).append(e)
        by_b.setdefault(e["b"], []).append(e)
    known = {(p.get("thread_ref"), p.get("sig_id")) for p in proposals or []}
    known_refs = {p.get("thread_ref") for p in proposals or []} | {p.get("sig_id") for p in proposals or []}
    events = [nid for nid, n in nodes.items() if n["kind"] == "event"]
    out = []
    for group in _clusters(mine, edges):
        who_e = [e for i in group for e in by_a.get(i, []) if e["type"] in WHO_EDGES
                 and (nodes.get(e["b"]) or {}).get("kind") == "person" and e["b"] != me]
        where_e = [e for i in group for e in by_a.get(i, []) if e["type"] in WHERE_EDGES
                   and (nodes.get(e["b"]) or {}).get("kind") == "place"]
        places = sorted({e["b"] for e in where_e})
        # an item's stated times (graph_edges' attrs.when: a list of "YYYY-MM-DDTHH:MM", #1450; a single string too)
        whens = []
        for i in group:
            w = nodes[i]["attrs"].get("when")
            for v in (w if isinstance(w, list) else [w]):
                t = _local(v, zone) if isinstance(v, str) and "T" in v else None
                if t:
                    whens.append((t, i))
        if not who_e or len(places) != 1 or not whens:
            continue
        start, when_item = max(whens)                    # the latest time said in the thread is the agreed one
        if start < now or start > now + timedelta(days=60):
            continue
        people = sorted({e["b"] for e in who_e} | ({me} if me in nodes else set()))
        place = places[0]
        covered = False
        for ev in events:
            t = _local((nodes[ev].get("attrs") or {}).get("start"), zone)
            if not t or abs((t - start).total_seconds()) > COVER_S:
                continue
            ev_places = {e["b"] for e in by_a.get(ev, []) if e["type"] == "at"}
            ev_people = {e["a"] for e in by_b.get(ev, []) if e["type"] in ("attends", "organises")}
            if place in ev_places or (set(people) & ev_people):
                covered = True
                break
        sig = _hex16("fe-plan", viewer, *group)
        ref = _hex16("fe-thread", viewer, group[0])
        if covered or sig in known_refs or ref in known_refs or (ref, sig) in known:
            continue
        household = [p for p in people if (nodes.get(p) or {}).get("attrs", {}).get("role") in ("admin", "adult", "kid")]
        label = nodes[place]["label"]
        body = {"kind": "event_proposal", "title": f"Plan at {label}"[:60], "start": start.isoformat(), "end": None,
                "place": label, "participants": [_native(p) for p in household], "others": len(people) - len(household),
                "confidence": CONFIDENCE, "thread_ref": ref, "sig_id": sig}
        snap = Bundle()
        snap.put("who", [_native(e["b"]) for e in who_e], who_e)
        snap.put("where", [_native(place)], where_e)
        snap.put("when", [start.strftime("%Y-%m-%dT%H:%M")])
        snap.cite_item(when_item)
        out.append({"sig_id": sig, "thread_ref": ref, "body": body, "items": group,
                    "snapshot": snap.out({"who", "where", "when"})})
    return out


def run(viewer: str, role: str, graph: Any, con: Any, *, zone: Any, now: datetime, apply: bool = False) -> list[dict]:
    """Detect, and (with the flag on and apply) raise each plan once through event_proposals."""
    import event_proposals as ep
    ep.ensure(con)
    existing = [{"thread_ref": r[0], "sig_id": r[1]} for r in
                con.execute("SELECT thread_ref, sig_id FROM event_proposals WHERE owner=?", (viewer,))]
    plans = detect(viewer, role, graph, zone=zone, now=now, proposals=existing)
    if apply and enabled():
        for p in plans:
            ep.put(viewer, p["body"], con, now=now)
    return plans
