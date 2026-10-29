#!/usr/bin/env python3
# gate: hermetic
"""graph_sync node_sources (#1461): a viewer's node is built only from the sources they can see. Over the golden
household (graph_golden_load: the fixture in temporary copies of the real stores, projected by graph_sync):
  - the issue's case: the household org Harbor Mobile is shared with the adults only, and Alex's private "Harbor Mobile
    Billing" (a phone claim, the alias "Billing") is fused into it. Robin and Jordan see an org labelled "Harbor Mobile"
    with no phone and no "Billing" alias; Alex sees the full node; Ava and a shared screen see nothing;
  - the property test: for every viewer and every node, the node exists iff one of its sources is theirs, and every
    label and attr value in it comes from a source of theirs;
  - every edge state (candidate, verified, accepted) on the org: a viewer who can't see the org has no edge to it, and
    nobody gets an edge whose sources aren't theirs;
  - a second viewer for every derived view (the name anchors, same_as, graph_query, situation): what only Alex's
    private source says never reaches another viewer, and Alex's own reads still find it;
  - the migration: an existing graph.db in the first shape moves to node_sources without loss, twice is the same as
    once, and a reader works on either shape.
Fictional people only. Pure reads of temp copies.

Run: TZ=UTC python3 scripts/test_graph_node_sources.py"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import graph_golden_load as GL  # noqa: E402  (first: points every store at a temp dir and loads the fixture)
import audience as AUD  # noqa: E402
import fences  # noqa: E402
import graph_edges as GE  # noqa: E402
import graph_query as Q  # noqa: E402
import graph_sync as G  # noqa: E402
import situation as S  # noqa: E402

passed = failed = 0


def check(label, got, expected=True):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


ORG = "household:harbor-mobile"
PHONE, PHONE_DIGITS, BILLING = "+1 555 010 0042", "5550100042", "Billing"
HIDDEN = (PHONE, PHONE_DIGITS, BILLING.lower())          # what only Alex's private source says
ROLES = {p["id"]: p["role"] for p in GL.H["people"]}
VIEWERS = [(p, ROLES[p]) for p in ROLES] + [(None, "household")]
NAME = lambda v: v[0] or "screen"                        # noqa: E731

# ── the issue's world: the org shared with the adults only; Alex's private record carries a phone and an alias ────────
wcon = sqlite3.connect(GL.TMP / "world" / "household.db")
wcon.execute("UPDATE entities SET audience='adults' WHERE id='harbor-mobile'")
wcon.commit()
wcon.close()
acon = sqlite3.connect(GL.TMP / "world" / "p_alex.db")
acon.execute("INSERT INTO aliases (entity_id, alias_norm, alias) VALUES ('harbor-mobile-billing', 'billing', ?)", (BILLING,))
cid = acon.execute("INSERT INTO claims (entity_id, attr, value_norm, value, created_at, updated_at) "
                   "VALUES ('harbor-mobile-billing', 'phone', ?, ?, 0, 0)", (PHONE_DIGITS, PHONE)).lastrowid
acon.execute("INSERT INTO evidence (claim_id, source, kind, ref, origin, observed_at, prior, owner, audience, added_at) "
             "VALUES (?, 'mail', 'stated', 'alex-bill-1', 'mail', '2026-10-01', 0.9, 'p_alex', 'private', 0)", (cid,))
acon.commit()
acon.close()
# #1461 review: an entity the adults see, with one claim only Alex's private evidence supports and one the adults' does
SHOP, SHOP_PHONE, SHOP_EMAIL = "household:corner-shop", "+1 555 010 0077", "hello@corner-shop.example"
hcon = sqlite3.connect(GL.TMP / "world" / "household.db")
hcon.execute("INSERT INTO entities (id, type, name, owner, audience, created_at) "
             "VALUES ('corner-shop', 'org', 'Corner Shop', 'p_alex', 'adults', 0)")
for attr, val, aud in (("phone", SHOP_PHONE, "private"), ("email", SHOP_EMAIL, "adults")):
    c = hcon.execute("INSERT INTO claims (entity_id, attr, value_norm, value, created_at, updated_at) "
                     "VALUES ('corner-shop', ?, ?, ?, 0, 0)", (attr, val.lower(), val)).lastrowid
    hcon.execute("INSERT INTO evidence (claim_id, source, kind, ref, origin, observed_at, prior, owner, audience, added_at) "
                 "VALUES (?, 'mail', 'stated', ?, 'mail', '2026-10-01', 0.9, 'p_alex', ?, 0)", (c, f"shop-{attr}", aud))
hcon.commit()
hcon.close()

SRC = dict(records=GL.bill_records, today=date.fromisoformat(GL.H["now"][:10]))
nodes, xw, edges, fused = G.build(**SRC)

# the edges on the org, one of each state, each standing on a source Alex alone has and on one the adults have: the
# real Edge, provenance and writer (the fixture's own edges are verified and accepted only)
def org_edge(typ, state, srcs, plane="typed"):
    e = GE.Edge("cx-note-wifi", typ, ORG, plane=plane, state=state, rung="attributed", conf=0.5,
                why=f"a {state} {typ} from {srcs[0]['ref']}", src=srcs)
    e["owner"], e["audience"] = GE.narrowest(srcs)
    return e


adults_src = GE.prov("hooks", "adults-note-1", "p_alex", "adults", item=None)
alex_src = GE.prov("hooks", "alex-only-1", "p_alex", "private", item=None)
extra = [org_edge("from", "verified", [adults_src]), org_edge("to", "accepted", [adults_src]),
         org_edge("mentions", "candidate", [adults_src]),
         org_edge("mentions", "verified", [alex_src], plane="measured"),
         org_edge("from", "rejected", [adults_src], plane="measured")]
G.write(nodes, xw, edges=sorted(edges + extra, key=lambda e: (e["a"], e["type"], e["b"], e["plane"])), fused=fused)
DB = GL.TMP / "graph.db"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

print("── the shape: nodes keep id and kind; every node has its own source ──")
check("nodes has only id and kind", [r[1] for r in con.execute("PRAGMA table_info(nodes)")], ["id", "kind"])
check("every node has a source row; the fused record and each of its claims' evidence are more sources of the org",
      (con.execute("SELECT count(*) FROM nodes WHERE id NOT IN (SELECT node_id FROM node_sources)").fetchone()[0],
       [(r["source"], r["ref"], r["audience"]) for r in con.execute(
           "SELECT * FROM node_sources WHERE node_id=? ORDER BY rowid", (ORG,))]),
      (0, [("world", ORG, "adults"), ("world", "p_alex:harbor-mobile-billing", "private"),
           ("world-claim", "p_alex:1:1", "private"), ("world-claim", "p_alex:4:4", "private")]))
check("the schema version is recorded", con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0], G.SCHEMA_VERSION)

print("── the issue's case ──")
v = {NAME(x): G.visible_nodes(*x, DB) for x in VIEWERS}
robin, jordan, alex, ava, screen = v["p_robin"], v["p_jordan"], v["p_alex"], v["p_ava"], v["screen"]
check("Robin sees an org labelled from the shared record", (robin[ORG]["kind"], robin[ORG]["label"]), ("org", "Harbor Mobile"))
check("…with no phone, no domain claim and no Billing alias", (sorted(robin[ORG]["attrs"]), robin[ORG]["attrs"]["aliases"]),
      (["aliases", "name"], ["Harbor", "harbormobile.com"]))
check("Jordan, the other adult, sees the same", jordan[ORG]["attrs"], robin[ORG]["attrs"])
check("Alex sees the full node: the label, the phone, the domain and the alias",
      (alex[ORG]["label"], alex[ORG]["attrs"].get("phone"), alex[ORG]["attrs"].get("domain"),
       BILLING in alex[ORG]["attrs"]["aliases"]), ("Harbor Mobile", [PHONE], ["harbormobile.com"], True))
check("Ava (a kid) and a shared screen see no such node", (ORG in ava, ORG in screen), (False, False))
check("…nor any node that carries what only Alex's source says",
      [n for x in (ava, screen) for n in x.values() if any(h in json.dumps(n).lower() for h in HIDDEN)], [])
check("project() is the same set as visible_nodes()", {NAME(x): sorted(y for k in G.project(*x, DB).values() for y in k)
                                                     for x in VIEWERS}, {k: sorted(x) for k, x in v.items()})

print("── a claim is seen only through its own evidence (#1461 review) ──")
for (who, rl) in VIEWERS:
    n = G.visible_nodes(who, rl, DB).get(SHOP)
    if who == "p_alex":
        check("Alex sees the shop's private phone and its shared email", (n["attrs"].get("phone"), n["attrs"].get("email")),
              ([SHOP_PHONE], [SHOP_EMAIL]))
    elif rl in ("admin", "adult"):
        check(f"{NAME((who, rl))} sees the shop and its shared email, never the phone only Alex's evidence supports",
              (n is not None, n and n["attrs"].get("email"), n and n["attrs"].get("phone")), (True, [SHOP_EMAIL], None))
    else:
        check(f"{NAME((who, rl))} sees nothing of the shop", n, None)

print("── the property test: a node exists iff a source is theirs; every value comes from a source of theirs ──")
fs = fences.get()


def theirs(s, viewer, role):
    who = None if role == "household" else viewer
    if not AUD.visible(s["audience"], who, role, owner=s["owner"]):
        return False
    return not s["fence_item"] or fs.admits(who, role, s["owner"], item=s["fence_item"])


def leaves(x):
    if isinstance(x, dict):
        return {y for k in x for y in leaves(x[k])}
    if isinstance(x, list):
        return {y for k in x for y in leaves(k)}
    return {json.dumps(x)}


srcs: dict[str, list[dict]] = {}
for r in con.execute("SELECT n.id, s.* FROM nodes n JOIN node_sources s ON s.node_id=n.id ORDER BY n.id, s.rowid"):
    srcs.setdefault(r["id"], []).append(dict(r))
kinds = {r["id"]: r["kind"] for r in con.execute("SELECT id, kind FROM nodes")}
for viewer, role in VIEWERS:
    seen, bad_exist, bad_value = v[NAME((viewer, role))], [], []
    for nid, rows in srcs.items():
        mine = [s for s in rows if theirs(s, viewer, role)]
        if bool(mine) != (nid in seen):
            bad_exist.append(nid)
        if nid in seen:
            allowed = set().union(*[leaves(json.loads(s["attrs"])) for s in mine])
            if not leaves(seen[nid]["attrs"]) <= allowed or seen[nid]["label"] not in {s["label"] for s in mine}:
                bad_value.append(nid)
    check(f"{NAME((viewer, role))}: no node without a source of theirs, none missing", bad_exist, [])
    check(f"{NAME((viewer, role))}: no label or attr value from a source that isn't theirs", bad_value, [])
check("the check has teeth: only Alex's view has the org's private sources (his record and its claims' evidence)",
      [NAME(x) for x in VIEWERS if len([s for s in srcs[ORG] if theirs(s, *x)]) > 1], ["p_alex"])
check("a node with one source reads back exactly as stored (an item, a place, a member)",
      all(alex[n]["attrs"] == json.loads(srcs[n][0]["attrs"]) for n in srcs if len(srcs[n]) == 1 and n in alex), True)

print("── every edge state on the org: no leak passes silently ──")
for state in GE.STATES:
    check(f"the {state} edge exists in graph.db", con.execute(
        "SELECT count(*) FROM edges WHERE b=? AND state=?", (ORG, state)).fetchone()[0] >= 1, True)
for x in VIEWERS:
    for state in GE.STATES:
        es = G.visible_edges(*x, DB, states=(state,))
        seen = v[NAME(x)]
        bad = [e for e in es if e["a"] not in seen or e["b"] not in seen
               or not all(AUD.visible(p["audience"], None if x[1] == "household" else x[0], x[1], owner=p["owner"])
                          for p in e["prov"])]
        check(f"{NAME(x)} · {state}: every edge has both ends and every source theirs", bad, [])
        on_org = [e for e in es if ORG in (e["a"], e["b"])]
        who = NAME(x)
        want = {"p_alex": {"verified": 2, "accepted": 1, "candidate": 1, "rejected": 1},
                "p_robin": {"verified": 1, "accepted": 1, "candidate": 1, "rejected": 1},
                "p_jordan": {"verified": 1, "accepted": 1, "candidate": 1, "rejected": 1}}.get(who, {})
        check(f"{who} · {state}: the org's edges are exactly the ones whose sources are theirs",
              len([e for e in on_org if e["a"] == "cx-note-wifi"]), want.get(state, 0))
check("a kid sees no edge to the org in any state, and the default states hide the candidate from the adults",
      (G.visible_edges("p_ava", "kid", DB, b=ORG, states=GE.STATES),
       sorted({e["state"] for e in G.visible_edges("p_robin", "adult", DB, b=ORG, a="cx-note-wifi")})),
      ([], ["accepted", "verified"]))

print("── a second viewer for every derived view ──")


class Graph:
    """graph_sync's real readers over the temp graph.db: what situation() reads live."""
    zone, now = GL.H["zone"], GL.H["now"]

    def visible_nodes(self, viewer, role, path=None):
        return G.visible_nodes(viewer, role, DB)

    def visible_edges(self, viewer, role, path=None, **kw):
        return G.visible_edges(viewer, role, DB, **kw)

    def same_as(self, viewer, role, node_id=None, path=None):
        return G.same_as(viewer, role, node_id, DB)

    def obligation_state(self, viewer, role, node_id, path=None):
        return G.obligation_state(viewer, role, node_id, records=GL.bill_records, today=date.fromisoformat(self.now[:10]),
                                  path=DB)


g = Graph()


def views(viewer, role) -> dict[str, str]:
    """Every derived read of one viewer as a lowercase string: nodes, edges (every state), same_as, a neighbourhood from
    each node they see, the name anchors for each hidden word, and the situation bundle for questions naming it."""
    seen = G.visible_nodes(viewer, role, DB)
    who = viewer or "p_alex"
    out = {"nodes": json.dumps(seen), "edges": json.dumps(G.visible_edges(viewer, role, DB, states=GE.STATES)),
           "same_as": json.dumps(G.same_as(viewer, role, None, DB)),
           "anchors": json.dumps([S.anchors(q, who, seen) for q in ("what is Billing", "Harbor Mobile Billing", PHONE)])}
    out["query"] = json.dumps([Q.neighborhood(viewer, role, n, now=GL.H["now"], hops=2, k=50, path=DB) for n in sorted(seen)
                               if n in (ORG, "cx-note-wifi")])
    out["situation"] = json.dumps([S.situation(q, who, role, g, zone=g.zone, now=g.now) for q in
                                   ("Who is Billing?", "What is the phone number for Harbor Mobile?",
                                    "Is Harbor Mobile Billing the same as Harbor Mobile?", "What is Harbor Mobile?")])
    return {k: s.lower() for k, s in out.items()}


mine = views("p_alex", "admin")
check("positive control: Alex's own reads find what his source says (nodes, anchors, same_as, situation)",
      (PHONE in json.dumps(G.visible_nodes("p_alex", "admin", DB)), "harbor-mobile" in mine["anchors"],
       "harbor mobile billing" in mine["same_as"], "billing" in mine["situation"]), (True, True, True, True))
check("Alex's name anchors find the org by his alias; Robin's find nothing",
      (S.anchors("what is Billing", "p_alex", alex)["org"], S.anchors("what is Billing", "p_robin", robin)["org"]), ([ORG], []))
for x in VIEWERS:
    if x[0] == "p_alex":
        continue
    got = views(*x)
    for view, text in got.items():
        check(f"{NAME(x)} · {view}: nothing only Alex's source says reaches it", [h for h in HIDDEN if h in text], [])
check("same_as: only the private record's owner is told it was merged",
      {NAME(x): [m["world_id"] for m in G.same_as(*x, None, DB)] for x in VIEWERS},
      {"p_alex": ["p_alex:harbor-mobile-billing"], "p_robin": [], "p_jordan": [], "p_ava": [], "p_leo": [], "screen": []})

print("── the migration: an existing graph.db moves to node_sources without loss ──")
OLD = GL.TMP / "old.db"


def make_old(path: Path) -> None:
    """graph.db as the build before #1461 wrote it: label, attrs, owner, audience, src and fence_item on nodes."""
    path.unlink(missing_ok=True)
    shutil.copy(DB, path)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE nodes_old (id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT, audience TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '', attrs TEXT NOT NULL DEFAULT '{}', src TEXT NOT NULL, fence_item TEXT);
        INSERT INTO nodes_old SELECT n.id, n.kind, s.owner, s.audience, s.label, s.attrs, s.source, s.fence_item
            FROM nodes n JOIN node_sources s ON s.node_id = n.id AND s.rowid = (SELECT min(rowid) FROM node_sources
                                                                               WHERE node_id = n.id);
        DROP TABLE nodes; DROP TABLE node_sources; ALTER TABLE nodes_old RENAME TO nodes;
        DELETE FROM meta WHERE key='schema';
    """)
    c.commit()
    c.close()


make_old(OLD)
os.chmod(OLD, 0o600)
oc = sqlite3.connect(OLD)
oc.row_factory = sqlite3.Row
before = {r["id"]: dict(r) for r in oc.execute("SELECT * FROM nodes")}
oc.close()
edges_before = sqlite3.connect(OLD).execute("SELECT count(*), sum(length(why)) FROM edges").fetchone()
check("the old shape: label and attrs are on nodes, no node_sources", (
    "label" in {r[1] for r in sqlite3.connect(OLD).execute("PRAGMA table_info(nodes)")},
    sqlite3.connect(OLD).execute("SELECT count(*) FROM sqlite_master WHERE name='node_sources'").fetchone()[0]), (True, 0))
old_view = {NAME(x): G.visible_nodes(*x, OLD) for x in VIEWERS}
check("a reader works on the old shape too (one source per node), before any migration",
      {k: sorted(x) for k, x in old_view.items() if k != "p_alex"}, {k: sorted(x) for k, x in v.items() if k != "p_alex"})
check("a dry run changes nothing", (G.migrate(OLD, dry_run=True)["result"],
                                    "label" in {r[1] for r in sqlite3.connect(OLD).execute("PRAGMA table_info(nodes)")}),
      ("would migrate", True))
r1 = G.migrate(OLD)
check("the migration reports the nodes and sources it moved", (r1["result"], r1["nodes"], r1["sources"]),
      ("migrated", len(before), len(before)))
c2 = sqlite3.connect(OLD)
c2.row_factory = sqlite3.Row
check("nodes keeps id and kind", [r[1] for r in c2.execute("PRAGMA table_info(nodes)")], ["id", "kind"])
after = {r["node_id"]: dict(r) for r in c2.execute("SELECT * FROM node_sources")}
check("every node's label, attrs, owner, audience, src and fence_item are in its source row, unchanged",
      all((after[i]["label"], after[i]["attrs"], after[i]["owner"], after[i]["audience"], after[i]["source"],
           after[i]["fence_item"], after[i]["ref"]) == (b["label"], b["attrs"], b["owner"], b["audience"], b["src"],
                                                        b["fence_item"], i) for i, b in before.items()), True)
check("edges, kinds and the 0600 mode are untouched; the schema version is set",
      (c2.execute("SELECT count(*), sum(length(why)) FROM edges").fetchone()[:] == edges_before,
       {r["id"]: r["kind"] for r in c2.execute("SELECT * FROM nodes")} == {i: b["kind"] for i, b in before.items()},
       oct(os.stat(OLD).st_mode & 0o777), c2.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0]),
      (True, True, "0o600", G.SCHEMA_VERSION))
snap = c2.execute("SELECT group_concat(node_id || source || ref || label || attrs, '|') FROM (SELECT * FROM node_sources "
                  "ORDER BY node_id, source, ref)").fetchone()[0]
r2 = G.migrate(OLD)
check("run again: current, and not a byte of node_sources different (idempotent)",
      (r2["result"], c2.execute("SELECT group_concat(node_id || source || ref || label || attrs, '|') FROM (SELECT * FROM "
                                "node_sources ORDER BY node_id, source, ref)").fetchone()[0] == snap), ("current", True))
mig_view = {NAME(x): G.visible_nodes(*x, OLD) for x in VIEWERS}
check("after the migration every viewer's nodes are what they were before it", mig_view, old_view)
check("a missing graph.db is absent, never created", (G.migrate(GL.TMP / "nope.db")["result"], (GL.TMP / "nope.db").exists()),
      ("absent", False))
make_old(OLD)
os.chmod(OLD, 0o600)
live_shape = sqlite3.connect(OLD).execute("SELECT count(*) FROM nodes").fetchone()[0]
check("--migrate on the CLI: the same result, on the file it is given", (
    G.main(["--migrate", "--db", str(OLD), "--dry-run"]),
    "label" in {r[1] for r in sqlite3.connect(OLD).execute("PRAGMA table_info(nodes)")}, live_shape > 0), (0, True, True))
check("the live graph.db path is never the default under test", str(G.DB).startswith(str(GL.TMP)), True)

print("── a rebuild after the migration is whole: the next sync writes the new shape and the same views ──")
G.write(nodes, xw, OLD, edges=edges, fused=fused)
check("a fresh build over the migrated file has the new shape and the fused source and claim sources back",
      (G.migrate(OLD)["result"], len(sqlite3.connect(OLD).execute("SELECT * FROM node_sources WHERE node_id=?", (ORG,)).fetchall())),
      ("current", 4))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
