#!/usr/bin/env python3
# gate: hermetic
"""Seeding the place registry (#1427): candidates per owner from calendar locations, place events and named places,
never pooled across people, their owner's (plus a kid's parents') only; a node at 3 sightings for its owner alone;
idempotent; a confirmed place never touched by a reseed and kept by a reset. POST /v1/places/confirm: the owner or a
kid's parent only (someone else's is a 404), no shared screen, the server decides owner and audience, a kid never
widens, a parent never widens a kid's place, "same place as" is the owner's own two. Fictional (Alex, Robin, Jordan,
Ava); temporary stores.
Run: python3 scripts/test_places_seed.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-places-seed-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PLACES_DB": "places.db",
             "CONNECTORS_LEDGER": "ledger.db", "WORLD_DIR": "world", "LINKS_DB": "links.db", "LINK_SALT_FILE": "salt",
             "KB_ACL_DB": "kb_acl.db", "FENCES_DB": "fences.db", "TASKS_DB": "tasks.db", "GRAPH_DB": "graph.db",
             "MAIL_SERVER_DB": "mail_server.db", "AGENT_TURN_LOG": "turns.jsonl", "SURFACES_DB": "surfaces.db",
             "PROACTIVE_DB": "proactive.db", "HOUSEHOLD_PREFS": "household.json", "SITTER_DB": "sitter.db"}.items():
    os.environ[k] = str(_TMP / v)
os.environ.pop("GEMINI_API_KEY", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


# ── the household, through the real pairing route (so the route test below uses real device tokens) ─────────────────
from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402

MASTER = "master-test-token-0123456789"
api._AGENT_TOKEN = MASTER
st = hh.HouseholdStore(_TMP / "household.db")
hh.reset_store(st)
c = TestClient(api.app)
M = {"X-Agent-Token": MASTER}


def paired(new: dict) -> tuple[str, dict]:
    s_ = c.post("/v1/pair/start", json={"new_person": new}, headers=M).json()
    r_ = c.post("/v1/pair/claim", json={"code": s_["code"], "device_name": new["name"] + "'s phone", "platform": "ios"}).json()
    tok = {"X-Agent-Token": r_["token"]}
    return c.get("/v1/me", headers=tok).json()["person"]["id"], tok


ROBIN, T_ROBIN = paired({"name": "Robin Quarry", "role": "adult"})
JORDAN, T_JORDAN = paired({"name": "Jordan Hale", "role": "adult"})
AVA, T_AVA = paired({"name": "Ava Quarry", "role": "kid"})
_, T_SCREEN = paired({"name": "Living room", "role": "household"})
st.update_person(AVA, parents=[ROBIN])
st._db.execute("UPDATE people SET places=? WHERE id=?", (json.dumps(["Cedar School"]), AVA))

# ── the sources: calendars (Robin's yoga ×3, Jordan's yoga ×1 at the same studio, an address), place events ─────────
import connectors_runner as cx  # noqa: E402
L = cx._db()
cal = [("ev-y1", ROBIN, "Willow Studio"), ("ev-y2", ROBIN, "willow  studio"), ("ev-y3", ROBIN, "Willow Studio"),
       ("ev-j1", JORDAN, "Willow Studio"), ("ev-p1", ROBIN, "12 Shore Rd")]
for sid, owner, loc in cal:
    L.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner, upstream_id, observed_at) "
              "VALUES ('calendar',?, 'h', ?, 0, ?, ?, '2026-10-01')", (sid, sid, owner, sid))
    L.execute("INSERT INTO versions (source, source_id, item_id, change, title, fields, owner, audience, at) VALUES "
              "('calendar', ?, ?, 'created', 'x', ?, ?, 'private', 0)", (sid, sid, json.dumps({"location": loc}), owner))
L.commit()
import places  # noqa: E402
P = places._db()
for i in range(3):
    P.execute("INSERT INTO place_events (id, person, event, place, at, received_at) VALUES (?,?,?,?,?,0)",
              (f"pe-a{i}", AVA, "arrived", "Cedar School", f"2026-10-0{i + 1}T08:00:00"))
P.execute("INSERT INTO place_events (id, person, event, place, at, received_at, region) VALUES "
          "('pe-r', ?, 'arrived', 'Somewhere', '2026-10-05T08:00:00', 0, 'rg_sa-1')", (AVA,))
P.commit()
import world  # noqa: E402
(_TMP / "world").mkdir(exist_ok=True)
world.World(_TMP / "world" / "household.db")
w = sqlite3.connect(_TMP / "world" / "household.db")
w.execute("INSERT INTO entities (id, type, name, owner, audience, created_at) VALUES "
          "('lakeside-clinic','org','Lakeside Clinic',NULL,'household',0)")
w.execute("INSERT INTO claims (entity_id, attr, value_norm, value, created_at, updated_at) VALUES "
          "('lakeside-clinic','address','12 shore rd','12 Shore Rd',0,0)")
w.commit()
w.close()

import places_seed as S  # noqa: E402

print("── the plan: per owner, never pooled ──")
p = S.plan()
cands = {(c_["owner"], c_["label"]): c_ for c_ in p["candidates"].values()}
check("candidates: Robin's studio (3), Jordan's studio (1), Robin's address, Ava's school (3 events + her named place)",
      sorted((o == ROBIN and "robin" or o == JORDAN and "jordan" or "ava", lb, c_["sightings"])
             for (o, lb), c_ in cands.items()),
      sorted([("robin", "Willow Studio", 3), ("jordan", "Willow Studio", 1), ("robin", "12 Shore Rd", 1),
              ("ava", "Cedar School", 4)]))
check("the label as most often written; a scheduled region's arrival never counts",
      ("willow  studio" not in [lb for _, lb in cands], any(lb == "Somewhere" for _, lb in cands)), (True, False))
check("audiences: the owner alone; a kid's adds her parents; never the household",
      (cands[(ROBIN, "Willow Studio")]["audience"], cands[(AVA, "Cedar School")]["audience"]),
      (json.dumps([ROBIN]), json.dumps(sorted([AVA, ROBIN]))))
check("an org link only on an exact address match", cands[(ROBIN, "12 Shore Rd")]["org_entity"], "household:lakeside-clinic")
check("2 reach 3 sightings (nodes for their owners)", p["nodes"], 2)

print("── apply, idempotent ──")
S.apply(p)
again = S.plan()
check("a rerun changes nothing", (again["add"], again["update"], again["drop"]), ([], [], []))
rows = {r[0]: r for r in sqlite3.connect(_TMP / "places.db").execute("SELECT id, kind, owner, confirmed_by FROM places")}
check("rows are candidates, unconfirmed", {(r[1], r[3]) for r in rows.values()}, {("candidate", None)})

print("── the graph: a node at 3 sightings, for its owner only ──")
import graph_sync as G  # noqa: E402
nodes, xw, edges, fused = G.build(records=lambda: [])
G.write(nodes, xw, edges=edges, fused=fused)
willow = S.cand_id(ROBIN, "Willow Studio")
check("Robin's studio is a place node for Robin; not for Jordan (his own is seen once: no node)",
      (willow in G.visible_nodes(ROBIN, "adult"), willow in G.visible_nodes(JORDAN, "adult"),
       S.cand_id(JORDAN, "Willow Studio") in {n["id"] for n in nodes}), (True, False, False))
school = S.cand_id(AVA, "Cedar School")
check("Ava's school: Ava and her parent Robin see it, Jordan and a screen don't",
      [school in G.visible_nodes(v, r) for v, r in ((AVA, "kid"), (ROBIN, "adult"), (JORDAN, "adult"), (None, "household"))],
      [True, True, False, False])

print("── POST /v1/places/confirm ──")
r = c.post("/v1/places/confirm", json={"id": willow, "label": "Willow Yoga", "kind": "venue"}, headers=T_ROBIN)
check("the owner confirms: renamed, kind set, the old label kept as an alias, still hers alone",
      (r.status_code, r.json().get("label"), r.json().get("kind"), r.json().get("aliases"), r.json().get("shared")),
      (200, "Willow Yoga", "venue", ["Willow Studio"], False))
check("someone else's place is a 404 (not theirs to know about)",
      c.post("/v1/places/confirm", json={"id": willow, "label": "x"}, headers=T_JORDAN).status_code, 404)
check("a shared screen can never call it", c.post("/v1/places/confirm", json={"id": willow}, headers=T_SCREEN).status_code, 403)
check("no body field names an owner or audience",
      c.post("/v1/places/confirm", json={"id": willow, "audience": "household"}, headers=T_ROBIN).status_code, 400)
r = c.post("/v1/places/confirm", json={"id": school, "kind": "school"}, headers=T_AVA)
check("a kid confirms her own place", (r.status_code, r.json().get("kind")), (200, "school"))
check("…but can't widen it", c.post("/v1/places/confirm", json={"id": school, "share": True}, headers=T_AVA).status_code, 403)
check("her parent may confirm it", c.post("/v1/places/confirm", json={"id": school, "label": "Cedar Elementary"},
                                          headers=T_ROBIN).status_code, 200)
check("…but never widens a kid's place", c.post("/v1/places/confirm", json={"id": school, "share": True},
                                                headers=T_ROBIN).status_code, 403)
r = c.post("/v1/places/confirm", json={"id": willow, "share": True}, headers=T_ROBIN)
check("an adult owner shares her own place, explicitly", (r.status_code, r.json().get("shared")), (200, True))
shore = S.cand_id(ROBIN, "12 Shore Rd")
check("same place as: not across owners (the parent can't merge her kid's school into her own place)",
      c.post("/v1/places/confirm", json={"id": school, "same_as": shore}, headers=T_ROBIN).status_code, 403)
r = c.post("/v1/places/confirm", json={"id": shore, "same_as": willow}, headers=T_ROBIN)
check("same place as: the owner's own two become one (aliases and sightings carried over)",
      (r.status_code, r.json().get("id"), "12 Shore Rd" in r.json().get("aliases", []), r.json().get("sightings")),
      (200, willow, True, 4))

print("── a reseed never touches a confirmed place; a reset keeps it ──")
p2 = S.plan()
S.apply(p2)
row = sqlite3.connect(_TMP / "places.db").execute("SELECT label, audience, confirmed_by FROM places WHERE id=?", (willow,)).fetchone()
check("the confirmed place is as its owner left it", row, ("Willow Yoga", "household", ROBIN))
check("its labels don't come back as new candidates (confirmed rows count first)",
      sorted(c_["label"] for c_ in p2["candidates"].values() if c_["owner"] == ROBIN), [])
import household_reset as R  # noqa: E402
R.STORES = _TMP
S.apply(S.plan())   # a fresh unconfirmed candidate beside the confirmed ones
con = sqlite3.connect(_TMP / "places.db")
n_conf, n_cand = (con.execute("SELECT count(*) FROM places WHERE confirmed_by IS NOT NULL").fetchone()[0],
                  con.execute("SELECT count(*) FROM places WHERE confirmed_by IS NULL").fetchone()[0])
con.close()
acts = {(a["table"], a["action"]): a for a in R.store_actions(False) if a["store"] == "places.db"}
check("household_reset's plan: the places table wipes exactly the unconfirmed rows, keeps the confirmed; place events kept",
      (acts.get(("places", "wipe"), {}).get("rows"), acts.get(("places", "wipe"), {}).get("where"),
       acts.get(("places", "wipe"), {}).get("note"), ("place_events", "keep") in acts),
      (n_cand, "confirmed_by IS NULL", f"{n_conf} people's rows kept", True))

print("── routes ──")
check("the route's roles: people, never a shared screen",
      [hh.route_allowed(x, "POST", "/v1/places/confirm") for x in ("admin", "adult", "kid", "household")],
      [True, True, True, False])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
