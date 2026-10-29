#!/usr/bin/env python3
# gate: hermetic
"""graph_sync --since (#1426): the change gate is the graph's own content digest. Over the golden household
(graph_golden_load, temp stores), with same-count, in-place edits in every store the graph reads:
  - an unchanged house: --since writes nothing (graph.db's bytes and mtime unchanged);
  - each edit (a share flip, a claim value, a place rename, a ledger version, a link statement, a replaced hook, a
    place event, a meeting decision): --since writes, and its hash is a fresh --full's;
  - read-time state isn't in graph.db: a fence flip or a person's word on a bill writes nothing, yet what the viewer
    gets changes at once;
  - a --since dry run never writes.
Run: python3 scripts/test_graph_since.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph_golden_load as L  # noqa: E402  (sets every store path to a temp dir and loads the fixture)

import graph_sync as G  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


SRC = dict(records=L.bill_records, today=date.fromisoformat(L.H["now"][:10]))
GDB = L.TMP / "graph.db"


def fresh() -> str:
    """What a fresh --full would record now (built in memory, nothing written)."""
    n, x, e, f = G.build(**SRC)
    return G.digest(n, x, e, f)


def stamp() -> tuple[int, bytes]:
    return os.stat(GDB).st_mtime_ns, GDB.read_bytes()


print("── first run, then an unchanged house ──")
r = G.since(**SRC)
check("no graph yet: --since writes one (0600), as --full would", (r["wrote"], r["was"], G.stored_hash(), oct(os.stat(GDB).st_mode & 0o777)),
      (True, None, fresh(), "0o600"))
before = stamp()
r = G.since(**SRC)
check("nothing changed: no write (same bytes, same mtime)", (r["changed"], r["wrote"], stamp() == before), (False, False, True))

print("── each same-count edit: --since writes, and equals a fresh --full ──")


def edit(label, fn):
    prev = G.stored_hash()
    fn()
    want = fresh()
    r = G.since(**SRC)
    check(f"{label}: written, the graph changed, and its hash is a fresh --full's",
          (r["wrote"], r["hash"] != prev, G.stored_hash() == want == r["hash"]), (True, True, True))


import kb_privacy  # noqa: E402
edit("a share flip in place (kb_acl: the wifi note household → private)",
     lambda: kb_privacy.set_acl("cx-note-wifi", "p_alex", "private", by="p_alex", why="test"))


def claim_value():
    con = sqlite3.connect(L.TMP / "world" / "p_alex.db")
    con.execute("UPDATE claims SET value='Pine Park Club head coach', value_norm='pine park club head coach' "
                "WHERE entity_id='morgan-reyes-coach' AND attr='role'")
    con.commit()
    con.close()


edit("a world claim's value changed in place (Morgan's role)", claim_value)


def place_rename():
    con = sqlite3.connect(os.environ["PLACES_DB"])
    con.execute("UPDATE places SET label='Pine Park North' WHERE id='pl-pine-park'")
    con.commit()
    con.close()


edit("a place renamed in place", place_rename)


def ledger_version():
    con = sqlite3.connect(os.environ["CONNECTORS_LEDGER"])
    it = next(i for i in L.H["items"] if i["id"] == "cx-calendar-practice")
    con.execute("INSERT INTO versions (source, source_id, item_id, change, title, fields, owner, audience, at) VALUES "
                "(?,?,?,?,?,?,?,?,?)", ("calendar", it["id"], it["id"], "changed", it["title"],
                                        json.dumps(dict(it["fields"], location="Cedar School")), it["owner"], it["audience"], 1))
    con.commit()
    con.close()


edit("a ledger version (Leo's practice now at Cedar School)", ledger_version)


def link_statement():
    con = sqlite3.connect(os.environ["LINKS_DB"])
    con.execute("INSERT INTO link_statements (a, b, related, by, at) VALUES ('cx-mail-bill-1','cx-mail-bill-2',0,'p_alex',99)")
    con.commit()
    con.close()


edit("a link statement (the Lakeside mails: not related)", link_statement)


def hook_replace():
    con = sqlite3.connect(os.environ["LINKS_DB"])
    con.execute("UPDATE hooks SET key='household:cedar-school' WHERE item_id='dev-text-plan-1' AND kind='mention:entity'")
    con.commit()
    con.close()


edit("a hook replaced in place (same count: the text now mentions Cedar School)", hook_replace)


def place_event():
    con = sqlite3.connect(os.environ["PLACES_DB"])
    con.execute("INSERT INTO place_events (id, person, event, place, at, received_at, device) VALUES "
                "('pe-leo-1','p_leo','arrived','Cedar School','2026-10-10T15:25:00-07:00',0,'dev-leo-watch')")
    con.commit()
    con.close()


edit("a place event (Leo arrived at school)", place_event)


def meeting_decision():
    import meeting_notes
    con = sqlite3.connect(os.environ["LINKS_DB"])
    md = L.H["meeting_decisions"]
    meeting_notes.put_decisions(con, md["owner"], md["sig_id"], md["decisions"][:2] + ["Jordan runs the sign-up"], md["kb_id"])
    con.commit()
    con.close()


edit("a meeting decision reworded (same count)", meeting_decision)

print("── a fence flip: nothing written, the view changes at once ──")
import fences  # noqa: E402
fs = fences.get()
health = next(r["id"] for r in fs.con.execute("SELECT id, subject FROM fences WHERE kind='class'") if r["subject"] == "health")
seen_before = "cx-note-health" in G.visible_nodes("p_jordan", "adult")
before = stamp()
fs.con.execute("UPDATE fences SET status='off' WHERE id=?", (health,))
fs.con.commit()
r = G.since(**SRC)
check("the health fence off: --since writes nothing (fences aren't in graph.db)", (r["changed"], r["wrote"], stamp() == before),
      (False, False, True))
check("…yet Jordan's view changes at once: the fenced note now shows (it was hidden)",
      (seen_before, "cx-note-health" in G.visible_nodes("p_jordan", "adult")), (False, True))
fs.con.execute("UPDATE fences SET status='active' WHERE id=?", (health,))
fs.con.commit()
check("…and back on: hidden again, still nothing written", ("cx-note-health" in G.visible_nodes("p_jordan", "adult"),
                                                             G.since(**SRC)["wrote"]), (False, False))

import bill_due  # noqa: E402
PINE = "obligation:pine water:2026-10-06"
was = (G.obligation_state("p_alex", "admin", PINE, **SRC) or {}).get("state")
before = stamp()
bcon = bill_due._db()
bcon.execute("INSERT INTO obligation_claims (id, owner, audience, account, kind, date, source, turn_id, at, superseded_by, asked, "
             "about) VALUES ('oc-alex-pine-1010','p_alex','private','pine water','paid','2026-10-10','chat',NULL,0,NULL,NULL,NULL)")
bcon.commit()
r = G.since(**SRC)
check("a person's word on a bill (Alex: Pine Water is paid): nothing written, the state is read time",
      (r["changed"], r["wrote"], stamp() == before), (False, False, True))
check("…yet obligation_state answers it at once", (was, (G.obligation_state("p_alex", "admin", PINE, **SRC) or {}).get("state")),
      ("open", "paid"))

print("── a dry run never writes ──")
place_rename_back = lambda: (sqlite3.connect(os.environ["PLACES_DB"]).execute(  # noqa: E731
    "UPDATE places SET label='Pine Park' WHERE id='pl-pine-park'").connection.commit())
place_rename_back()
before = stamp()
r = G.since(dry_run=True, **SRC)
check("a change pending, --since --dry-run: reports it, writes nothing", (r["changed"], r["wrote"], stamp() == before),
      (True, False, True))
check("…then the real pass writes it, a fresh --full's hash", (G.since(**SRC)["wrote"], G.stored_hash() == fresh()), (True, True))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
