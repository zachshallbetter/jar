#!/usr/bin/env python3
"""The contacts connector's titan side (#1380), over the golden household (graph_golden_load: temp stores) with
fictional contacts added here, not to household.json:
- the boundary: a field titan doesn't keep is refused (naming the field, never a value), and a kid's sync is refused;
- never raw: after every sync, no temp store holds a contact's phone digits or email address;
- the viewer matrix: a contact is its owner's only (another adult, a kid and a shared screen never see it);
- fusion: a contact is a household member only on a shared keyed phone or email, or the owner's word; the two Morgan
  Reyes (Alex's coach, Robin's colleague) stay two people;
- relations: the owner's card's Related Names land on the contact they name;
- deletes: a delete, and a full listing that no longer has a contact, leave nothing (entity, claims, aliases, merges,
  reference);
- the measurement the design requires first: attributing messages to people by key against by name. Counts only;
  key must win.
Fictional people only. Run: TZ=UTC python3 scripts/test_contacts_connector.py
"""
# gate: hermetic
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_golden_load as GL  # noqa: E402  (first: points every store at a temp dir and loads the fixture)
import contacts_connector as C  # noqa: E402
import graph_edges as GE  # noqa: E402
import graph_sync as G  # noqa: E402
import hooks  # noqa: E402
import world  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": {detail}"))


W = world.Bubbles(GL.TMP / "world")
REFS = GL.TMP / "refs-contacts.db"
DAY = "2026-10-09"
ALEX = [
    {"id": "A1", "given": "Morgan", "family": "Reyes", "phones": ["+1 (555) 010-0077"]},
    {"id": "A2", "given": "Dana", "family": "Pike", "emails": ["Dana@PikePlumbing.example"]},
    {"id": "A3", "given": "Robin", "family": "Q", "phones": ["555-010-0001"]},           # Robin's own phone (golden)
    {"id": "A4", "given": "Jordan", "family": "Hale", "nickname": "Jo", "phones": ["555 010 0066"],
     "emails": ["Jo@HaleFamily.example"]},   # no member's phone or address
]
ALEX_REL = [{"label": "Coach", "name": "Morgan Reyes"}]
ROBIN = [{"id": "R1", "given": "Morgan", "family": "Reyes", "phones": ["555 010 0088"]}]
RAW = ["5550100077", "0100077", "010-0077", "5550100088", "0100088", "010 0088", "5550100066", "0100066", "010 0066", "halefamily", "HaleFamily",
       "pikeplumbing", "PikePlumbing"]


def ent(owner, nid):
    return f"{world.bubble_name(owner)}:{C.entity_for(owner, nid)}"


def rows_about(owner, nid):
    ww = W.world(world.bubble_name(owner))
    eid = C.entity_for(owner, nid)
    q = {"entities": "SELECT COUNT(*) FROM entities WHERE id=?", "claims": "SELECT COUNT(*) FROM claims WHERE entity_id=?",
         "aliases": "SELECT COUNT(*) FROM aliases WHERE entity_id=?",
         "merges": "SELECT COUNT(*) FROM merges WHERE src=? OR dst=?"}
    return {k: ww.con.execute(s, (eid, eid) if s.count("?") == 2 else (eid,)).fetchone()[0] for k, s in q.items()}


INVITE_RAW = {"halefamily", "HaleFamily"}


def raw_hits():
    """Every temp store, byte for byte: any raw phone digits or address of a contact."""
    hits = []
    for p in GL.TMP.rglob("*"):
        if p.is_file():
            b = p.read_bytes()
            hits += [f"{p.name}:{r}" for r in RAW if r.encode() in b
                     and not (p.name == "ledger.db" and r in INVITE_RAW)]   # the test's own invite carries its address
    return hits


def graph():
    """Rebuild the projection from the stores, and the resolution context graph_sync builds edges with."""
    path = GL.build()
    nodes, xw = G.collect()
    reg = G.registry_rows()
    G.finish(nodes, reg)
    ctx = GE.Ctx(nodes, xw, G._world_rows(GL.TMP / "world"), reg, G.people_rows())
    return path, ctx


print("── the boundary ──")
for bad, why in (([{"id": "X", "given": "Ava", "birthday": "2015-01-01"}], "birthday"),
                 ([{"id": "X", "given": "Ava", "postal": "1 Main St"}], "postal")):
    try:
        C.ingest("p_alex", "admin", "mac-alex", bad, refs_path=REFS, w=W)
        check(f"a contact with `{why}` is refused", False, "accepted")
    except C.ContactError as e:
        check(f"a contact with `{why}` is refused, naming the field only", why in str(e) and "Main" not in str(e)
              and "2015" not in str(e), str(e))
try:
    C.ingest("p_ava", "kid", "mac-ava", [{"id": "K1", "given": "Leo"}], refs_path=REFS, w=W)
    check("a kid's sync is refused", False, "accepted")
except C.ContactError:
    check("a kid's sync is refused", True)
check("nothing was written by a refused sync", rows_about("p_alex", "X")["entities"] == 0)

print("── a sync ──")
n = C.ingest("p_alex", "admin", "mac-alex", ALEX, ALEX_REL, observed_at=DAY, refs_path=REFS, w=W)
check("Alex's four contacts", n["contacts"] == 4, n)
C.ingest("p_robin", "adult", "mac-robin", ROBIN, observed_at=DAY, refs_path=REFS, w=W)
check("never raw: no store holds a contact's phone or address", not raw_hits(), raw_hits()[:4])
aw = W.world(world.bubble_name("p_alex"))
keys = {r["value"] for r in aw.con.execute("SELECT value FROM claims WHERE entity_id=? AND attr='phone_key'",
                                           (C.entity_for("p_alex", "A1"),))}
check("the coach's phone is kept as the hook key", keys == {hooks.keyed("phone", "5550100077")}, keys)
rel = aw.con.execute("SELECT value FROM claims WHERE entity_id=? AND attr='relation'",
                     (C.entity_for("p_alex", "A1"),)).fetchall()
check("the owner's card's relation lands on the contact it names", [r["value"] for r in rel] == ["coach"], rel)
check("the same sync again changes nothing (idempotent)",
      C.ingest("p_alex", "admin", "mac-alex", ALEX, ALEX_REL, observed_at=DAY, refs_path=REFS, w=W)["claims"] == n["claims"])

print("── the measurement: by key against by name ──")
P, ctx = graph()
# (viewer, role, hook kind, raw sender, display name, who it really is (None: nobody the viewer knows))
MSGS = [
    ("p_alex", "admin", "party:phone", "+1 555 010 0077", "Morgan", ent("p_alex", "A1")),
    ("p_robin", "adult", "party:phone", "555-010-0088", "Morgan", ent("p_robin", "R1")),
    ("p_alex", "admin", "party:addr", "dana@pikeplumbing.example", "Dana Pike", ent("p_alex", "A2")),
    ("p_alex", "admin", "party:phone", "+1 555 010 0001", "Robin", "p_robin"),
    ("p_alex", "admin", "party:phone", "+1 555 010 0099", "Morgan", None),
    ("p_robin", "adult", "party:phone", "+1 555 010 0077", "Morgan", None),        # Alex's coach: not Robin's to know
]


def by_key(viewer, role, kind, raw, seen):
    digits = "".join(ch for ch in raw if ch.isdigit())
    key = hooks.keyed("phone", digits[-10:]) if kind == "party:phone" else hooks.keyed("addr", raw.lower())
    node = ctx.party(kind, key)
    return node if node in seen else None


def by_name(viewer, role, name, seen):
    """The comparison baseline (not a rule titan uses: #1428 decided against any name rule, wrong 18/18 live): an exact
    full name, else a unique first name, among the people the viewer sees."""
    people = [n for n in seen.values() if n["kind"] == "person" and n["id"] != viewer]
    full = [n["id"] for n in people if (n.get("label") or "").lower() == name.lower()]
    if len(full) == 1:
        return full[0]
    first = [n["id"] for n in people if (n.get("label") or "").lower().split(" ")[:1] == [name.lower()]]
    return first[0] if len(first) == 1 else None


tally = {"key": {"right": 0, "wrong": 0, "none": 0}, "name": {"right": 0, "wrong": 0, "none": 0}}
for viewer, role, kind, raw, name, truth in MSGS:
    seen = G.visible_nodes(viewer, role, P)
    for how, got in (("key", by_key(viewer, role, kind, raw, seen)), ("name", by_name(viewer, role, name, seen))):
        tally[how]["right" if got == truth else "none" if got is None else "wrong"] += 1
print(f"  attribution over {len(MSGS)} messages: by key {tally['key']}, by name {tally['name']}")
check("by key gets more right than by name", tally["key"]["right"] > tally["name"]["right"], tally)
check("by key is never wrong (it says nothing rather than guess)", tally["key"]["wrong"] == 0, tally)

print("── fusion and the viewer matrix ──")
nodes = {n["id"] for n in G.collect()[0]}
check("the two Morgan Reyes are two people", ent("p_alex", "A1") in nodes and ent("p_robin", "R1") in nodes)
check("Alex's 'Robin Q' is Robin (her own phone): no separate node", ent("p_alex", "A3") not in nodes)
fused = {f["world_id"]: f for f in G.world_fusion(GL.TMP / "world")[2]}
check("…fused by key, and only Alex's record says so",
      fused.get(ent("p_alex", "A3"), {}).get("why") == "the same phone"
      and fused[ent("p_alex", "A3")]["owner"] == "p_alex", fused.get(ent("p_alex", "A3")))
check("a name alone fuses nobody: Alex's 'Jordan Hale' (a phone no member has) stays his own", ent("p_alex", "A4") in nodes)
for who, role in (("p_robin", "adult"), ("p_ava", "kid"), (None, "household")):
    seen = set(G.visible_nodes(who, role, P))
    check(f"{who or 'a shared screen'} sees none of Alex's contacts",
          not ({ent("p_alex", x) for x in ("A1", "A2", "A4")} & seen))
check("Alex sees his own", {ent("p_alex", x) for x in ("A1", "A2", "A4")} <= set(G.visible_nodes("p_alex", "admin", P)))

print("── the owner's word ──")
C.same_as_member("p_alex", "A4", "p_jordan", observed_at=DAY, w=W)
fused = {f["world_id"]: f for f in G.world_fusion(GL.TMP / "world")[2]}
check("'that's Jordan' fuses Alex's contact into p_jordan", fused.get(ent("p_alex", "A4"), {}).get("node_id") == "p_jordan"
      and fused[ent("p_alex", "A4")]["why"] == "the owner's word", fused.get(ent("p_alex", "A4")))

# KB's must-fix (#1445 review): Alex's private word and key must never attribute anyone else's text to Jordan.
hcon = hooks.connect()
for iid, owner in (("dev-text-ct-robin", "p_robin"), ("dev-text-ct-alex", "p_alex")):
    for kind, key in (("date:item", DAY), ("party:phone", hooks.keyed("phone", "5550100066"))):
        hcon.execute("INSERT INTO hooks (item_id, source, kind, key, at, locator, owner, audience, made_at, version) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (iid, "text", kind, key, DAY, "{}", owner, "private", 0, 1))
hcon.commit()
import connectors_runner as cx  # noqa: E402
lcon = cx._db()   # a household invite (Robin's calendar) listing that address as an attendee and its organiser
lcon.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner, upstream_id, observed_at) "
             "VALUES (?,?,?,?,?,?,?,?)", ("calendar", "cx-ct-invite", "h", "cx-ct-invite", 0, "p_robin", "cx-ct-invite", DAY))
lcon.execute("INSERT INTO versions (source, source_id, item_id, change, title, fields, owner, audience, at) "
             "VALUES (?,?,?,?,?,?,?,?,?)", ("calendar", "cx-ct-invite", "cx-ct-invite", "created", "Planning call",
                                            json.dumps({"start": DAY + "T15:00:00-07:00", "end": DAY + "T16:00:00-07:00",
                                                        "attendees": ["p_robin", "jo@halefamily.example"],
                                                        "organiser": "Jo@HaleFamily.example"}),
                                            "p_robin", "household", 0))
lcon.commit()
P = GL.build()


def from_jordan(viewer, role, iid):
    return any(e["a"] == iid and e["type"] == "from" and e["b"] == "p_jordan" for e in G.visible_edges(viewer, role, P, states=("accepted", "candidate", "verified", "rejected")))


check("Alex's own text from that number is from Jordan, for Alex", from_jordan("p_alex", "admin", "dev-text-ct-alex"))
for who, role in (("p_robin", "adult"), ("p_jordan", "adult"), ("p_ava", "kid"), (None, "household")):
    check(f"Robin's text from that number is not from Jordan for {who or 'a shared screen'}",
          not from_jordan(who, role, "dev-text-ct-robin"))
    check(f"…nor is Alex's, for {who or 'a shared screen'}", not from_jordan(who, role, "dev-text-ct-alex"))


def on_invite(viewer, role):
    return sorted(e["type"] for e in G.visible_edges(viewer, role, P, states=("accepted", "candidate", "verified", "rejected"))
                  if e["a"] == "p_jordan" and e["b"] == "event:cx-ct-invite")


check("the household invite's event is there for everyone", all(
    "event:cx-ct-invite" in G.visible_nodes(w_, r_, P) for w_, r_ in (("p_alex", "admin"), ("p_robin", "adult"), (None, "household"))))
check("Alex sees Jordan attend and organise the invite (his own word, his own key)", on_invite("p_alex", "admin") == ["attends", "organises"],
      on_invite("p_alex", "admin"))
for who, role in (("p_robin", "adult"), ("p_jordan", "adult"), ("p_ava", "kid"), (None, "household")):
    check(f"…for {who or 'a shared screen'}, Jordan neither attends nor organises it", not on_invite(who, role),
          on_invite(who, role))

print("── deletes ──")
eid2 = C.entity_for("p_alex", "A2")
aw = W.world(world.bubble_name("p_alex"))
claims2 = [r[0] for r in aw.con.execute("SELECT id FROM claims WHERE entity_id=?", (eid2,))]
with aw.con:   # what a turn, a gap, an alias, a prepared answer and a review would have left about the contact
    aw.con.execute("INSERT INTO asks (claim_id, said_by, audience, title, created_at, entity_id) VALUES (?,?,?,?,?,?)",
                   (claims2[0], "p_alex", "private", "Dana Pike's email?", 0, eid2))
    aw.con.execute("INSERT INTO claim_log (claim_id, to_state, at, by) VALUES (?,?,?,?)", (claims2[0], "confirmed", 0, "p_alex"))
    aw.con.execute("INSERT INTO gaps (entity_id, attr, person, at) VALUES (?,?,?,?)", (eid2, "phone", "p_alex", 0))
    aw.con.execute("INSERT INTO mentions (entity_id, person, at) VALUES (?,?,?)", (eid2, "p_alex", 0))
    aw.con.execute("INSERT INTO alias_learn (speaker, phrase_norm, phrase, entity_id, turn_id, at) VALUES (?,?,?,?,?,?)",
                   ("p_alex", "the plumber", "the plumber", eid2, "t1", 0))
    aw.con.execute("INSERT INTO prepared (person, day, entity, attr, line, made) VALUES (?,?,?,?,?,?)",
                   ("p_alex", DAY, eid2, "phone", "Dana Pike: on record", 0))
    aw.con.execute("INSERT INTO note_reviews (person, kind, source_id, title, entity, made) VALUES (?,?,?,?,?,?)",
                   ("p_alex", "fact", "n1", "Dana Pike", eid2, 0))
C.delete("p_alex", "mac-alex", "A2", w=W, refs_path=REFS)


def anywhere(con, needle):
    """Every table and text column of a bubble file holding `needle` (an entity id)."""
    out = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for col in [c[1] for c in con.execute(f"PRAGMA table_info({t})")]:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE CAST({col} AS TEXT) LIKE ?", (f"%{needle}%",)).fetchone()[0]
            if n:
                out.append(f"{t}.{col}={n}")
    return out


check("a deleted contact's id is in no table or column of the bubble file", not anywhere(aw.con, eid2), anywhere(aw.con, eid2))
check("…nor its claims' logs, asks or evidence", bool(claims2) and all(aw.con.execute(f"SELECT COUNT(*) FROM {t} WHERE claim_id IN ({','.join('?' * len(claims2))})",
                                                          claims2).fetchone()[0] == 0 for t in ("claim_log", "asks", "evidence")))
check("a deleted contact leaves nothing in the world", rows_about("p_alex", "A2") == {"entities": 0, "claims": 0,
                                                                                      "aliases": 0, "merges": 0},
      rows_about("p_alex", "A2"))
import references  # noqa: E402
check("…nor a reference", "A2" not in references.native_ids("p_alex", "mac-alex", C.KIND, path=REFS))
r = C.ingest("p_alex", "admin", "mac-alex", [ALEX[0], ALEX[2]], ALEX_REL, full=True, observed_at=DAY, refs_path=REFS, w=W)
check("a full listing without A4 deletes it, word and all", r["deleted"] == 1 and rows_about("p_alex", "A4")["entities"] == 0
      and rows_about("p_alex", "A4")["claims"] == 0, (r, rows_about("p_alex", "A4")))
check("still never raw after the deletes", not raw_hits(), raw_hits()[:4])
check("ids are deterministic", C.entity_for("p_alex", "A1") == C.entity_for("p_alex", "A1"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
