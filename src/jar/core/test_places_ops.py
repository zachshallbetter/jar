#!/usr/bin/env python3
# gate: hermetic
"""Settings › Household › Places (#1510): add, edit and remove as household-facts@1 operations over the one place
registry. The surface lists the operations with contracts, fields, exposure and recovery; a kid sees them unavailable
and cannot change anything; one adult's private place is invisible to another; a person-given name is never overwritten
by a learned one (a seed rerun); remove archives (never deletes) and undo puts the row back exactly; every change
leaves an undo token. Fictional (Robin, Jordan, Ava); temporary stores; no model.
Run: python3 scripts/test_places_ops.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-places-ops-"))
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


from fastapi.testclient import TestClient  # noqa: E402
import agent_api_server as api  # noqa: E402
import household_identity as hh  # noqa: E402
import places_seed as PS  # noqa: E402
import surfaces as S  # noqa: E402
import graph_sync as G  # noqa: E402

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

SURF = "/v1/surfaces/household-facts"


def surface(h):
    return c.get(SURF, headers=h).json()


def places_group(h):
    return next((g for g in surface(h)["groups"] if g["id"] == "g-places"), None)


def names(h):
    g = places_group(h)
    return sorted(m["slots"]["title"] for m in g["members"]) if g else None


def ops(h):
    return {o["id"]: o for o in surface(h)["operations"] if o["contract"].startswith("jellybean.place.")}


def db():
    con = sqlite3.connect(_TMP / "places.db")
    con.row_factory = sqlite3.Row
    return con


print("── the surface lists the operations, with contracts ──")
o = ops(T_ROBIN)
check("an empty registry still offers add, on the Places group", sorted(o), ["op-place-add"])
add_op = o["op-place-add"]
check("add: contract, route, exposure, undo, subject",
      (add_op["contract"], add_op["verb"], add_op["route"], add_op["exposure"], add_op["recovery"], add_op["subject"]),
      ("jellybean.place.add@1", "add", {"method": "POST", "path": "/v1/places"}, "available", ["undo"],
       {"kind": "group", "id": "g-places"}))
check("add declares its fields (name text, kind and visibility choices pre-filled)",
      [(f["key"], f["kind"], f["required"], f.get("value")) for f in add_op["fields"]],
      [("name", "text", True, None), ("kind", "choice", True, "named"), ("visibility", "choice", True, "private")])
check("kind offers the registry's kinds", [x["value"] for x in add_op["fields"][1]["choices"]], list(PS.KINDS))

print("── add ──")
r = c.post("/v1/places", json={"name": "Willow Studio", "kind": "venue"}, headers=T_ROBIN)
check("an adult adds a place: 201, theirs alone, an undo token",
      (r.status_code, r.json()["place"]["label"], r.json()["place"]["kind"], r.json()["place"]["shared"],
       r.json()["place"]["confirmed"], bool(r.json()["token"])), (201, "Willow Studio", "venue", False, True, True))
willow = r.json()["place"]["id"]
row = db().execute("SELECT owner, audience, confirmed_by FROM places WHERE id=?", (willow,)).fetchone()
check("the server decided owner and audience", tuple(row), (ROBIN, json.dumps([ROBIN]), ROBIN))
check("a second place by that name (any case or spacing) is a 409",
      c.post("/v1/places", json={"name": "willow  STUDIO"}, headers=T_ROBIN).status_code, 409)
check("bad bodies: no name, an unknown kind, an owner field, an unknown visibility",
      [c.post("/v1/places", json=b, headers=T_ROBIN).status_code for b in
       ({}, {"name": "X", "kind": "castle"}, {"name": "X", "owner": JORDAN}, {"name": "X", "visibility": "everyone"})],
      [400, 400, 400, 400])
o = ops(T_ROBIN)
check("the new place gets edit and remove, both undoable; remove is destructive",
      (sorted(o), o[f"op-place-remove-{willow}"]["variant"], o[f"op-place-remove-{willow}"]["recovery"],
       o[f"op-place-remove-{willow}"].get("fields"), o[f"op-place-edit-{willow}"]["route"]),
      (sorted(["op-place-add", f"op-place-edit-{willow}", f"op-place-remove-{willow}"]), "destructive", ["undo"], None,
       {"method": "PATCH", "path": f"/v1/places/{willow}"}))
check("edit pre-fills its fields from the place",
      [(f["key"], f.get("value")) for f in o[f"op-place-edit-{willow}"]["fields"]],
      [("name", "Willow Studio"), ("kind", "venue"), ("visibility", "private")])

print("── per-person: one adult's place is invisible to another ──")
check("Robin sees it; Jordan sees no Willow Studio, nor in any operation or route",
      (names(T_ROBIN), names(T_JORDAN), willow in json.dumps(surface(T_JORDAN)), "Willow" in json.dumps(surface(T_JORDAN))),
      (["Willow Studio"], [], False, False))
check("Jordan can't edit or remove it (404, as if unknown)",
      [c.patch(f"/v1/places/{willow}", json={"name": "Mine now"}, headers=T_JORDAN).status_code,
       c.delete(f"/v1/places/{willow}", headers=T_JORDAN).status_code], [404, 404])
check("Jordan may add his own place of the same name", c.post("/v1/places", json={"name": "Willow Studio"},
                                                           headers=T_JORDAN).status_code, 201)
check("…and the two stay apart", (names(T_ROBIN), names(T_JORDAN)), (["Willow Studio"], ["Willow Studio"]))
check("the graph holds Robin's place as a node owned by Robin, with her audience",
      [(n["owner"], n["audience"]) for n in G.place_nodes()[0] if n["id"] == willow], [(ROBIN, json.dumps([ROBIN]))])

print("── edit, and the person-given name rule ──")
r = c.patch(f"/v1/places/{willow}", json={"name": "Willow Yoga", "kind": "venue"}, headers=T_ROBIN)
check("rename: 200, old name kept as an alias, a token",
      (r.status_code, r.json()["place"]["label"], r.json()["place"]["aliases"], bool(r.json()["token"])),
      (200, "Willow Yoga", ["Willow Studio"], True))
tok_edit = r.json()["token"]
check("a name another of their places goes by is a 409",
      (c.post("/v1/places", json={"name": "Cedar"}, headers=T_ROBIN).status_code,
       c.patch(f"/v1/places/{willow}", json={"name": "cedar"}, headers=T_ROBIN).status_code), (201, 409))
# a learned label for the same place, seen three times: a seed run must not touch their name
import connectors_runner as cx  # noqa: E402
L = cx._db()
for i in range(3):
    L.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner, upstream_id, observed_at) "
              "VALUES ('calendar',?, 'h', ?, 0, ?, ?, '2026-10-01')", (f"ev-{i}", f"ev-{i}", ROBIN, f"ev-{i}"))
    L.execute("INSERT INTO versions (source, source_id, item_id, change, title, fields, owner, audience, at) VALUES "
              "('calendar', ?, ?, 'created', 'x', ?, ?, 'private', 0)", (f"ev-{i}", f"ev-{i}", json.dumps({"location": "Willow Studio"}), ROBIN))
    L.execute("INSERT INTO items (source, source_id, content_hash, item_id, synced_at, owner, upstream_id, observed_at) "
              "VALUES ('calendar',?, 'h', ?, 0, ?, ?, '2026-10-01')", (f"ex-{i}", f"ex-{i}", ROBIN, f"ex-{i}"))
    L.execute("INSERT INTO versions (source, source_id, item_id, change, title, fields, owner, audience, at) VALUES "
              "('calendar', ?, ?, 'created', 'x', ?, ?, 'private', 0)", (f"ex-{i}", f"ex-{i}", json.dumps({"location": "Harbor Pool"}), ROBIN))
L.commit()
p = PS.plan()
labels = sorted(x["label"] for x in p["candidates"].values() if x["owner"] == ROBIN)
check("a learned label for a place they named (its old name is an alias) isn't a new candidate; an unknown one is",
      labels, ["Harbor Pool"])
PS.apply(p)
row = db().execute("SELECT label, confirmed_by FROM places WHERE id=?", (willow,)).fetchone()
check("their name stands after a seed run", tuple(row), ("Willow Yoga", ROBIN))
check("Harbor Pool earned 3 sightings: it lists as a learned place with operations",
      [m["slots"]["meta"] for m in places_group(T_ROBIN)["members"] if m["slots"]["title"] == "Harbor Pool"],
      ["Private, learned"])
pool = next(x for x in PS.listing(ROBIN, "adult") if x["label"] == "Harbor Pool")["id"]
r = c.patch(f"/v1/places/{pool}", json={"name": "Harbor Pool & Spa"}, headers=T_ROBIN)
check("naming a learned place makes the name theirs", (r.status_code, r.json()["place"]["confirmed"]), (200, True))
PS.apply(PS.plan())
check("…and a rerun never gives the learned label back",
      db().execute("SELECT label FROM places WHERE id=?", (pool,)).fetchone()[0], "Harbor Pool & Spa")
check("share: an adult owner widens explicitly; the household then sees it, and can't edit it",
      (c.patch(f"/v1/places/{willow}", json={"visibility": "household"}, headers=T_ROBIN).json()["place"]["shared"],
       names(T_JORDAN), c.patch(f"/v1/places/{willow}", json={"name": "x"}, headers=T_JORDAN).status_code,
       [k for k in ops(T_JORDAN) if willow in k]), (True, ["Willow Studio", "Willow Yoga"], 404, []))
check("…and takes it back to just them", c.patch(f"/v1/places/{willow}", json={"visibility": "private"},
                                                  headers=T_ROBIN).json()["place"]["shared"], False)
check("undo of the rename puts the old name back",
      (c.post("/v1/surfaces/undo", json={"token": tok_edit}, headers=T_ROBIN).status_code,
       db().execute("SELECT label FROM places WHERE id=?", (willow,)).fetchone()[0]), (200, "Willow Studio"))

print("── remove is an archive; undo puts it back ──")
r = c.delete(f"/v1/places/{willow}", headers=T_ROBIN)
check("remove: 200 with a token", (r.status_code, r.json()["place"]["id"], bool(r.json()["token"])), (200, willow, True))
tok_rm = r.json()["token"]
row = db().execute("SELECT label, archived_at FROM places WHERE id=?", (willow,)).fetchone()
check("the row is still there, archived (never a hard delete)", (row["label"], bool(row["archived_at"])), ("Willow Studio", True))
check("gone from the surface, its operations, and the graph and hooks readers",
      (names(T_ROBIN), [k for k in ops(T_ROBIN) if willow in k], willow in {n['id'] for n in G.place_nodes()[0]},
       willow in {r_["id"] for r_ in G.registry_rows()} or willow in __import__("hooks")._places_for(ROBIN, "adult")), (["Cedar", "Harbor Pool & Spa"], [], False, False))
check("removing it again, or editing it, is a 404",
      [c.delete(f"/v1/places/{willow}", headers=T_ROBIN).status_code,
       c.patch(f"/v1/places/{willow}", json={"name": "z"}, headers=T_ROBIN).status_code], [404, 404])
PS.apply(PS.plan())
check("a reseed does not relearn a removed place (nor drop the archived row)",
      ("Willow Studio" in [x["label"] for x in PS.plan()["candidates"].values()],
       bool(db().execute("SELECT archived_at FROM places WHERE id=?", (willow,)).fetchone()[0])), (False, True))
check("Jordan can't use Robin's token", c.post("/v1/surfaces/undo", json={"token": tok_rm}, headers=T_JORDAN).status_code, 404)
check("undo restores it exactly", (c.post("/v1/surfaces/undo", json={"token": tok_rm}, headers=T_ROBIN).status_code,
                                    names(T_ROBIN)), (200, ["Cedar", "Harbor Pool & Spa", "Willow Studio"]))
check("…and a token works once", c.post("/v1/surfaces/undo", json={"token": tok_rm}, headers=T_ROBIN).status_code, 404)
r = c.post("/v1/places", json={"name": "Orchard"}, headers=T_ROBIN)
check("undo of an add archives the new place",
      (c.post("/v1/surfaces/undo", json={"token": r.json()["token"]}, headers=T_ROBIN).status_code,
       "Orchard" in (names(T_ROBIN) or []),
       bool(db().execute("SELECT archived_at FROM places WHERE id=?", (r.json()["place"]["id"],)).fetchone()[0])),
      (200, False, True))
check("adding a removed place's name brings the same row back",
      (lambda rr: (rr.status_code, rr.json()["place"]["id"]))(
          (c.delete(f"/v1/places/{willow}", headers=T_ROBIN), c.post("/v1/places", json={"name": "willow studio"}, headers=T_ROBIN))[1]),
      (201, willow))

print("── a kid only proposes ──")
ko = ops(T_AVA)
check("a kid's surface offers add, with the operation unavailable and the reason",
      (sorted(ko), ko["op-place-add"]["exposure"], ko["op-place-add"]["reason"]),
      (["op-place-add"], "unavailable", {"key": "kids.propose_only"}))
k = c.post("/v1/places", json={"name": "Treehouse"}, headers=T_AVA)
check("…and the route refuses her (403), so does the code", (k.status_code, names(T_AVA)), (403, []))
con = db()
con.execute("INSERT INTO places (id, label, kind, owner, audience, aliases, sightings, confirmed_by, updated_at) VALUES "
            "('pl-ava1','Cedar School','school',?,?, '[]', 0, ?, 0)", (AVA, json.dumps(sorted([AVA, ROBIN])), AVA))
con.commit()
con.close()
ko = ops(T_AVA)
check("Ava sees her own place with edit and remove unavailable; she can't change them",
      (names(T_AVA), {o_["exposure"] for o_ in ko.values()}, c.patch("/v1/places/pl-ava1", json={"name": "Z"}, headers=T_AVA).status_code,
       c.delete("/v1/places/pl-ava1", headers=T_AVA).status_code), (["Cedar School"], {"unavailable"}, 403, 403))
po = ops(T_ROBIN)
check("her parent sees it and may change it (available); Jordan doesn't see it at all",
      (po["op-place-edit-pl-ava1"]["exposure"], names(T_JORDAN) is not None and "Cedar School" in names(T_JORDAN)), ("available", False))
check("the parent's edit form has no 'who sees it' (a parent never widens a kid's place)",
      [f["key"] for f in po["op-place-edit-pl-ava1"]["fields"]], ["name", "kind"])
check("…and sending it is refused", c.patch("/v1/places/pl-ava1", json={"visibility": "household"}, headers=T_ROBIN).status_code, 403)
check("the parent renames it", c.patch("/v1/places/pl-ava1", json={"name": "Cedar Elementary"}, headers=T_ROBIN).status_code, 200)

print("── a shared screen, the route table, and the confirm route still works ──")
check("a screen gets 403 on the surface and on every place route",
      [c.get(SURF, headers=T_SCREEN).status_code, c.post("/v1/places", json={"name": "x"}, headers=T_SCREEN).status_code,
       c.patch("/v1/places/x", json={}, headers=T_SCREEN).status_code, c.delete("/v1/places/x", headers=T_SCREEN).status_code],
      [403, 403, 403, 403])
check("route roles: adults only",
      [[hh.route_allowed(x, m_, p_) for x in ("admin", "adult", "kid", "household")]
       for m_, p_ in (("POST", "/v1/places"), ("PATCH", "/v1/places/pl-1"), ("DELETE", "/v1/places/pl-1"))],
      [[True, True, False, False]] * 3)
check("confirm of an archived place is a 404",
      (c.delete(f"/v1/places/{pool}", headers=T_ROBIN).status_code, c.post("/v1/places/confirm", json={"id": pool, "kind": "shop"},
                                                                          headers=T_ROBIN).status_code), (200, 404))
import world as W  # noqa: E402
check("the resolver without a places argument is unchanged (no Places group)",
      "g-places" in {g["id"] for g in S.household_facts(W.get(), ROBIN, "adult", 0.0)["groups"]}, False)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
