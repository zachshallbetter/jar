#!/usr/bin/env python3
# gate: hermetic   (the golden household plus a few rows of its own, in temp stores; no model, no network)
"""#1650 (#1396, part 4): one owner's texts that carry the same thread:device value (one conversation on their device)
on the same household day get a measured `related` edge, and a text's reply:device (an inline reply) a measured
`replies_to` edge to the item it answers, the same owner's. Both are the owner's own evidence and nobody else's.

The world is the golden household (graph_golden/household.json, whose family thread carries thread:device and
reply:device hooks) with rows added through the same loader (graph_golden_load, GRAPH_GOLDEN_FIXTURE): Robin's own
texts with the very thread value Alex's family thread has (a value two owners share), Ava's own thread and reply (a
kid's, which her parents' views must not learn), Leo's lone text, a busy conversation over the cap, one at the cap, a
pair Alex said is not related, two texts either side of a household midnight, a reply naming someone else's item, and
rows claiming to be another person's on Alex's items. Checked per viewer (Alex, Robin, Jordan, Ava, Leo, the shared
screen) and for every edge state, and through situation() and fe_proactive."""
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
os.environ.setdefault("TZ", "UTC")
os.environ["AGENT_TZ"] = "America/Los_Angeles"

FAMILY = "5a6b7c8d9e0f1a2b"      # Alex's conversation with Robin in the golden fixture


def text(iid, owner, at):
    return {"id": iid, "source": "texts", "kind": "text", "title": "Text", "observed_at": at, "owner": owner,
            "audience": "private", "fields": {}, "body": "-"}


def thread(iid, ref):
    return {"item": iid, "kind": "thread:device", "raw": ref}


def reply(iid, parent):
    return {"item": iid, "kind": "reply:device", "raw": parent}


BUSY = [f"al-busy-{i}" for i in range(1, 10)]            # nine on one day: over MAX_SAME_THREAD
CAP = [f"al-cap-{i}" for i in range(1, 9)]               # eight: at it
W["items"] += [text(i, "p_alex", f"2026-10-08T{9 + n:02d}:00:00-07:00") for n, i in enumerate(BUSY)]
W["items"] += [text(i, "p_alex", f"2026-10-07T{9 + n:02d}:00:00-07:00") for n, i in enumerate(CAP)]
W["items"] += [text("al-x-1", "p_alex", "2026-10-09T12:00:00-07:00"),       # the family conversation, another day
               text("al-j-1", "p_alex", "2026-10-09T13:00:00-07:00"),       # a pair Alex said is not related
               text("al-j-2", "p_alex", "2026-10-09T13:05:00-07:00"),
               text("al-n-1", "p_alex", "2026-10-09T23:30:00-07:00"),       # either side of the household's midnight
               text("al-n-2", "p_alex", "2026-10-10T00:30:00-07:00"),       # (one UTC day: 06:30Z and 07:30Z)
               text("al-r-1", "p_alex", "2026-10-09T14:00:00-07:00"),       # a reply naming Robin's item
               text("rb-t-1", "p_robin", "2026-10-10T11:00:00-07:00"),      # Robin's own, Alex's thread value
               text("rb-t-2", "p_robin", "2026-10-10T11:10:00-07:00"),
               text("av-t-1", "p_ava", "2026-10-10T12:00:00-07:00"),        # a kid's own thread and reply
               text("av-t-2", "p_ava", "2026-10-10T12:05:00-07:00"),
               text("lo-t-1", "p_leo", "2026-10-10T12:30:00-07:00")]
W["hooks"] += [thread(i, "b0b0b0b0b0b0b0b0") for i in BUSY] + [thread(i, "d0d0d0d0d0d0d0d0") for i in CAP]
W["hooks"] += [thread("al-x-1", FAMILY),
               thread("al-j-1", "c0c0c0c0c0c0c0c0"), thread("al-j-2", "c0c0c0c0c0c0c0c0"), reply("al-j-2", "al-j-1"),
               thread("al-n-1", "e0e0e0e0e0e0e0e0"), thread("al-n-2", "e0e0e0e0e0e0e0e0"),
               reply("al-r-1", "rb-t-1"),                                  # the route never keeps it; the graph refuses it
               thread("rb-t-1", FAMILY), thread("rb-t-2", FAMILY), reply("rb-t-2", "rb-t-1"),
               thread("av-t-1", "a1a1a1a1b2b2b2b2"), thread("av-t-2", "a1a1a1a1b2b2b2b2"), reply("av-t-2", "av-t-1"),
               {"item": "av-t-1", "kind": "mention:place", "raw": "pl-larkspur"},
               {"item": "av-t-2", "kind": "mention:person", "raw": "p_jordan"},
               thread("lo-t-1", "a1a1a1a1b2b2b2b2")]
W["link_statements"] += [{"a": "al-j-1", "b": "al-j-2", "related": False, "by": "p_alex", "source": "chat"}]
fix = Path(tempfile.mkdtemp(prefix="titan-thread-edges-")) / "world.json"
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

# rows a device never can write (an item id is minted from its poster), written anyway: Robin's thread value and a
# reply of Robin's on Alex's items are not Alex's evidence
_hc = hooks.connect()
_hc.executemany("INSERT INTO hooks (item_id, source, kind, key, at, locator, owner, audience, made_at, version) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [("dev-text-hale-1", "text", "thread:device", FAMILY, "2026-10-10", "{}", "p_robin", "private", 0, 1),
                 ("al-x-1", "text", "reply:device", "dev-text-hale-2", "2026-10-09", "{}", "p_robin", "private", 0, 1)])
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
MINE = (":thread:device", ":reply:device")


def ours(e):
    return any(p["ref"].endswith(MINE) for p in e["prov"])


def seen_by(viewer, states=ALL):
    """The thread and reply edges this viewer can see, as 'a type b'."""
    return sorted(f"{e['a']} {e['type']} {e['b']}" for e in G.visible_edges(viewer, ROLE[viewer], db, states=states)
                  if ours(e))


def stored():
    con = G._ro_graph(db)
    try:
        return {(r["a"], r["type"], r["b"]): dict(r) for r in con.execute(
            "SELECT DISTINCT e.* FROM edges e JOIN edge_prov p ON p.edge_id=e.id "
            "WHERE p.ref LIKE '%:thread:device' OR p.ref LIKE '%:reply:device'")}
    finally:
        con.close()


def pairs(ids):
    return [f"{a} related {b}" for n, a in enumerate(sorted(ids)) for b in sorted(ids)[n + 1:]]


ALEX = sorted(["dev-text-hale-2 related dev-text-hale-3", "dev-text-hale-3 replies_to dev-text-hale-2", *pairs(CAP)])
ROBIN = ["rb-t-1 related rb-t-2", "rb-t-2 replies_to rb-t-1"]
AVA = ["av-t-1 related av-t-2", "av-t-2 replies_to av-t-1"]

print("── the edges: one owner's conversation on one day, and an inline reply ──")
rows = stored()
check("exactly these: Alex's family thread and its reply, the 8-text conversation, Robin's own, Ava's own",
      sorted(" ".join(k) for k in rows), sorted(ALEX + ROBIN + AVA))
check("measured and accepted: related attributed, replies_to observed (never typed, never verified)",
      sorted({(r["type"], r["plane"], r["state"], r["rung"]) for r in rows.values()}),
      [("related", "measured", "accepted", "attributed"), ("replies_to", "measured", "accepted", "observed")])
check("owner the texts' owner, audience private (the narrowest of the hook rows and both items)",
      sorted({(k[0].split("-")[0], r["owner"], r["audience"]) for k, r in rows.items()}),
      [("al", "p_alex", "private"), ("av", "p_ava", "private"), ("dev", "p_alex", "private"),
       ("rb", "p_robin", "private")])
check("never across owners: Robin's texts carry Alex's thread value, and no edge joins his texts to hers",
      [k for k in rows if {k[0].split("-")[0], k[2].split("-")[0]} in ({"rb", "dev"}, {"rb", "al"})], [])
check("a busy conversation (9 of Alex's texts on one day) links nothing", [k for k in rows if "al-busy" in k[0]], [])
check("…8 is the cap: every pair links", len([k for k in rows if k[0].startswith("al-cap")]), 28)
check("the same conversation on another day: no link", [k for k in rows if "al-x-1" in (k[0], k[2])], [])
check("either side of the household's midnight (one UTC day): two days, no link",
      [k for k in rows if "al-n-1" in (k[0], k[2])], [])
check("a reply naming someone else's item: no edge", [k for k in rows if k[0] == "al-r-1"], [])
check("a thread row of Robin's on Alex's text joins it to nothing", [k for k in rows if "dev-text-hale-1" in k], [])
check("a reply row of Robin's on Alex's text: no edge", [k for k in rows if k[0] == "al-x-1"], [])
con = G._ro_graph(db)
judged = [tuple(r) for r in con.execute("SELECT type, plane, state FROM edges WHERE a='al-j-1' AND b='al-j-2' "
                                         "OR a='al-j-2' AND b='al-j-1' ORDER BY type, plane")]
con.close()
check("a pair Alex said is not related stays rejected: no thread link over it, no reply edge either",
      judged, [("related", "measured", "rejected")])
check("a lone text in a thread (Leo's): nothing", [k for k in rows if "lo-t-1" in k], [])

print("── per viewer: only the owner, every state ──")
want = {"p_alex": ALEX, "p_robin": ROBIN, "p_jordan": [], "p_ava": AVA, "p_leo": [], None: []}
for v in ROLE:
    check(f"{v or 'the screen'} sees only their own", seen_by(v), want[v])

# the leak-tests lesson (#1445): visible_edges defaults to verified/accepted, so a candidate leak passes silently. Put
# every thread and reply edge in each state in turn and ask for exactly that state.
con = sqlite3.connect(db)
ids = [r[0] for r in con.execute("SELECT DISTINCT e.id FROM edges e JOIN edge_prov p ON p.edge_id=e.id "
                                 "WHERE p.ref LIKE '%:thread:device' OR p.ref LIKE '%:reply:device'")]
orig = {i: con.execute("SELECT state FROM edges WHERE id=?", (i,)).fetchone()[0] for i in ids}
try:
    for st in ("candidate", "verified", "accepted", "rejected"):
        con.executemany("UPDATE edges SET state=? WHERE id=?", [(st, i) for i in ids])
        con.commit()
        for v in ROLE:
            check(f"{st}: {v or 'the screen'} sees only their own", seen_by(v, states=(st,)), want[v])
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
print("── situation(): golden inf-1 whole, and nobody learns another's thread ──")
alex = situation("Who did we come with?", "p_alex", "admin", g, zone=g.zone, now=g.now)
check("Alex at Larkspur Hall: Robin, Ava and Leo, the host only as 'invited by' (Ava's own thread, which names the "
      "place and Jordan, adds nobody)",
      (sorted(alex["who"]["is"]), alex["what"]["is"]), (["p_ava", "p_leo", "p_robin"], ["invited by Hale Dorsey"]))
check("…and the chain cites Robin's reply (dev-text-hale-3), joined by the thread and the inline reply",
      "dev-text-hale-3" in (alex.get("via") or []), True)
SECRET = {"p_alex": ("rb-t-", "av-t-", "lo-t-"), "p_robin": ("dev-text-hale", "al-", "av-t-", "lo-t-"),
          "p_jordan": ("dev-text-hale", "al-", "rb-t-", "av-t-", "lo-t-"),
          "p_ava": ("dev-text-hale", "al-", "rb-t-", "lo-t-"), "p_leo": ("dev-text-hale", "al-", "rb-t-", "av-t-")}
for v, secret in SECRET.items():
    for q in ("Who did we come with?", "Who's here with us?", "What is Ava doing today?", "Where is Ava going?"):
        blob = json.dumps(situation(q, v, ROLE[v], g, zone=g.zone, now=g.now)).lower()
        check(f"{v} asking {q!r}: nothing of another's thread", [x for x in secret if x in blob], [])
screen = json.dumps(situation("Who did we come with?", None, "household", g, zone=g.zone, now=g.now)).lower()
check("the screen: nothing", [x for x in ("dev-text-hale", "al-", "rb-t-", "av-t-", "lo-t-") if x in screen], [])

print("── fe_proactive: cards only from the viewer's own threads ──")
now = datetime.fromisoformat(GL.H["now"])
for v, secret in SECRET.items():
    blob = json.dumps(fe_proactive.detect(v, ROLE[v], g, zone=ZoneInfo(g.zone), now=now)).lower()
    check(f"{v}: no card from another's texts", [x for x in secret if x in blob], [])

print("── the edge builders directly: the owner's own rows only ──")
nodes, xw, _e, _f = G.build(records=GL.bill_records, today=now.date())
ctx = GE.Ctx(nodes, xw, {}, [], [])
mem = sqlite3.connect(":memory:")
mem.row_factory = sqlite3.Row
hooks.ensure(mem)


def built(rows_, edges=()):
    mem.execute("DELETE FROM hooks")
    mem.executemany("INSERT INTO hooks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(i, "text", k, v, "2026-10-10", "{}", o, "private", 0, 1) for i, k, v, o in rows_])
    out = GE.same_thread_day_edges(ctx, mem, list(edges)) + GE.device_reply_edges(ctx, mem, list(edges))
    return sorted((e["a"], e["type"], e["b"]) for e in out)


check("the owner's two rows: one related edge",
      built([("dev-text-hale-2", "thread:device", FAMILY, "p_alex"), ("dev-text-hale-3", "thread:device", FAMILY, "p_alex")]),
      [("dev-text-hale-2", "related", "dev-text-hale-3")])
check("one row another person's: none",
      built([("dev-text-hale-2", "thread:device", FAMILY, "p_alex"), ("dev-text-hale-3", "thread:device", FAMILY, "p_robin")]),
      [])
check("ownerless rows: none",
      built([("dev-text-hale-2", "thread:device", FAMILY, None), ("dev-text-hale-3", "thread:device", FAMILY, None)]), [])
check("two owners' texts with one value: none",
      built([("dev-text-hale-2", "thread:device", FAMILY, "p_alex"), ("rb-t-1", "thread:device", FAMILY, "p_robin")]), [])
check("a reply to the owner's own item: one replies_to",
      built([("dev-text-hale-3", "reply:device", "dev-text-hale-2", "p_alex")]),
      [("dev-text-hale-3", "replies_to", "dev-text-hale-2")])
check("a reply to another person's item, to a missing item or to itself: none",
      built([("dev-text-hale-3", "reply:device", "rb-t-1", "p_alex"), ("dev-text-hale-2", "reply:device", "nope", "p_alex"),
             ("al-x-1", "reply:device", "al-x-1", "p_alex")]), [])
no = GE.Edge("dev-text-hale-2", "related", "dev-text-hale-3", plane="measured", state="rejected", rung="observed",
             conf=0.0, why="not related, said by its owner", src=[GE.prov("link_statements", "x", "p_alex", "private")])
check("a pair its owner said is not related: no thread link and no reply over the 'no'",
      built([("dev-text-hale-2", "thread:device", FAMILY, "p_alex"), ("dev-text-hale-3", "thread:device", FAMILY, "p_alex"),
             ("dev-text-hale-3", "reply:device", "dev-text-hale-2", "p_alex")], [no]), [])

print(f"\n{'FAIL' if fails else 'ok'}: {fails} failed")
sys.exit(1 if fails else 0)
