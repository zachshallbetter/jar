#!/usr/bin/env python3
# gate: hermetic   (a synthetic graph through the read contract, temp stores; no model, no network)
"""FE graph P4 (#1396): a plan complete in the viewer's graph (who, one place, a time) with nothing covering it raises
ONE event proposal through event_proposals, with situation()'s bundle as its snapshot; never a second card for one
plan, never for a kid or a shared screen, never from candidate edges, nothing written with the flag off.

The plan cases run on a small synthetic graph built here through the same contract (visible_nodes / visible_edges),
shaped as the graph will be once #1436 lands (a text's time of day as an item's `when`, a mention of a place, a
directed sender). The golden household (P2's real projection) is checked too, for the honest number today: nothing new,
since its plan in texts is only complete as the Mac's own proposal, which covers it. Fictional people and places.
Run: python3 scripts/test_fe_proactive.py
"""
from __future__ import annotations

import io
import contextlib
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.pop("AGENT_FE_PROACTIVE", None)
import fe_proactive as P  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 10, 9, 0, tzinfo=ZONE)
ROLES = {"p_alex": "admin", "p_robin": "adult", "p_ava": "kid", "screen": "household"}


class Synth:
    """The graph read contract over in-memory rows: a node is visible to its owner, or by audience (household: all;
    adults: admin/adult); an edge when both ends are and its state is asked for (default verified + accepted)."""

    def __init__(self, nodes, edges):
        self.nodes, self.edges = nodes, edges

    def _sees(self, viewer, role, n):
        a = n.get("audience") or "private"
        return n.get("owner") in (None, viewer) and a != "private" or n.get("owner") == viewer or a == "household" \
            or (a == "adults" and role in ("admin", "adult"))

    def visible_nodes(self, viewer, role, path=None):
        return {k: v for k, v in self.nodes.items() if self._sees(viewer, role, v)}

    def visible_edges(self, viewer, role, path=None, *, a=None, b=None, types=None, states=None):
        vis = self.visible_nodes(viewer, role)
        st = set(states or ("verified", "accepted"))
        return [e for e in self.edges if e["a"] in vis and e["b"] in vis and e["state"] in st
                and (not types or e["type"] in types) and (a is None or e["a"] == a) and (b is None or e["b"] == b)]


def node(nid, kind, label, owner=None, audience="household", **attrs):
    return nid, {"id": nid, "kind": kind, "label": label, "owner": owner, "audience": audience, "attrs": attrs}


def edge(a, t, b, state="accepted", rung="attributed", src="texts", ref=None):
    return {"a": a, "type": t, "b": b, "plane": "typed", "state": state, "rung": rung, "conf": 1.0,
            "prov": [{"source": src, "ref": ref or a}]}


def household(extra_nodes=(), extra_edges=()):
    ns = dict([node("p_alex", "person", "Alex Quarry", role="admin"), node("p_robin", "person", "Robin Quarry", role="adult"),
               node("p_ava", "person", "Ava Quarry", role="kid"), node("pl-park", "place", "Pine Park"),
               node("pl-cafe", "place", "Lantern Café"),
               # Alex's thread with Robin: "Sat 10:00 at Pine Park?" / "Yes, bring Ava"
               node("dev-t1", "item", "Text", owner="p_alex", audience="private", when="2026-10-11T10:00:00-07:00"),
               node("dev-t2", "item", "Text", owner="p_alex", audience="private")])
    ns.update(dict(extra_nodes))
    es = [edge("dev-t1", "from", "p_robin"), edge("dev-t1", "mentions", "pl-park"), edge("dev-t2", "replies_to", "dev-t1",
          state="verified", rung="observed"), edge("dev-t2", "mentions", "p_ava")] + list(extra_edges)
    return Synth(ns, es)


def run(g, viewer, apply=False, con=None):
    con = con or sqlite3.connect(":memory:")
    return P.run(viewer, ROLES[viewer], g, con, zone=ZONE, now=NOW, apply=apply), con


print("── a plan complete in the graph ──")
plans, _ = run(household(), "p_alex")
check("Alex's thread (who, one place, a time; nothing covers it): one plan", len(plans), 1)
p = plans[0]
check("…the Mac's body shape: Pine Park, Sat 10:00, Robin and Ava with Alex, no one outside", (
      p["body"]["place"], p["body"]["start"][:16], p["body"]["participants"], p["body"]["others"]),
      ("Pine Park", "2026-10-11T10:00", ["p_alex", "p_ava", "p_robin"], 0))
snap = p["snapshot"]
check("…its snapshot is a situation() bundle: who, where, when, with the items it stands on",
      (sorted(snap["who"]["is"]), snap["where"]["is"], snap["when"]["is"], set(snap["via"]) >= {"dev-t1"}),
      (["p_ava", "p_robin"], ["pl-park"], ["2026-10-11T10:00"], True))
check("…body-free: no item text, title or sender id in the body", any(w in str(p["body"]) for w in ("Text", "dev-t")),
      False)

g = household()
g.nodes["dev-t1"]["attrs"]["when"] = ["2026-10-11T09:00", "2026-10-11T10:00"]   # graph_edges' shape (#1450): a list
lst = run(g, "p_alex")[0]
check("attrs.when as a list of local times (graph_edges, #1450): one plan, at the latest time stated",
      [p["body"]["start"][:16] for p in lst], ["2026-10-11T10:00"])
g.nodes["dev-t1"]["attrs"]["when"] = [None, 7, "2026-10-11"]
check("…a list with nothing usable (no time of day): nothing, no raise", len(run(g, "p_alex")[0]), 0)

print("── never twice, never for the wrong person ──")
for viewer in ("p_robin", "p_ava", "screen"):
    check(f"{viewer}: nothing (not their items; a kid or a screen never raises)", len(run(household(), viewer)[0]), 0)
cover = household([node("event:cal-1", "event", "Park", owner="p_alex", audience="private",
                        start="2026-10-11T09:30:00-07:00")], [edge("event:cal-1", "at", "pl-park")])
check("a calendar event at that place within 2 h covers it: nothing", len(run(cover, "p_alex")[0]), 0)
far = household([node("event:cal-1", "event", "Park", owner="p_alex", audience="private",
                      start="2026-10-11T14:30:00-07:00")], [edge("event:cal-1", "at", "pl-park")])
check("…one 4.5 h later doesn't", len(run(far, "p_alex")[0]), 1)
mac = household([node("event:ep-aaaa", "event", "Pine Park with Ava", owner="p_alex", audience="private",
                      start="2026-10-11T10:00:00-07:00")], [edge("p_robin", "attends", "event:ep-aaaa")])
check("the Mac's own proposal for the plan (same time, a shared person) covers it: no second card",
      len(run(mac, "p_alex")[0]), 0)
con = sqlite3.connect(":memory:")
os.environ["AGENT_FE_PROACTIVE"] = "1"
first, _ = run(household(), "p_alex", apply=True, con=con)
again, _ = run(household(), "p_alex", apply=True, con=con)
check("raised once: a second pass finds its own proposal (same sig and thread) and raises nothing",
      (len(first), len(again), con.execute("SELECT COUNT(*) FROM event_proposals").fetchone()[0]), (1, 0, 1))
check("…owned by Alex only", [r[0] for r in con.execute("SELECT owner FROM event_proposals")], ["p_alex"])
os.environ.pop("AGENT_FE_PROACTIVE")
off = sqlite3.connect(":memory:")
run(household(), "p_alex", apply=True, con=off)
check("flag off: detected, nothing written", off.execute("SELECT COUNT(*) FROM event_proposals").fetchone()[0], 0)

print("── incomplete is not a plan ──")
g = household()
g.edges = [e if not (e["type"] == "from") else {**e, "state": "candidate"} for e in g.edges]
g.edges = [e for e in g.edges if not (e["type"] == "mentions" and e["b"] == "p_ava")]
check("who only through a candidate edge (a text's undirected party): nothing", len(run(g, "p_alex")[0]), 0)
g = household()
g.nodes["dev-t1"]["attrs"]["when"] = "2026-10-11"
check("a day without a time of day: nothing", len(run(g, "p_alex")[0]), 0)
g = household(extra_edges=[edge("dev-t2", "mentions", "pl-cafe")])
check("two different places named: ambiguous, nothing", len(run(g, "p_alex")[0]), 0)
g = household()
g.nodes["dev-t1"]["attrs"]["when"] = "2026-10-09T10:00:00-07:00"
check("a time already past: nothing", len(run(g, "p_alex")[0]), 0)

print("── the golden household (P2's real projection), today ──")
with contextlib.redirect_stdout(io.StringIO()):
    import graph_golden_load as GL
    db = GL.build()
import graph_sync as G  # noqa: E402


class Golden:
    def visible_nodes(self, viewer, role, path=None):
        return G.visible_nodes(viewer, role, db)

    def visible_edges(self, viewer, role, path=None, **kw):
        return G.visible_edges(viewer, role, db, **kw)


gnow = datetime.fromisoformat(GL.H["now"]).astimezone(ZoneInfo(GL.H["zone"]))
new = {v: len(P.detect(v, r, Golden(), zone=ZoneInfo(GL.H["zone"]), now=gnow.replace(hour=6)))
       for v, r in (("p_alex", "admin"), ("p_robin", "adult"), ("p_jordan", "adult"), ("p_ava", "kid"),
                    ("p_leo", "kid"), ("screen", "household"))}
check("nothing new for anyone: a plan in texts isn't complete in the graph until #1436, and the Mac's proposal covers "
      "the one there is", new, {v: 0 for v in new})

print("── end to end: a text plan through /v1/hooks, projected by #1450, completed by P4 ──")
import hooks  # noqa: E402
ROBIN_PHONE = "+1 555 010 0001"                                      # the golden world's claim for Robin
con = hooks.connect()
# Robin to Alex: "Monday 18:30 at Pine Park?" (the body stays on the Mac; only its hooks come)
plan = hooks.put_device("p_alex", "admin", "d_alex_mac", {
    "item_id": "guid-p4-plan", "source": "text", "at": "2026-10-10", "hooks": [
        {"kind": "party:phone", "value": ROBIN_PHONE, "dir": "from", "line": 0},
        {"kind": "date:mention", "value": "2026-10-12T18:30", "line": 0},
        {"kind": "mention:place", "value": "pl-pine-park", "line": 0}]}, con)["item_id"]
# #1450's own ask: "Saturday 10:00 at Pine Park?", the plan the Mac's proposal already covers
ask = hooks.put_device("p_alex", "admin", "d_alex_mac", {
    "item_id": "guid-p4-ask", "source": "text", "at": "2026-10-09", "hooks": [
        {"kind": "party:phone", "value": ROBIN_PHONE, "dir": "from", "line": 0},
        {"kind": "date:mention", "value": "2026-10-10T10:00", "line": 0},
        {"kind": "mention:place", "value": "pl-pine-park", "line": 0}]}, con)["item_id"]
con.close()
with contextlib.redirect_stdout(io.StringIO()):
    db = GL.build()
gzone = ZoneInfo(GL.H["zone"])
got = P.detect("p_alex", "admin", Golden(), zone=gzone, now=gnow.replace(hour=6))
check("Alex: one proposal, for the Monday plan only (the covered Saturday ask raises nothing)",
      [(p["items"], p["body"]["place"], p["body"]["start"]) for p in got],
      [([plan], "Pine Park", "2026-10-12T18:30:00-07:00")])
check("…with Robin and Alex as its people", sorted(got[0]["body"]["participants"]) if got else None,
      ["p_alex", "p_robin"])
snap = got[0]["snapshot"] if got else {}
check("…its snapshot: who Robin, where Pine Park, when Monday 18:30, citing the text",
      (snap.get("who"), snap.get("where"), snap.get("when"), plan in str(snap.get("via"))),
      ({"is": ["p_robin"]}, {"is": ["pl-pine-park"]}, {"is": ["2026-10-12T18:30"]}, True))
others = {v: len(P.detect(v, r, Golden(), zone=gzone, now=gnow.replace(hour=6)))
          for v, r in (("p_robin", "adult"), ("p_jordan", "adult"), ("p_ava", "kid"), ("screen", "household"))}
check("nobody else gets it: the text is Alex's own (Robin, the sender, included)", others, {v: 0 for v in others})
check("a time already past by then: nothing", P.detect("p_alex", "admin", Golden(), zone=gzone,
                                                      now=datetime(2026, 10, 12, 19, 0, tzinfo=gzone)), [])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
