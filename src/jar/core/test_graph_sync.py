#!/usr/bin/env python3
# gate: hermetic
"""Relational FE graph P1 (#1399; docs/relational-fe-graph.md §4, §7, §9, §10): graph_sync builds the node
projection from the stores themselves. The shared fictional household (graph_golden/household.json) is written into
temporary copies of the real stores (the household, world bubbles, places, the ledger, the KB's sharing table, the
link index, fences, event proposals, tasks; bills as the record runtime gives them), graph_sync builds graph.db, and:
  - the rebuild is deterministic (two builds, the same hash);
  - every viewer's projection is exactly `expected` (the property test: nothing a viewer's stores wouldn't show);
  - fusion: members are one node; a private org fuses into a household one only with an alias AND a hard key; the
    same name in two bubbles stays two people; a candidate place is a node only once seen 3 times;
  - graph.db holds viewer-independent rows only (no bill state), and is 0600.
Run: python3 scripts/test_graph_sync.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph_golden_load import *  # noqa: E402,F401,F403  (the fixture, loaded into temp stores)
from graph_golden_load import _TMP, HERE, H  # noqa: E402

passed = failed = 0

def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.400} expected={expected!r:.400}"))



# ── the build ───────────────────────────────────────────────────────────────────────────────────────────────────────
import graph_sync as G  # noqa: E402
TODAY = date.fromisoformat(H["now"][:10])
SRC = dict(records=bill_records, today=TODAY)
n1, x1, e1, f1 = G.build(**SRC)
h1 = G.write(n1, x1, edges=e1, fused=f1)
n2, x2, e2, f2 = G.build(**SRC)
h2 = G.write(n2, x2, edges=e2, fused=f2)

print("── deterministic ──")
check("two builds from the same stores (nodes and edges): the same hash", h1, h2)
check("graph.db is 0600", oct(os.stat(_TMP / "graph.db").st_mode & 0o777), "0o600")

print("── fusion ──")
ids = {n["id"] for n in n1}
xw = {k: v for k, v in x1.items()}
check("a member's world entity is that member's one node", (xw.get(("world", "household:alex-quarry")), "household:alex-quarry" in ids),
      ("p_alex", False))
check("Alex's private 'Harbor Mobile Billing' fuses into the household org (alias + the shared domain)",
      (xw.get(("world", "p_alex:harbor-mobile-billing")), "p_alex:harbor-mobile-billing" in ids), ("household:harbor-mobile", False))
check("the same name in two bubbles stays two people (no shared hard key)",
      ("p_alex:morgan-reyes-coach" in ids, "p_robin:morgan-reyes-colleague" in ids), (True, True))
check("a candidate place seen 3 times is a node; once is not", ("pl-cand-willow" in ids, "pl-cand-ember" in ids), (True, False))
check("graph.db stores no bill state (it's per viewer, at read time)",
      any("state" in (n["attrs"] or {}) for n in n1 if n["kind"] == "obligation"), False)
check("counts by kind (the expected nodes, and the meeting's decisions)", {k: sum(1 for n in n1 if n["kind"] == k) for k in G.KINDS},
      {k: len({x for v in H["expected"] if v != "about" for x in H["expected"][v].get(k, [])}) + (len(DECISION_IDS) if k == "item" else 0)
       for k in G.KINDS})

print("── per viewer: exactly what their stores would show (the property test) ──")
ROLES = {p["id"]: p["role"] for p in H["people"]}
for viewer, want in H["expected"].items():
    if viewer == "about":
        continue
    pid, role = (None, "household") if viewer == "screen" else (viewer, ROLES[viewer])
    got = G.project(pid, role)
    want = dict(want, item=want.get("item", []) + (DECISION_IDS if viewer == MD["owner"] else []))   # its owner's only
    for kind in G.KINDS:
        check(f"{viewer} · {kind}", sorted(got.get(kind, [])), sorted(want.get(kind, [])))

print("── P2 edges: exactly the fixture's expected edges ──")
import audience as AUD  # noqa: E402
import graph_edges as GE  # noqa: E402
fmt = lambda e: f"{e['a']} {e['type']} {e['b']} · {e['state']} · {e['plane']}" + (  # noqa: E731
    f" @{e['valid_from'] or e['valid_to']}" if (e["valid_from"] or e["valid_to"]) else "")
got_e = [fmt(e) for e in e1]
want_e = H["expected_edges"]["edges"]
check("every expected edge, and nothing else", (sorted(set(want_e) - set(got_e)), sorted(set(got_e) - set(want_e))), ([], []))
check("every edge's ends have the kinds its type allows (§5.1)", all(GE._ok(GE.Ctx(n1, x1, {}, [], []), e) for e in e1), True)
check("every edge says why and where it came from", all(e["why"] and e["prov"] and all(p["extractor"] == GE.EXTRACTOR
                                                                                     for p in e["prov"]) for e in e1), True)

print("── P2 per viewer: an edge only with both ends and every source (the property test) ──")
VIEWERS = [(v, ROLES[v]) for v in ROLES] + [(None, "household")]
for pid, role in VIEWERS:
    nodes_v = G.visible_nodes(pid, role)
    edges_v = G.visible_edges(pid, role, states=GE.STATES)
    who = None if role == "household" else pid
    bad = [fmt(e) for e in edges_v if e["a"] not in nodes_v or e["b"] not in nodes_v
           or not all(AUD.visible(p["audience"], who, role, owner=p["owner"]) for p in e["prov"])]
    kin = [fmt(e) for e in edges_v if role in ("kid", "household") and nodes_v[e["a"]]["kind"] == nodes_v[e["b"]]["kind"]
           == "person" and who not in (e["a"], e["b"])]
    check(f"{pid or 'screen'}: no edge without both ends and every source; none between two other people on a "
          f"kid's view or a screen", (bad, kin), ([], []))
    check(f"{pid or 'screen'}: visible_nodes is project() with the rows", sorted(nodes_v),
          sorted(x for v in G.project(pid, role).values() for x in v))

def ev(pid, role, **kw):
    return {fmt(e) for e in G.visible_edges(pid, role, **kw)}

pe_ava = "p_ava located_at pl-cedar · verified · typed @2026-10-10T15:20:00-07:00"
pe_alex = "p_alex located_at pl-home · verified · typed @2026-10-10T15:40:00-07:00"
check("a place event: a parent sees their kid's (where-1)", pe_ava in ev("p_alex", "admin"), True)
check("…the kid sees their own", pe_ava in ev("p_ava", "kid"), True)
check("…an adult who isn't a parent doesn't, though the place is the household's (where-2)", pe_ava in ev("p_jordan", "adult"),
      False)
check("…another adult's place events stay theirs (where-3)", (pe_alex in ev("p_robin", "adult"), pe_alex in ev("p_alex", "admin")),
      (False, True))
check("…a shared screen sees none", any("@" in x for x in ev(None, "household")), False)
check("the standing fact (Ava's school, the people row) is a separate edge, household-wide",
      "p_ava located_at pl-cedar · verified · typed" in ev("p_jordan", "adult"), True)
sees = "p_ava sees household:summit-dental · accepted · typed"
check("health (sees): Ava and her parents, never Jordan or a screen",
      [sees in ev(p, r) for p, r in (("p_ava", "kid"), ("p_alex", "admin"), ("p_robin", "adult"), ("p_jordan", "adult"),
                                     (None, "household"))], [True, True, True, False, False])
rej = "cx-mail-bill-3 related cx-mail-trade-2 · rejected · measured"
cand = "cx-mail-bill-3 related cx-note-wifi · candidate · measured"
check("a rejected and a candidate edge: never by default, only when asked (trade-2)",
      (rej in ev("p_alex", "admin"), cand in ev("p_alex", "admin"), rej in ev("p_alex", "admin", states=GE.STATES),
       cand in ev("p_alex", "admin", states=("candidate",))), (False, False, True, True))
about = "cx-mail-dentist about event:cx-calendar-dentist · accepted · measured"
check("Alex's private dentist mail is about the shared appointment, for Alex only (dent-1, dent-2)",
      (about in ev("p_alex", "admin"), about in ev("p_robin", "adult"), about in ev("p_ava", "kid")), (True, False, False))
check("…and its summary travels only with the item", ("summary" in (G.visible_nodes("p_alex", "admin").get("cx-mail-dentist")
                                                                    or {}).get("attrs", {}),
                                                       "cx-mail-dentist" in G.visible_nodes("p_robin", "adult")), (True, False))
check("an unresolved location stays text on the event, a resolved one is an `at` edge (place-2, where-4)",
      (G.visible_nodes("p_jordan", "adult")["event:cx-calendar-bookclub"]["attrs"].get("location_text"),
       "location_text" in G.visible_nodes("p_alex", "admin")["event:cx-calendar-pickup"]["attrs"],
       "event:cx-calendar-pickup at pl-cedar · accepted · typed" in ev("p_alex", "admin")), ("Ember Café", False, True))
check("a text's party is its counterparty, never a sender: candidate only",
      "dev-text-plan-2 from p_robin · candidate · typed" in got_e and
      "dev-text-plan-2 from p_robin · candidate · typed" not in ev("p_alex", "admin"), True)
check("the plan's attendees come from the proposal (plan-1, plan-2)",
      sorted(e["a"] for e in G.visible_edges("p_alex", "admin", b="event:ep-a1b2c3d4e5f60718", types=["attends"])),
      ["p_alex", "p_ava", "p_jordan", "p_leo", "p_robin"])
check("…and none of it reaches a kid (plan-5)", G.visible_edges("p_ava", "kid", b="event:ep-a1b2c3d4e5f60718"), [])
check("parents (rel-1): Jordan → Leo, verified from the people row", [e["a"] for e in G.visible_edges(
    "p_alex", "admin", b="p_leo", types=["parent_of"])], ["p_jordan"])
check("a kid sees their own parents but no edge between two other people",
      (sorted(e["a"] for e in G.visible_edges("p_ava", "kid", b="p_ava", types=["parent_of"])),
       G.visible_edges("p_ava", "kid", b="p_leo", types=["parent_of"])), (["p_alex", "p_robin"], []))
check("a role claim is a node attr only with its node (priv-6, priv-7)",
      (G.visible_nodes("p_alex", "admin")["p_alex:morgan-reyes-coach"]["attrs"].get("role"),
       "p_robin:morgan-reyes-colleague" in G.visible_nodes("p_alex", "admin")), ("Pine Park Club coach", False))

check("a merge is told only to the merged record's owner (rel-3), never the house",
      ([(f["world_id"], f["node_id"], f["name"]) for f in G.same_as("p_alex", "admin", "household:harbor-mobile")],
       G.same_as("p_robin", "adult", "household:harbor-mobile"), G.same_as(None, "household")),
      ([("p_alex:harbor-mobile-billing", "household:harbor-mobile", "Harbor Mobile Billing")], [], []))
check("obligation nodes carry the bill's display name (account_label)",
      sorted(n["attrs"]["account_label"] for n in n1 if n["kind"] == "obligation"), ["Harbor Mobile", "Lakeside Clinic", "Pine Water"])
dec = G.visible_edges("p_alex", "admin", a="cx-meeting-club-1", types=["decided"])
check("decided (meet-1, meet-3): the meeting's item → each decision, verified, read through decisions_for",
      [(e["b"], e["state"], G.visible_nodes("p_alex", "admin")[e["b"]]["label"]) for e in dec],
      [(f"md-{MD['sig_id']}-{i}", "verified", t) for i, t in enumerate(MD["decisions"])])
check("…owner-only: nobody else sees a decision or its edge (meet-5)",
      [(p, any(x in G.visible_nodes(p, r) for x in DECISION_IDS), G.visible_edges(p, r, types=["decided"]))
       for p, r in (("p_jordan", "adult"), ("p_robin", "adult"), ("p_ava", "kid"), (None, "household"))],
      [(p, False, []) for p in ("p_jordan", "p_robin", "p_ava", None)])
check("…never by SQL here: only meeting_notes.decisions_for reads the table",
      "meeting_decisions " in (HERE / "graph_sync.py").read_text() + (HERE / "graph_edges.py").read_text().replace(
          "meeting_decisions rows", ""), False)
OB = {"lake": "obligation:lakeside clinic:2026-09-18", "harbor": "obligation:harbor mobile:2026-10-03",
      "pine": "obligation:pine water:2026-10-06"}
st = lambda p, r, k: G.obligation_state(p, r, OB[k], records=bill_records, today=TODAY)  # noqa: E731
print("   obligation states (read time):", {k: (st("p_alex", "admin", k) or {}).get("lens") for k in OB})
check("obligation state, at read time: Lakeside paid on the receipt's day for Alex (bill-2)",
      {k: v for k, v in (st("p_alex", "admin", "lake") or {}).items() if k in ("state", "on")}, {"state": "paid", "on": "2026-09-24"})
check("…Harbor: settled for Alex by his own device's text (the signal, Alex's view only)",
      {k: v for k, v in (st("p_alex", "admin", "harbor") or {}).items() if k in ("state", "on")}, {"state": "paid", "on": "2026-10-08"})
check("…with the records and the mail they were filed from: the receipt adopted through its record's candidate edge "
      "(bill-2, bill-3 via)",
      ((st("p_alex", "admin", "lake") or {}).get("items"), (st("p_alex", "admin", "harbor") or {}).get("items")),
      (["cx-mail-bill-1", "cx-mail-bill-2"], ["cx-mail-bill-3"]))
check("…Pine Water: open, due 2026-10-20, nothing paid it (bill-1: what's waiting on Alex)",
      {k: v for k, v in (st("p_alex", "admin", "pine") or {}).items() if k in ("state", "on", "items")},
      {"state": "open", "on": "2026-10-20", "items": ["cx-mail-bill-4"]})
check("…a kid and a screen get nothing (bill-4)", (st("p_ava", "kid", "harbor"), st(None, "household", "lake")), (None, None))
check("…nor an adult the bill isn't shared with", st("p_robin", "adult", "harbor"), None)
print("── P2 review (Coordination, #1425): fenced sources, edges with no sources ──")
import shutil  # noqa: E402
RV = _TMP / "review.db"
shutil.copy(_TMP / "graph.db", RV)
rc = sqlite3.connect(RV)
cur = rc.execute("INSERT INTO edges (a, type, b, plane, state, rung, conf, owner, audience, why) VALUES "
                 "('p_ava','sees','household:summit-dental','measured','accepted','attributed',0.8,'p_alex','adults','from the plan')")
rc.execute("INSERT INTO edge_prov VALUES (?,?,?,?,?,?,?,?,?)", (cur.lastrowid, "kb", "cx-note-health", GE.EXTRACTOR, None,
                                                                 "p_alex", "adults", "cx-note-health", None))
rc.execute("INSERT INTO edges (a, type, b, plane, state, rung, conf, owner, audience, why) VALUES "
           "('p_alex','knows','p_robin','typed','verified','observed',1.0,NULL,'household','no source')")
rc.commit()
rc.close()
fenced = lambda p, r: [e["why"] for e in G.visible_edges(p, r, RV, a="p_ava", types=["sees"])]  # noqa: E731
check("an edge whose only source is a fenced item: not for the viewer the fence hides it from (Jordan), though "
      "its audience (adults) would show it", (fenced("p_jordan", "adult"), fenced("p_robin", "adult")),
      ([], ["from the plan", "Ava's dentist"]))
check("an edge with no sources: never shown, to anyone", [e for p, r in VIEWERS for e in
                                                          G.visible_edges(p, r, RV, a="p_alex", b="p_robin", types=["knows"])], [])
try:
    GE.write(sqlite3.connect(":memory:"), [GE.Edge("p_alex", "knows", "p_robin", state="verified", rung="observed", conf=1,
                                                  why="x", src=[])])
    refused = False
except ValueError:
    refused = True
check("…and graph_sync refuses to write one", (refused, any(not e["prov"] for e in e1)), (True, False))
check("a world claim's fences ride on its source (sees: the health class, Ava's subject)",
      json.loads(next(p["fence"] for e in G.visible_edges("p_alex", "admin", a="p_ava", types=["sees"]) for p in e["prov"]
                      if p["fence"])).get("subject"), "p_ava")
print("── members' own addresses, keyed (#1424 → graph_edges) ──")
att = {e["a"]: (e["state"], e["rung"]) for e in G.visible_edges("p_alex", "admin", b="event:cx-calendar-dentist", types=["attends"])}
check("an attendee given as a member's own address (any case) is that member, verified · observed",
      att.get("p_alex"), ("verified", "observed"))
check("…an organiser too (Robin's pickup)", [(e["a"], e["state"]) for e in G.visible_edges(
    "p_alex", "admin", b="event:cx-calendar-pickup", types=["organises"])], [("p_robin", "verified")])
import hooks as HK  # noqa: E402
import re as _re  # noqa: E402
k_alex = HK.keyed("addr", "alex.quarry@mail.example.org")
amb = GE.Ctx(n1, x1, {}, [], [{"id": "p_alex", "addr_keys": [k_alex]}, {"id": "p_robin", "addr_keys": [k_alex]}])
check("a key two members hold (a shared mailbox) resolves to no one", amb.party("party:addr", k_alex), None)
RAW = sorted({e.lower() for p in H["people"] for e in p.get("emails") or []} |
             {str(v).lower() for it in H["items"] for k, v in (it.get("fields") or {}).items()
              if isinstance(v, str) and "@" in v} |
             {str(a).lower() for it in H["items"] for a in (it.get("fields") or {}).get("attendees") or [] if "@" in str(a)})
GB = (_TMP / "graph.db").read_bytes().lower()
check(f"none of the fixture's {len(RAW)} addresses appears in graph.db (only keys)", [a for a in RAW if a.encode() in GB], [])

print("── the local judge's labels are never a person's word (linker.statements) ──")
jc = sqlite3.connect(os.environ["LINKS_DB"])
if "source" not in {r[1] for r in jc.execute("PRAGMA table_info(link_statements)")}:
    jc.execute("ALTER TABLE link_statements ADD COLUMN source TEXT")
jc.execute("INSERT INTO link_statements (a, b, related, by, at, source) VALUES "
           "('cx-mail-bill-1','cx-mail-bill-2',0,'judge:qwen3',999,'judge')")
jc.execute("INSERT INTO link_statements (a, b, related, by, at, source) VALUES "
           "('cx-mail-club-2','cx-mail-trade-2',1,'judge:qwen3',999,'judge')")
jc.commit()
jc.close()
nj, xj, ej, fj = G.build(**SRC)
check("a judge's 'not related' doesn't reject an edge, and its 'related' makes none",
      (sorted(fmt(e) for e in ej) == sorted(fmt(e) for e in e1),
       any(e["a"] == "cx-mail-club-2" and e["b"] == "cx-mail-trade-2" for e in ej)), (True, False))

print("── P2 sources not in the fixture ──")
mcon2 = sqlite3.connect(":memory:")
mcon2.row_factory = sqlite3.Row
mcon2.execute("CREATE TABLE messages (account, owner, item_id, message_id, in_reply_to)")
mcon2.executemany("INSERT INTO messages VALUES (?,?,?,?,?)", [("a", "p_alex", "cx-mail-bill-1", "<m1@x>", None),
                                                              ("a", "p_alex", "cx-mail-bill-2", "<m2@x>", "<M1@x>"),
                                                              ("b", "p_alex", "cx-mail-bill-3", "<m3@x>", "<m1@x>")])
ctx = GE.Ctx(n1, x1, {}, [], [])
check("replies_to: the one parent by Message-ID, in the same account only",
      [(e["a"], e["b"], e["state"]) for e in GE.mail_edges(ctx, mcon2)], [("cx-mail-bill-2", "cx-mail-bill-1", "verified")])
check("two people behind one key: never guessed", (GE.Ctx.party(type("C", (), {"keys": {("party:phone", "k"): {"p_alex", "p_robin"}}})(), "party:phone", "k")), None)
print("── writes nothing it shouldn't ──")
import glob, hashlib, subprocess  # noqa: E402,E401
D = Path(tempfile.mkdtemp(prefix="titan-graph-ro-"))
env = dict(os.environ, **{k: str(D / v) for k, v in {"HOUSEHOLD_DB": "h.db", "WORLD_DIR": "w", "PLACES_DB": "p.db",
      "CONNECTORS_LEDGER": "l.db", "KB_ACL_DB": "a.db", "LINKS_DB": "k.db", "TASKS_DB": "t.db", "GRAPH_DB": "g.db",
      "FENCES_DB": "f.db", "MAIL_SERVER_DB": "m.db"}.items()})
sqlite3.connect(D / "p.db").execute("CREATE TABLE place_events (id TEXT)").connection.commit()
snap = lambda: {f: hashlib.sha1(open(f, "rb").read()).hexdigest() for f in glob.glob(str(D / "*"))}  # noqa: E731
before = snap()
r = subprocess.run([sys.executable, str(HERE / "graph_sync.py"), "--full", "--dry-run"], env=env, capture_output=True,
                   text=True, stdin=subprocess.DEVNULL)
check("a dry run writes nothing anywhere (every store read-only; no file created or changed)", (r.returncode, snap() == before),
      (0, True))
r = subprocess.run([sys.executable, str(HERE / "graph_sync.py"), "--full"], env=env, capture_output=True, text=True,
                   stdin=subprocess.DEVNULL)
pt = sorted(x[0] for x in sqlite3.connect(D / "p.db").execute("SELECT name FROM sqlite_master WHERE type='table'"))
check("a real build's only write outside graph.db: the empty places registry (CREATE TABLE IF NOT EXISTS)",
      (r.returncode, pt, sorted(Path(f).name for f in set(snap()) - set(before))), (0, ["place_events", "places"], ["g.db"]))
src = (HERE / "graph_sync.py").read_text()
check("no import-time writes, no timer, no flag yet (run by hand)",
      ("ensure_places()" in src.split("def main(")[1], "AGENT_FE_GRAPH" in src.split('"""', 2)[2]), (True, False))

# Coordination's review of #1419: the build file is 0600 the moment it exists, whatever the umask
_seen = {}
_real_replace, _real_connect = os.replace, G.sqlite3.connect
def _peek_replace(a, b):
    _seen["at_replace"] = os.stat(a).st_mode & 0o777
    _seen["siblings"] = sorted(x.name for x in Path(a).parent.iterdir() if x.name.startswith(Path(a).name + "-"))
    return _real_replace(a, b)
def _peek_connect(path, *a, **k):
    _seen["at_connect"] = os.stat(path).st_mode & 0o777
    return _real_connect(path, *a, **k)
_old_umask = os.umask(0o022)
os.replace, G.sqlite3.connect = _peek_replace, _peek_connect
try:
    leftover = _TMP / "perm" / "graph.building"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("old")
    os.chmod(leftover, 0o644)
    G.write(n1, x1, _TMP / "perm" / "graph.db")
finally:
    os.replace, G.sqlite3.connect = _real_replace, _real_connect
    os.umask(_old_umask)
check("under umask 022 the build file is 0600 before sqlite opens it, and at the replace",
      (_seen.get("at_connect"), _seen.get("at_replace")), (0o600, 0o600))
check("…no -wal/-shm/-journal sibling left at the replace", _seen.get("siblings"), [])
check("…and graph.db itself is 0600", os.stat(_TMP / "perm" / "graph.db").st_mode & 0o777, 0o600)

# Coordination's review of #1419: graph.db lives beside the stores (spec §9), never in the deploy checkout
_root = Path(__file__).resolve().parent.parent
check("the default graph.db is /ai/datasets/connectors/graph.db, not under the repo root",
      (str(G.DEFAULT_DB) == "/ai/datasets/connectors/graph.db",
       _root in G.DEFAULT_DB.resolve().parents, "titan-local-agent" in str(G.DEFAULT_DB)), (True, False, False))

import time as _time  # noqa: E402
from zoneinfo import ZoneInfo as _ZI  # noqa: E402

import local_time  # noqa: E402

_LA = _ZI("America/Los_Angeles")
_REAL = (_time.time, local_time.now)


def freeze_household(y, mo, d, h, mi):
    """Pin the household clock (and the epoch clock) to a local time; titan's own UTC day is already the next one at 23:30."""
    at = datetime(y, mo, d, h, mi, tzinfo=_LA)
    _time.time = lambda: at.timestamp()
    local_time.now = lambda name=None: at.astimezone(local_time.zone(name) or _LA)
    return at


def unfreeze_household():
    _time.time, local_time.now = _REAL

print("\n── the household's day, not titan's UTC day (#1567) ──")
import obligations as _O  # noqa: E402
_real_threads = _O.threads
for _label, _d, _hh, _mm in (("23:30", 9, 23, 30), ("00:30", 10, 0, 30)):
    _seen = []
    _O.threads = lambda recs, today, *a, **k: (_seen.append(today), _real_threads(recs, today, *a, **k))[1]
    freeze_household(2026, 10, _d, _hh, _mm)
    try:
        G.obligation_nodes(bill_records)
    finally:
        unfreeze_household()
        _O.threads = _real_threads
    check(f"obligation threads settle on the household's day at {_label} household time", _seen, [date(2026, 10, _d)])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
