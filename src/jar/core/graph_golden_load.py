#!/usr/bin/env python3
"""The golden household (graph_golden/household.json) written into the real stores, as temporary copies in their
own rows: the loader test_graph_sync (P1/P2) and test_situation (P3) share. Importing it sets the store paths to a
fresh temp dir (TMP) and loads every store; build() then projects graph.db there with graph_sync and returns its path.
Test use only: it points every store at the temp dir before anything opens one."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

# GRAPH_GOLDEN_DIR: a fixture box's stores dir (box_fixture_load.py); otherwise a fresh temp dir for the tests
_TMP = Path(os.environ.get("GRAPH_GOLDEN_DIR") or tempfile.mkdtemp(prefix="titan-graph-p1-"))
_TMP.mkdir(parents=True, exist_ok=True)
for k, v in {"HOUSEHOLD_DB": "household.db", "WORLD_DIR": "world", "PLACES_DB": "places.db",
             "CONNECTORS_LEDGER": "ledger.db", "KB_ACL_DB": "kb_acl.db", "LINKS_DB": "links.db",
             "LINK_SALT_FILE": "salt", "FENCES_DB": "fences.db", "TASKS_DB": "tasks.db", "GRAPH_DB": "graph.db",
             "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl", "MAIL_SERVER_DB": "mail_server.db",
             "MAIL_BILLS_DB": "mail_bills.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["AGENT_FENCES"] = "1"
os.environ.pop("GEMINI_API_KEY", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# GRAPH_GOLDEN_FIXTURE: a variant of the household (a test that adds a few rows of its own), else the golden one
H = json.loads(Path(os.environ.get("GRAPH_GOLDEN_FIXTURE") or HERE / "graph_golden" / "household.json").read_text())


# ── the loader: the fixture into the real stores (temporary copies), in their own rows ─────────────────────────────
(_TMP / "salt").write_text(H["salt"])
import household_identity as hh  # noqa: E402
store = hh.HouseholdStore(_TMP / "household.db")
hh.reset_store(store)
store._db.execute("DELETE FROM people WHERE id=?", (hh.ADMIN_PERSON_ID,))   # the store's own default admin: not this house
for p in H["people"]:
    store._db.execute("INSERT INTO people (id, name, role, created_at) VALUES (?,?,?,?)", (p["id"], p["name"], p["role"], 0))
store._db.commit()
for p in H["people"]:
    if p.get("parents"):
        store.update_person(p["id"], parents=p["parents"])
    # the people row's own columns: aliases and the places they named (a list of names, as /v1/me stores them)
    store._db.execute("UPDATE people SET aliases=?, places=? WHERE id=?",
                      (json.dumps(p.get("aliases") or []), json.dumps(sorted((p.get("places") or {}).values())), p["id"]))
    if p.get("emails"):   # their own addresses, keyed only, as member_addresses stores them (#1424)
        import hooks
        store.set_addr_keys(p["id"], [hooks.keyed("addr", e.lower()) for e in p["emails"]])
store._db.commit()

import world  # noqa: E402
(_TMP / "world").mkdir()
for bubble, w in H["world"].items():
    world.World(_TMP / "world" / f"{bubble}.db")
    con = sqlite3.connect(_TMP / "world" / f"{bubble}.db")
    for e in w["entities"]:
        con.execute("INSERT INTO entities (id, type, name, owner, audience, merged_into, created_at, person_id) "
                    "VALUES (?,?,?,?,?,?,?,?)", (e["id"], e["type"], e["name"], e.get("owner"), e.get("audience") or "private",
                                                 e.get("merged_into"), 0, e.get("person_id")))
    for a in w.get("aliases", []):
        con.execute("INSERT INTO aliases (entity_id, alias_norm, alias) VALUES (?,?,?)",
                    (a["entity_id"], a["alias"].lower(), a["alias"]))
    ents = {e["id"]: e for e in w["entities"]}
    for c in w.get("claims", []):
        if c.get("value") is None:
            continue
        cur = con.execute("INSERT INTO claims (entity_id, attr, value_norm, value, value_entity, created_at, updated_at) "
                          "VALUES (?,?,?,?,?,0,0)", (c["entity"], c["attr"], str(c["value"]).lower(), str(c["value"]),
                                                     c.get("value_entity")))
        # a claim stands on evidence, as world.World records it (#1461 review): the fixture's own source, with the
        # claim's audience when it gives one, else its entity's
        ent = ents.get(c["entity"], {})
        con.execute("INSERT INTO evidence (claim_id, source, kind, ref, origin, observed_at, prior, owner, audience, "
                    "added_at) VALUES (?, 'fixture', 'stated', ?, 'fixture', '2026-10-01', 0.9, ?, ?, 0)",
                    (cur.lastrowid, f"{bubble}:{c['entity']}:{c['attr']}", c.get("owner", ent.get("owner")),
                     c.get("audience") or ent.get("audience") or "private"))
    con.commit()
    con.close()

import places  # noqa: E402
pcon = places._db()
places.ensure_registry(pcon)
for pl in H["places"]:
    pcon.execute("INSERT INTO places (id, label, kind, owner, audience, aliases, org_entity, sightings) VALUES (?,?,?,?,?,?,?,?)",
                 (pl["id"], pl["label"], pl.get("kind") or "named", pl.get("owner"), pl.get("audience") or "household",
                  json.dumps(pl.get("aliases") or []), pl.get("org_entity"), int(pl.get("sightings") or 0)))
pcon.commit()

import connectors_runner as cx  # noqa: E402
import kb_privacy  # noqa: E402
import hooks  # noqa: E402
lcon = cx._db()
hcon = hooks.connect()
LEDGER_SOURCES = {"calendar", "mail", "meeting"}
for it in H["items"]:
    if it["source"] in LEDGER_SOURCES:
        lcon.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner, upstream_id, observed_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (it["source"], it["id"], "h", it["id"], 0, it["owner"], it["id"], it["observed_at"]))
        lcon.execute("INSERT INTO versions (source, source_id, item_id, change, title, fields, owner, audience, at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (it["source"], it["id"], it["id"], "created", it["title"],
                                                    json.dumps(it.get("fields") or {}), it["owner"], it["audience"], 0))
    elif it["source"] == "notes":
        kb_privacy.set_acl(it["id"], it["owner"], it["audience"], by=it["owner"], why="write")
    else:   # texts (a device's items) and chat turns: the link index's own rows, keyed hooks only
        src = "text" if it["source"] == "texts" else "conversation"
        hcon.execute("INSERT INTO hooks (item_id, source, kind, key, at, locator, owner, audience, made_at, version) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (it["id"], src, "date:item", it["observed_at"][:10], it["observed_at"][:10],
                                                       "{}", it["owner"], it["audience"], 0, 1))
lcon.commit()
ITEMS = {i["id"]: i for i in H["items"]}


def hook_key(kind, raw):
    """A hook's key as hooks.extract mints it (never the fixture's own shape): a domain and an entity id as they
    are, a day as its ISO date, a phone's last ten digits and an address or identifier under the house salt."""
    if kind == "party:domain":
        return hooks.registrable(raw)
    if kind in ("party:entity", "mention:entity", "party:vendor", "mention:place", "mention:person", "thread:device",
                "reply:device"):
        return raw                             # a registry place id, a household person id, a conversation's thread_ref
        #                                        or the replied-to item's id, as a device's hooks keep them
    if kind == "amount":
        return raw                             # dollars and cents, as they are
    if kind.startswith("date:"):
        return raw[:10]
    if kind == "party:phone":
        import re
        return hooks.keyed("phone", re.sub(r"\D", "", raw)[-10:])
    if kind in ("party:addr", "party:to"):
        return hooks.keyed("addr", raw.lower())
    if kind == "id:source":
        return hooks.keyed("id", raw)          # an item id as it is (hooks.index / extract's derived_from)
    return hooks.keyed("id", raw.upper().replace("-", ""))


for h in H["hooks"]:
    it = ITEMS.get(h["item"]) or ITEMS[h["item"].removeprefix("rec-")]   # a record: owned and shared as its mail
    part = "header" if h["kind"].startswith("party:") else "body"
    loc = {"part": part, "line": 0, **({"dir": h["dir"]} if h.get("dir") else {}), **({"time": h["time"]} if h.get("time") else {})}
    hcon.execute("INSERT OR IGNORE INTO hooks (item_id, source, kind, key, at, locator, owner, audience, made_at, version) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (h["item"], "record" if h["item"].startswith("rec-") else "fixture", h["kind"], hook_key(h["kind"], h["raw"]),
                                                   it["observed_at"][:10], json.dumps(loc),
                                                   it["owner"], it["audience"], 0, 1))
import linker  # noqa: E402
hcon.executescript(linker.SCHEMA)
for ln in H["links"]:
    hcon.execute("INSERT INTO links (a, b, state, typed, measured, why, made_at) VALUES (?,?,?,?,?,?,0)",
                 (*sorted((ln["a"], ln["b"])), ln["state"], ln["typed"], "{}", ln["why"]))
for i, st in enumerate(H["link_statements"]):
    hcon.execute("INSERT INTO link_statements (a, b, related, by, at) VALUES (?,?,?,?,?)",
                 (*sorted((st["a"], st["b"])), int(st["related"]), st["by"], float(i + 1)))
import event_proposals as ep  # noqa: E402
ep.ensure(hcon)
for p in H["event_proposals"]:
    hcon.execute("INSERT INTO event_proposals (owner, sig_id, thread_ref, title, start, end_at, place, participants, others, "
                 "with_name, confidence, made_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (p["owner"], p["sig_id"], p["thread_ref"], p["title"], p["start"], p.get("end"), p.get("place"),
                  json.dumps(p["participants"]), p.get("others") or 0, p.get("with_name"), p["confidence"], 0, p["state"]))
hcon.commit()

for pe in H["place_events"]:
    pcon.execute("INSERT INTO place_events (id, person, event, place, at, received_at, device) VALUES (?,?,?,?,?,0,?)",
                 (pe["id"], pe["person"], pe["event"], pe["place"], pe["at"], pe["device"]))
pcon.commit()

import mail_server  # noqa: E402
mcon = mail_server._db()
for n, it in enumerate(i for i in H["items"] if i["source"] == "mail"):
    env = {"interpretation": {"summary": it["summary"]}} if it.get("summary") else None
    mcon.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, in_reply_to, "
                 "thread_id, envelope) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("acct-alex", "INBOX", 1, n + 1, it["owner"], it["id"], f"<{it['id']}@fixture>", None, it["id"],
                  json.dumps(env) if env else None))
mcon.commit()

import meeting_notes  # noqa: E402
meeting_notes.ensure(hcon)
MD = H["meeting_decisions"]
hcon.execute("INSERT INTO meeting_summaries (owner, sig_id, ref_id, title, day, speakers, turns, proposals, made_at, state, "
             "kb_id, decisions) VALUES (?,?,?,?,?,6,40,0,0,'done',?,?)", (MD["owner"], MD["sig_id"], "rf-fixture-club",
                                                                        MD["title"], MD["day"], MD["kb_id"],
                                                                        len(MD["decisions"])))
meeting_notes.put_decisions(hcon, MD["owner"], MD["sig_id"], MD["decisions"], MD["kb_id"])
hcon.commit()
DECISION_IDS = [f"md-{MD['sig_id']}-{i}" for i in range(len(MD["decisions"]))]

import bill_due  # noqa: E402
import obligation_signals  # noqa: E402
bcon = bill_due._db()
for c in H["obligation_claims"]:
    bcon.execute("INSERT INTO obligation_claims (id, owner, audience, account, kind, date, source, turn_id, at, superseded_by, "
                 "asked, about) VALUES (?,?,?,?,?,?,?,?,0,?,?,?)", (c["id"], c["owner"], c["audience"], c["account"], c["kind"],
                                                                     c["date"], c["source"], c["turn_id"], c["superseded_by"],
                                                                     c["asked"], c["about"]))
bcon.commit()
obligation_signals.ensure(hcon)
for sg in H["obligation_signals"]:
    hcon.execute("INSERT INTO obligation_signals (owner, sig_id, signal, account, amount, on_day, confidence, made_at) "
                 "VALUES (?,?,?,?,?,?,?,0)", (sg["owner"], sg["sig_id"], sg["signal"], sg["account"], sg["amount"],
                                              sg["on_day"], sg["confidence"]))
hcon.commit()

import fences  # noqa: E402
fs = fences.get()
health = next(r["id"] for r in fs.con.execute("SELECT id, subject FROM fences WHERE kind='class'") if r["subject"] == "health")
for f in H["fences"]:
    it = next(i for i in H["items"] if i["id"] == f["item"])
    fs.carry(f["item"], [health], (it.get("fields") or {}).get("about_person"))   # the real health fence, as carried

import scenarios as sc  # noqa: E402
scon = sc._db()
for s in H["scenarios"]:
    scon.execute("INSERT INTO scenarios (task_id, owner, goal, state, deps, created_at) VALUES (?,?,?,?,?,?)",
                 (s["task_id"], s["owner"], s["goal"], s["state"], json.dumps(s["deps"]), 0))
scon.commit()


def bill_records():
    """The bill and receipt records the record runtime holds for the fixture's bill mail (mail_bills' shape)."""
    out = []
    for it in H["items"]:
        f = it.get("fields") or {}
        if it["kind"] != "mail" or "-bill-" not in it["id"] or not f.get("vendor"):
            continue   # what mail_bills files: the bill and receipt mail (the trade-in mail isn't a bill)
        paid = it["id"] == "cx-mail-bill-2"
        out.append({"node_id": f"rec-{it['id']}", "node_type": "receipt" if paid else "bill", "data": {
            "doc_type": "receipt" if paid else "bill", "vendor": f["vendor"], "amount": f["amount"],
            "due_date": f.get("due"), "mail_date": it["observed_at"][:10], "paid_date": it["observed_at"][:10] if paid else None,
            "payment_status": "paid" if paid else "due",
            "provenance": {"source": "mail", "source_id": it["id"], "owner": it["owner"], "audience": it["audience"]}}})
    return out




TMP = _TMP


def build() -> Path:
    """graph_sync over the loaded stores (as of the fixture's day) → the temp graph.db's path."""
    import graph_sync as G
    n, x, e, f = G.build(records=bill_records, today=date.fromisoformat(H["now"][:10]))
    G.write(n, x, edges=e, fused=f)
    return _TMP / "graph.db"
