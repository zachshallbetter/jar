#!/usr/bin/env python3
# gate: hermetic
"""Delete my data with a 7-day undo (#1080, with #903's lineage): an adult's chats, memories, KB notes, thread titles,
Claude conversations, lineage and export move into a tombstone; recall and retrieval stop seeing them at once; Cancel
puts back exactly what was there; the purge deletes it after 7 days. Only the caller's rows: another adult's and a
kid's stay. Kids: "ask a parent"; a shared screen: 403. Fictional people (Robin, an adult; Jordan, another adult;
Ava, a kid); temporary stores everywhere; the KB and its runtime are fakes; no model.
Run: python3 scripts/test_me_delete.py"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-me-delete-"))
for k, v in {"AGENT_MEMORY_DIR": "memory", "ME_DELETE_DIR": "tomb", "EXPORT_DIR": "export", "KB_ACL_DB": "kb_acl.db",
             "SPACES_DB": "spaces.db", "CHAT_TITLES_DB": "titles.db", "CLAUDE_CHATS_DB": "claude.db",
             "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json", "WORLD_DIR": "world",
             "PLACES_DB": "places.db", "UPLOADS_DB": "uploads.db", "UPLOADS_DIR": "uploads"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["MEMORY_HYBRID"] = "0"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_memory  # noqa: E402
import chat_judge  # noqa: E402
import claude_chats  # noqa: E402
import connectors_runner  # noqa: E402
import household_identity as hh  # noqa: E402
import kb_privacy  # noqa: E402
import me_delete as X  # noqa: E402
import places  # noqa: E402
import spaces  # noqa: E402
import uploads  # noqa: E402
import world  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


ROBIN, JORDAN, AVA = "p_robin", "p_jordan", "p_ava"
LEDGER = _TMP / "ledger.db"

# ── the fake KB: retrieval keyed by source_id, chunks hold the metadata ──
KB: dict[str, dict] = {}
DOWN = {"on": False}


def call(service, path, method="POST", payload=None, timeout=30):
    import urllib.parse
    sid = urllib.parse.unquote(path.split("/sources/")[1].split("/")[0])
    if method == "DELETE":
        KB.pop(sid, None)
        return {}
    if DOWN["on"]:
        raise OSError("retrieval down")
    s = KB[sid]
    return {"chunks": [{"chunk_index": 0, "chunk_text": s["text"], "metadata": s["metadata"]}]}


def kb_index(sid, title, text, source_type, metadata=None, **_):
    KB[sid] = {"source_id": sid, "title": title, "text": text, "source_type": source_type, "metadata": dict(metadata or {})}


def kb_sources():
    return [{"source_id": s["source_id"], "title": s["title"], "source_type": s["source_type"],
             "metadata": s["metadata"]} for s in KB.values()]


def note(sid, owner, audience, title, text):
    kb_index(sid, title, text, "note", {"owner": owner, "audience": audience})
    kb_privacy.set_acl(sid, owner, audience, by=owner, why="fixture")


note("note-r1", ROBIN, "private", "Packing list", "Sunscreen, two towels.")
note("note-r2", ROBIN, "household", "Recycling days", "Blue bin on Tuesdays.")
note("cx-notes-r3", ROBIN, "private", "Garden plan", "Tomatoes by the fence.")
note("note-j1", JORDAN, "private", "Jordan's list", "Paint the shed.")
note("note-a1", AVA, "private", "Ava's poem", "The moon is round.")

# the synced item's ledger row
con = connectors_runner._db(LEDGER)
con.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, synced_at, owner, upstream_id) "
            "VALUES ('notes', 'robin:garden', 'h', 'cx-notes-r3', 1, 0, ?, ?, 'garden')", (time.time(), ROBIN))
con.commit()
con.close()

# ── memory: Robin's chat, facts, a forgotten fact and a scene; Jordan's and Ava's too ──
mem = agent_memory.AgentMemory()
for pid in (ROBIN, JORDAN, AVA):
    conv = hh.conv_id_for(pid)
    mem.add_message(conv, "user", f"hello from {pid}")
    mem.add_message(conv, "assistant", "hi")
    mem.add_fact(conv, f"{pid} likes green tea in the morning.", person_id=pid)
    mem.add_fact(conv, f"{pid} parks on the left side of the drive.", person_id=pid)
fid = mem.list_facts(person_id=ROBIN)[1]["id"]
mem.forget_fact(fid, person_id=ROBIN)
scene = Path(agent_memory._REFS_DIR) / "scene-r.md"
scene.write_text("# Robin's scene")
mem._db.execute("INSERT INTO scenes (id, conv_id, title, md_path, ts) VALUES ('scene-r', ?, 'A scene', ?, ?)",
                (hh.conv_id_for(ROBIN), str(scene), time.time()))
mem._db.commit()

spaces.note_branch(ROBIN, "c-branch", {"parent": "c-root", "upto": "m1"})
spaces.note_branch(JORDAN, "c-jb", {"parent": "c-jroot", "upto": None})
spaces.tag(ROBIN, "c-branch", "pr-1", None)
spaces.tag(JORDAN, "c-jb", "pr-2", None)
chat_judge.save_title(ROBIN, "t-1", "Trip to the coast")
chat_judge.save_title(JORDAN, "t-2", "Shed")
claude_chats.store(ROBIN, "claude-ai", [{"conv_id": "cc-1", "title": "Bread", "messages": [{"role": "user", "text": "Sourdough?"}]}])
claude_chats.store(JORDAN, "claude-ai", [{"conv_id": "cc-2", "title": "Bikes", "messages": [{"role": "user", "text": "Gears?"}]}])
# world: Robin's private evidence (one claim only Robin backs, one Jordan backs too), a household fact Robin observed,
# Robin's mention and learned alias; Jordan's own private fact
W = world.get()


def claim(bubble, cid, entity, attr, value, *ev):
    con = W.world(bubble).con
    con.execute("INSERT INTO claims (id, entity_id, attr, value_norm, value, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (cid, entity, attr, value.lower(), value, 1.0, 1.0))
    for i, (owner, aud, ref) in enumerate(ev):
        con.execute("INSERT INTO evidence (claim_id, source, kind, ref, origin, observed_at, prior, owner, audience, added_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (cid, "chat", "said", ref, "chat", "2026-10-01", 0.8, owner, aud, 1.0))
    con.execute("INSERT INTO claim_log (claim_id, to_state, at, by) VALUES (?, 'current', 1.0, 'test')", (cid,))
    con.commit()


claim(ROBIN, 1, "e-gym", "opens", "6am", (ROBIN, "private", "m-1"))
claim("household", 2, "e-bins", "day", "Tuesday", (ROBIN, "household", "m-2"))
claim("household", 3, "e-shed", "color", "green", (ROBIN, "private", "m-3"), (JORDAN, "private", "m-4"))
claim(JORDAN, 4, "e-bike", "gears", "21", (JORDAN, "private", "m-5"))
hc = W.world("household").con
hc.execute("INSERT INTO mentions (entity_id, person, at) VALUES ('e-bins', ?, 1.0), ('e-bins', ?, 1.0)", (ROBIN, JORDAN))
hc.execute("INSERT INTO aliases (entity_id, alias_norm, alias, speaker) VALUES ('e-bins', 'the bins', 'the bins', ?)", (ROBIN,))
hc.commit()
# places and uploads
places.record("pe-1", ROBIN, "arrived", "home", "2026-10-05T08:00:00Z")
places.record("pe-2", JORDAN, "arrived", "home", "2026-10-05T09:00:00Z")
places.set_mode(ROBIN, "dev-robin", "Robin's iPhone", "always", True)
places.set_mode(JORDAN, "dev-jordan", "Jordan's iPhone", "off", None)
st_r, up_r = uploads.save(ROBIN, "scan.txt", b"receipt text")
st_j, up_j = uploads.save(JORDAN, "list.txt", b"jordan text")

exp = _TMP / "export" / ROBIN
exp.mkdir(parents=True)
(exp / "export-1.zip").write_bytes(b"zip")

parts = X.Parts(mem, kb_sources=kb_sources, call=call, kb_index=kb_index, ledger=LEDGER)


def snapshot():
    db = mem._db
    db.row_factory = sqlite3.Row
    out = {t: sorted(tuple(r) for r in db.execute(f"SELECT * FROM {t}")) for t in ("messages", "facts", "forgotten_facts", "scenes")}
    out["kb"] = {k: (v["title"], v["text"], v["metadata"]) for k, v in KB.items()}
    out["acl"] = {k: v for k, v in kb_privacy.acl_rows().items()}
    out["lineage"] = sorted(tuple(r) for r in spaces._db().execute("SELECT person, conversation, parent, upto FROM lineage"))
    out["filings"] = sorted(tuple(r) for r in spaces._db().execute("SELECT person, conversation, project FROM tags"))
    out["titles"] = sorted(tuple(r) for r in chat_judge._titles_db().execute("SELECT person, thread, title FROM titles"))
    cc = claude_chats._db()
    out["claude"] = (sorted(tuple(r) for r in cc.execute("SELECT owner, conv_id, title FROM convs")),
                     sorted(tuple(r) for r in cc.execute("SELECT owner, conv_id, seq, text FROM msgs")))
    lg = connectors_runner._db(LEDGER)
    out["ledger"] = (lg.execute("SELECT deleted_at IS NULL FROM items").fetchall(), lg.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0])
    out["scene_file"] = scene.exists()
    out["world"] = {b: (sorted(tuple(r) for r in W.world(b).con.execute("SELECT id, entity_id, attr, value FROM claims")),
                        sorted(tuple(r) for r in W.world(b).con.execute("SELECT claim_id, owner, audience, ref FROM evidence")),
                        sorted(tuple(r) for r in W.world(b).con.execute("SELECT claim_id, to_state FROM claim_log")),
                        sorted(tuple(r) for r in W.world(b).con.execute("SELECT entity_id, person FROM mentions")),
                        sorted(tuple(r) for r in W.world(b).con.execute("SELECT entity_id, alias, speaker FROM aliases")))
                    for b in W.bubbles()}
    pc = places._db()
    out["places"] = sorted(tuple(r) for r in pc.execute("SELECT id, person, place FROM place_events"))
    places._modes(pc)
    out["modes"] = sorted(tuple(r) for r in pc.execute("SELECT device, person, value FROM location_modes"))
    uc = uploads._db()
    out["uploads"] = (sorted(tuple(r) for r in uc.execute("SELECT id, owner, name FROM uploads")),
                      sorted(p.name for p in uploads.UPLOADS_DIR.iterdir()) if uploads.UPLOADS_DIR.exists() else [])
    return out


before = snapshot()

print("── refused ──")
for who, role, status in ((AVA, "kid", 403), ("house", "household", 403), (None, "admin", 403)):
    try:
        X.delete(who, role, parts, conv="x", confirm="delete")
        check(f"{role}: refused", "deleted", status)
    except X.Refused as e:
        check(f"{role}: refused", (e.status, e.detail if role == "kid" else ""), (status, "ask a parent" if role == "kid" else ""))
try:
    X.delete(ROBIN, "adult", parts, conv=hh.conv_id_for(ROBIN), confirm="yes")
    check("no confirm: 400", "deleted", 400)
except X.Refused as e:
    check("no confirm: 400", e.status, 400)

DOWN["on"] = True
try:
    X.delete(ROBIN, "adult", parts, conv=hh.conv_id_for(ROBIN), confirm="delete")
    check("KB unreadable: 503", "deleted", 503)
except X.Refused as e:
    check("KB unreadable: 503", e.status, 503)
DOWN["on"] = False
check("…and nothing changed, no tombstone left", (snapshot() == before, X.state(ROBIN, "adult")["state"]), (True, "none"))

print("── delete ──")
now = time.time()
st = X.delete(ROBIN, "adult", parts, conv=hh.conv_id_for(ROBIN), confirm="delete", now=now)
check("pending, purge in 7 days, with counts", (st["state"], round(st["purge_after"] - now), st["counts"]),
      ("pending", 7 * 86400, {"chats_messages": 2, "memories": 2, "notes": 3, "claude_conversations": 1,
                                       "world_facts": 2, "place_events": 1, "uploads": 1}))
after = snapshot()
check("Robin's messages, facts, forgotten and scenes are gone; Jordan's and Ava's stay",
      (sorted(m[1] for m in after["messages"]), sorted({f[-1] for f in after["facts"] if isinstance(f[-1], str)} & {ROBIN, JORDAN, AVA}),
       len(after["forgotten_facts"]), after["scenes"], after["scene_file"]),
      (sorted([hh.conv_id_for(JORDAN)] * 2 + [hh.conv_id_for(AVA)] * 2), [AVA, JORDAN], 0, [], False))
def recalled(q):
    r = mem.recall(q, person_id=ROBIN)
    return (r.prependContext or "") + (r.appendSystemContext or "")


check("recall stops seeing them at once", "parks" in recalled("parks left side drive"), False)
check("KB: Robin's notes (and the one shared with the household) are gone; others' stay",
      sorted(after["kb"]), ["note-a1", "note-j1"])
check("…their access rows too", sorted(k for k in after["acl"] if k.endswith(("r1", "r2", "r3"))), [])
check("the synced item is held so the device doesn't send it back", after["ledger"], ([(0,)], 1))
check("filings: only Robin's gone", [r[0] for r in after["filings"]], [JORDAN])
check("lineage (#903), thread titles and Claude chats: only Robin's gone",
      ([r[0] for r in after["lineage"]], [r[0] for r in after["titles"]], [r[0] for r in after["claude"][0]],
       sorted({r[0] for r in after["claude"][1]})), ([JORDAN], [JORDAN], [JORDAN], [JORDAN]))
check("the export archive is gone", exp.exists(), False)
check("world: Robin's private claim and evidence go; the household fact Robin saw stays; a claim Jordan also backs "
      "keeps Jordan's evidence; Jordan's own stays",
      ({b: [c[1] for c in v[0]] for b, v in after["world"].items()},
       {b: [e[1] for e in v[1]] for b, v in after["world"].items()}),
      ({ROBIN: [], "household": ["e-bins", "e-shed"], JORDAN: ["e-bike"]},
       {ROBIN: [], "household": [ROBIN, JORDAN], JORDAN: [JORDAN]}))
check("world: Robin's mentions and aliases go, Jordan's stay",
      (after["world"]["household"][3], after["world"]["household"][4]), ([("e-bins", JORDAN)], []))
check("places: only Robin's events go", [r[1] for r in after["places"]], [JORDAN])
check("…and only Robin's phone's location mode (#1133)", [r[1] for r in after["modes"]], [JORDAN])
check("uploads: Robin's row and file go; Jordan's stay", (after["uploads"][0], after["uploads"][1]),
      ([(up_j["id"], JORDAN, "list.txt")], [up_j["id"]]))
tomb = X.tomb_dir(ROBIN)
check("the tombstone is private (0700 folder, 0600 file)",
      (oct(tomb.stat().st_mode & 0o777), oct((tomb / "tomb.db").stat().st_mode & 0o777)), ("0o700", "0o600"))
try:
    X.delete(ROBIN, "adult", parts, conv=hh.conv_id_for(ROBIN), confirm="delete")
    check("a second delete: 409", "deleted", 409)
except X.Refused as e:
    check("a second delete: 409", e.status, 409)
check("another person sees no tombstone of theirs", X.state(JORDAN, "adult")["state"], "none")
try:
    X.cancel(JORDAN, "adult", parts)
    check("another adult can't cancel Robin's", "cancelled", 404)
except X.Refused as e:
    check("another adult can't cancel Robin's", e.status, 404)

print("── cancel ──")
# while it's pending, Robin's bubble learns something new and SQLite hands it the freed claim id 1
rc = W.world(ROBIN).con
rc.execute("INSERT INTO claims (entity_id, attr, value_norm, value, created_at, updated_at) VALUES "
           "('e-pool', 'opens', '7am', '7am', 2.0, 2.0)")
rc.commit()
new_id = rc.execute("SELECT id FROM claims WHERE entity_id='e-pool'").fetchone()[0]
got = X.cancel(ROBIN, "adult", parts)
pool = rc.execute("SELECT id FROM claims WHERE entity_id='e-pool'").fetchone()[0]
gym = rc.execute("SELECT id FROM claims WHERE entity_id='e-gym'").fetchone()[0]
check("cancel never overwrites a claim id reused meantime: the new fact keeps it, the restored one gets another, "
      "and its evidence follows", (new_id, pool, gym != pool,
                                   [tuple(r) for r in rc.execute("SELECT owner FROM evidence WHERE claim_id=?", (gym,))]),
      (1, 1, True, [(ROBIN,)]))
rc.execute("DELETE FROM claims WHERE entity_id='e-pool'")
rc.execute("UPDATE claims SET id=1 WHERE entity_id='e-gym'")
rc.execute("UPDATE evidence SET claim_id=1 WHERE claim_id=?", (gym,))
rc.execute("UPDATE claim_log SET claim_id=1 WHERE claim_id=?", (gym,))
rc.commit()
back = snapshot()
for key in ("world", "places", "modes", "uploads", "messages", "facts", "forgotten_facts", "scenes", "kb", "lineage", "filings", "titles", "claude", "scene_file"):
    check(f"cancel puts back exactly: {key}", back[key], before[key])
check("cancel: access rows back", {k: v[:2] for k, v in back["acl"].items()}, {k: v[:2] for k, v in before["acl"].items()})
check("cancel: the synced item is released", back["ledger"], ([(1,)], 0))
check("cancel: no tombstone left", (got["state"], tomb.exists(), X.state(ROBIN, "adult")["state"]), ("restored", False, "none"))
check("recall sees them again", "parks" in recalled("parks left side drive"), True)

print("── purge ──")
X.delete(ROBIN, "adult", parts, conv=hh.conv_id_for(ROBIN), confirm="delete", now=now)
check("day 6: waiting", X.purge(now + 6 * 86400), {"due": 0, "purged": 0, "waiting": 1})
check("day 8, dry run: due, untouched", (X.purge(now + 8 * 86400, dry_run=True)["due"], tomb.exists()), (1, True))
check("day 8: purged for good", (X.purge(now + 8 * 86400)["purged"], tomb.exists()), (1, False))
check("…and the synced item stays held", snapshot()["ledger"], ([(0,)], 1))
try:
    X.cancel(ROBIN, "adult", parts)
    check("after the purge, nothing to cancel", "cancelled", 404)
except X.Refused as e:
    check("after the purge, nothing to cancel", e.status, 404)

print("── routes ──")
check("adults, kids (to hear 'ask a parent') and the admin reach it; a shared screen doesn't",
      [hh.route_allowed(r, "POST", "/v1/me/delete") for r in ("admin", "adult", "kid", "household")],
      [True, True, True, False])
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
