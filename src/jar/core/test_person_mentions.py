#!/usr/bin/env python3
# gate: hermetic   (the golden household plus a few rows of its own, in temp stores; no model, no network)
"""#1396 part 3: a device hook naming the household members a text includes (mention:person, by household person id
only), mapped by graph_edges.hook_edges to the owner's own `mentions` evidence, and kept the owner's.

The world is the golden household (graph_golden/household.json, whose family thread now carries mention:person hooks
for Ava and Leo) with rows added through the same loader (graph_golden_load, GRAPH_GOLDEN_FIXTURE): Robin's own text
naming Ava, Ava's own text naming Leo, and rows the device route would refuse (an outsider's node, an unknown id) written
straight into the link index, plus a row claiming to be Robin's on Alex's item. Checked per viewer (Alex, Robin, Jordan,
Ava, Leo, the shared screen) and for every edge state."""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
H = json.loads((HERE / "graph_golden" / "household.json").read_text())
W = copy.deepcopy(H)
os.environ["TZ"] = "UTC"
os.environ["AGENT_TZ"] = "America/Los_Angeles"


def text(iid, owner, at):
    return {"id": iid, "source": "texts", "kind": "text", "title": "Text", "observed_at": at, "owner": owner,
            "audience": "private", "fields": {}, "body": "-"}


W["items"] += [text("rb-kids-1", "p_robin", "2026-10-09T18:00:00-07:00"),
               text("av-1", "p_ava", "2026-10-09T19:00:00-07:00"),
               text("al-odd-1", "p_alex", "2026-10-09T20:00:00-07:00")]
W["hooks"] += [{"item": "rb-kids-1", "kind": "mention:person", "raw": "p_ava"},
               {"item": "av-1", "kind": "mention:person", "raw": "p_leo"},
               # rows the device route never stores (hooks.put_device drops them); the graph refuses them too
               {"item": "al-odd-1", "kind": "mention:person", "raw": "p_alex:hale-dorsey"},   # an outsider's node
               {"item": "al-odd-1", "kind": "mention:person", "raw": "p_nobody"},             # no such person
               {"item": "al-odd-1", "kind": "mention:person", "raw": "pl-larkspur"},          # not a person at all
               {"item": "dev-text-hale-1", "kind": "date:mention", "raw": "2026-10-12", "time": "16:00"}]   # a time: a card
fix = Path(tempfile.mkdtemp(prefix="titan-person-mentions-")) / "world.json"
fix.write_text(json.dumps(W))
os.environ["GRAPH_GOLDEN_FIXTURE"] = str(fix)
sys.path.insert(0, str(HERE))
import graph_golden_load as GL  # noqa: E402  (first: points every store at a temp dir and loads the fixture)
import graph_edges as GE  # noqa: E402
import graph_sync as G  # noqa: E402
import hooks  # noqa: E402
import fe_proactive  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402
from situation import situation  # noqa: E402

# a row claiming Robin's device named Jordan on Alex's item: a device never can (the item id is minted from the
# poster), and a row that says so anyway is not the item owner's evidence
_hc = hooks.connect()
_hc.execute("INSERT INTO hooks (item_id, source, kind, key, at, locator, owner, audience, made_at, version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", ("dev-text-hale-1", "text", "mention:person", "p_jordan", "2026-10-10",
                                             "{}", "p_robin", "private", 0, 1))
_hc.commit()

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n       got  {got!r}\n       want {want!r}"))


db = GL.build()
ROLE = {"p_alex": "admin", "p_robin": "adult", "p_jordan": "adult", "p_ava": "kid", "p_leo": "kid", None: "household"}
ALL = ("rejected", "verified", "accepted", "candidate")
PEOPLE = ("p_alex", "p_robin", "p_jordan", "p_ava", "p_leo")


def person_mentions(viewer, states=ALL):
    """The mention:person edges this viewer can see, as 'a b state'."""
    return sorted(f"{e['a']} {e['b']} {e['state']}" for e in G.visible_edges(viewer, ROLE[viewer], db, states=states)
                  if any(p["ref"].endswith(":mention:person") for p in e["prov"]))


def stored():
    con = G._ro_graph(db)
    try:
        return {(r["a"], r["b"]): dict(r) for r in con.execute(
            "SELECT e.* FROM edges e JOIN edge_prov p ON p.edge_id=e.id WHERE p.ref LIKE '%:mention:person'")}
    finally:
        con.close()


print("── the edge: the owner's evidence that a text names a member ──")
rows = stored()
check("exactly these: the family thread's two texts naming Ava and Leo, Robin's text naming Ava, Ava's naming Leo",
      sorted(rows), [("av-1", "p_leo"), ("dev-text-hale-2", "p_ava"), ("dev-text-hale-2", "p_leo"),
                     ("dev-text-hale-3", "p_ava"), ("dev-text-hale-3", "p_leo"), ("rb-kids-1", "p_ava")])
check("measured and accepted, attributed: a device's reading of its own text, never typed or verified",
      sorted({(r["type"], r["plane"], r["state"], r["rung"]) for r in rows.values()}),
      [("mentions", "measured", "accepted", "attributed")])
check("owner the item's owner, audience private", sorted({(a, r["owner"], r["audience"]) for (a, _), r in rows.items()}),
      [("av-1", "p_ava", "private"), ("dev-text-hale-2", "p_alex", "private"), ("dev-text-hale-3", "p_alex", "private"),
       ("rb-kids-1", "p_robin", "private")])
check("an outsider's node, an unknown id and a place id named as a person: no edge",
      [k for k in stored() if k[0] == "al-odd-1"], [])
check("a row from Robin on Alex's item attaches nothing to it", [k for k in rows if k == ("dev-text-hale-1", "p_jordan")],
      [])

print("── per viewer: only the owner, every state ──")
want = {"p_alex": ["dev-text-hale-2 p_ava accepted", "dev-text-hale-2 p_leo accepted", "dev-text-hale-3 p_ava accepted",
                   "dev-text-hale-3 p_leo accepted"],
        "p_robin": ["rb-kids-1 p_ava accepted"], "p_jordan": [], "p_ava": ["av-1 p_leo accepted"], "p_leo": [], None: []}
for v in ROLE:
    check(f"{v or 'the screen'} sees only their own", person_mentions(v), want[v])

# the leak-tests lesson (#1445): visible_edges defaults to verified/accepted, so a candidate leak passes silently. Put
# every mention:person edge in each state in turn and ask for exactly that state.
con = sqlite3.connect(db)
ids = [r[0] for r in con.execute("SELECT DISTINCT e.id FROM edges e JOIN edge_prov p ON p.edge_id=e.id "
                                 "WHERE p.ref LIKE '%:mention:person'")]
orig = {i: con.execute("SELECT state FROM edges WHERE id=?", (i,)).fetchone()[0] for i in ids}
try:
    for st in ("candidate", "verified", "accepted", "rejected"):
        con.executemany("UPDATE edges SET state=? WHERE id=?", [(st, i) for i in ids])
        con.commit()
        for v in ROLE:
            got = person_mentions(v, states=(st,))
            check(f"{st}: {v or 'the screen'} sees only their own",
                  [g.rsplit(" ", 1)[0] for g in got], [w.rsplit(" ", 1)[0] for w in want[v]])
finally:
    con.executemany("UPDATE edges SET state=? WHERE id=?", [(s, i) for i, s in orig.items()])
    con.commit()
    con.close()


class Graph:
    zone, now = GL.H["zone"], GL.H["now"]

    def visible_nodes(self, viewer, role, path=None):
        return G.visible_nodes(viewer, role, db)

    def visible_edges(self, viewer, role, path=None, **kw):
        return G.visible_edges(viewer, role, db, **kw)

    def same_as(self, viewer, role, node_id=None, path=None):
        return []


g = Graph()
print("── situation(): Alex's plan names the family; nobody else learns it ──")
alex = situation("Who did we come with?", "p_alex", "admin", g, zone=g.zone, now=g.now)
check("Alex at Larkspur Hall: Robin, Ava and Leo (golden inf-1), the host only as 'invited by'",
      (sorted(alex["who"]["is"]), alex["what"]["is"]), (["p_ava", "p_leo", "p_robin"], ["invited by Hale Dorsey"]))
check("…Ava and Leo said through the text's mentions", sorted(
    {ln["b"] for ln in alex.get("why") or [] if ln.get("type") == "mentions" and ln["a"] == "dev-text-hale-2"}
    & {"p_ava", "p_leo"}), ["p_ava", "p_leo"])
SECRET = ("dev-text-hale", "hale dorsey", "rb-kids-1")
for v in ("p_robin", "p_jordan", "p_ava", "p_leo"):
    for q in ("Who did we come with?", "Who's here with us?", "Where is Ava going?", "What is Leo doing today?"):
        blob = json.dumps(situation(q, v, ROLE[v], g, zone=g.zone, now=g.now)).lower()
        bad = [x for x in SECRET if x in blob and not (x == "rb-kids-1" and v == "p_robin")]
        check(f"{v} asking {q!r}: nothing of another's mention:person", bad, [])
screen = json.dumps(situation("Who did we come with?", None, "household", g, zone=g.zone, now=g.now)).lower()
check("the screen: nothing", [x for x in SECRET + ("av-1",) if x in screen], [])

print("── fe_proactive: the card names the family, for its owner only ──")
now = datetime.fromisoformat(GL.H["now"])
plans = fe_proactive.detect("p_alex", "admin", g, zone=ZoneInfo(g.zone), now=now)
check("Alex's plan card: Robin, Ava and Leo as well as Alex, and the host as one other",
      [(p["body"]["participants"], p["body"]["others"]) for p in plans if "dev-text-hale-2" in p["items"]],
      [(["p_alex", "p_ava", "p_leo", "p_robin"], 1)])
for v in ("p_robin", "p_jordan", "p_ava", "p_leo"):
    blob = json.dumps(fe_proactive.detect(v, ROLE[v], g, zone=ZoneInfo(g.zone), now=now)).lower()
    check(f"{v}: no card from Alex's texts", [x for x in ("dev-text-hale", "larkspur") if x in blob], [])

print("── hook_edges directly: the owner's own row, a member's node ──")
nodes, xw, _e, _f = G.build(records=GL.bill_records, today=now.date())
ctx = GE.Ctx(nodes, xw, {}, [], [])
mem = sqlite3.connect(":memory:")
mem.row_factory = sqlite3.Row
hooks.ensure(mem)


def edges_for(rows_):
    mem.execute("DELETE FROM hooks")
    mem.executemany("INSERT INTO hooks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(i, "text", "mention:person", k, "2026-10-10", "{}", o, "private", 0, 1) for i, k, o in rows_])
    return sorted((e["a"], e["b"]) for e in GE.hook_edges(ctx, mem) if e["type"] == "mentions")


check("the owner's row naming a member: one edge", edges_for([("dev-text-hale-2", "p_ava", "p_alex")]),
      [("dev-text-hale-2", "p_ava")])
check("another person's row on that item: none", edges_for([("dev-text-hale-2", "p_ava", "p_robin")]), [])
check("an ownerless row: none", edges_for([("dev-text-hale-2", "p_ava", None)]), [])
check("an outsider's person node, an unknown id, a place: none",
      edges_for([("dev-text-hale-2", "p_alex:hale-dorsey", "p_alex"), ("dev-text-hale-2", "p_nobody", "p_alex"),
                 ("dev-text-hale-2", "pl-larkspur", "p_alex")]), [])

print(f"\n{'FAIL' if fails else 'ok'}: {fails} failed")
sys.exit(1 if fails else 0)
