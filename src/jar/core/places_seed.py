#!/usr/bin/env python3
"""Seeding the place registry (#1427; relational FE graph §7.2, #1399): candidate places from what titan already holds,
promoted only on evidence or on their owner's word.

  sources      a calendar item's location (its latest version), a person's own place events (arrived/left, no
               scheduled regions), and the places a person named (people.places); each counted per OWNER
  candidate    one row per (owner, normalised label): id pl-c-<hash>, kind "candidate", the label as most often
               written, sightings = distinct source rows. Audience: the owner alone, plus their parents for a kid
               (places.viewable's rule): never the household. Two people's "Willow Studio" are two candidates.
  node         graph_sync makes a candidate a node, for its owner only, once seen PLACE_SIGHTINGS (3) times
  confirmed    only through confirm() (POST /v1/places/confirm), on the owner's word (or a kid's parent): a label, a
               kind, "same place as …" (the owner's own two candidates), and sharing with the household only on an
               explicit share from an adult owner (#1397: kids never widen). confirmed_by is a person's decision and
               survives a reset; candidates are derivable and a reset may drop them (household_reset).
  org link     org_entity only when the location exactly equals a world org's address claim the owner can see

Never coordinates, never a raw address beyond the location text the calendar already holds. A rerun is idempotent:
it recounts, adds and removes candidates and never touches a confirmed row.

  python3 scripts/places_seed.py            dry run: counts only (candidates, by source, reaching 3 sightings)
  python3 scripts/places_seed.py --apply    write the candidates (on Coordination's go)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
PLACE_SIGHTINGS = 3
KINDS = ("named", "home", "school", "work", "park", "venue", "clinic", "shop", "other")


def norm(s: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def cand_id(owner: str, label: str) -> str:
    return "pl-c-" + hashlib.sha1(f"{owner}|{norm(label)}".encode()).hexdigest()[:12]


def _ro(path: Any) -> sqlite3.Connection | None:
    p = Path(path) if path else None
    if not p or not p.exists():
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _has(con: sqlite3.Connection | None, table: str) -> bool:
    return bool(con) and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def people(path: Any = None) -> list[dict[str, Any]]:
    con = _ro(path or os.environ.get("HOUSEHOLD_DB") or "/ai/datasets/connectors/household.db")
    out: list[dict[str, Any]] = []
    if not _has(con, "people"):
        return out
    for r in con.execute("SELECT * FROM people WHERE role != 'household' ORDER BY id"):
        row = {"id": str(r["id"]), "role": r["role"]}
        for k in ("parents", "places"):
            try:
                row[k] = json.loads(r[k] or "[]") if k in r.keys() and r[k] else []
            except ValueError:
                row[k] = []
        out.append(row)
    con.close()
    return out


def audience_of(owner: str, folks: list[dict[str, Any]]) -> str:
    """The owner alone, plus their parents for a kid: a person's places are theirs (places.viewable)."""
    import audience as A
    p = next((x for x in folks if x["id"] == owner), {})
    return A.normalize([owner] + (list(p.get("parents") or []) if p.get("role") == "kid" else []), default="private")


def sightings(folks: list[dict[str, Any]], *, ledger: Any = None, places_db: Any = None) -> dict[tuple[str, str], Counter]:
    """{(owner, normalised label): Counter(label as written → distinct source rows)}, read-only."""
    import connectors_runner as cx
    import places
    members = {p["id"] for p in folks}
    out: dict[tuple[str, str], Counter] = {}

    def add(owner: Any, label: Any) -> None:
        if owner in members and norm(label):
            out.setdefault((owner, norm(label)), Counter())[" ".join(str(label).split())] += 1

    con = _ro(ledger or os.environ.get("CONNECTORS_LEDGER") or cx.LEDGER_PATH)
    if _has(con, "items"):
        for iid, owner in con.execute("SELECT item_id, owner FROM items WHERE source='calendar' AND deleted_at IS NULL "
                                      "ORDER BY item_id").fetchall():
            v = con.execute("SELECT fields FROM versions WHERE item_id=? ORDER BY at DESC LIMIT 1", (iid,)).fetchone()
            try:
                f = json.loads(v[0] or "{}") if v else {}
            except ValueError:
                continue
            if isinstance(f, dict) and f.get("location"):
                add(owner, f["location"])
    if con:
        con.close()
    con = _ro(places_db or os.environ.get("PLACES_DB") or places.DB_PATH)
    if _has(con, "place_events"):
        cols = {r[1] for r in con.execute("PRAGMA table_info(place_events)")}
        region = " WHERE region IS NULL" if "region" in cols else ""
        for person, place in con.execute(f"SELECT person, place FROM place_events{region} ORDER BY id"):
            add(person, place)
    if con:
        con.close()
    for p in folks:
        for name in p.get("places") or []:
            add(p["id"], name)
    return out


def _registry(places_db: Any = None) -> list[dict[str, Any]]:
    import places
    con = _ro(places_db or os.environ.get("PLACES_DB") or places.DB_PATH)
    if not _has(con, "places"):
        return []
    rows = [dict(r) for r in con.execute("SELECT * FROM places ORDER BY id")]
    con.close()
    for r in rows:
        try:
            r["aliases"] = json.loads(r.get("aliases") or "[]")
        except ValueError:
            r["aliases"] = []
    return rows


def _org_addresses(root: Any = None) -> dict[str, str]:
    """{normalised address: "bubble:org"} from world org address claims, read-only (a link needs an exact match)."""
    import world
    out: dict[str, str] = {}
    for f in sorted(Path(root or os.environ.get("WORLD_DIR") or world.WORLD_DIR).glob("*.db")):
        con = _ro(f)
        if con is None:
            continue
        try:
            for eid, val in con.execute("SELECT c.entity_id, c.value FROM claims c JOIN entities e ON e.id=c.entity_id "
                                        "WHERE e.type='org' AND c.attr='address' AND c.status='current'"):
                if norm(val):
                    out.setdefault(norm(val), f"{f.stem}:{eid}")
        except sqlite3.Error:
            pass
        con.close()
    return out


def plan(*, ledger: Any = None, places_db: Any = None, household: Any = None, world_root: Any = None) -> dict[str, Any]:
    """The candidates a seed would hold now, and what it would change. A label a confirmed place the owner can see
    already carries (its label or an alias) is that place, not a new candidate."""
    import audience as A
    folks = people(household)
    seen = sightings(folks, ledger=ledger, places_db=places_db)
    reg = _registry(places_db)
    orgs = _org_addresses(world_root)
    # a person's word stands, and so does their removal (#1510): a place they named or archived is never relearned
    confirmed = [r for r in reg if r.get("confirmed_by") or r.get("archived_at")]
    cands: dict[str, dict[str, Any]] = {}
    for (owner, key), spellings in sorted(seen.items()):
        if any(A.visible(r["audience"], owner, None, owner=r.get("owner"))
               and key in {norm(r["label"]), *(norm(a) for a in r["aliases"])} for r in confirmed):
            continue
        label = sorted(spellings.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        cid = cand_id(owner, key)
        cands[cid] = {"id": cid, "label": label, "kind": "candidate", "owner": owner,
                      "audience": audience_of(owner, folks), "aliases": [], "org_entity": orgs.get(key),
                      "sightings": sum(spellings.values())}
    old = {r["id"]: r for r in reg if not r.get("confirmed_by") and not r.get("archived_at")
           and r.get("kind") == "candidate"}
    return {"candidates": cands, "add": sorted(set(cands) - set(old)), "drop": sorted(set(old) - set(cands)),
            "update": sorted(c for c in set(cands) & set(old) if (cands[c]["label"], cands[c]["sightings"],
                                                                  cands[c]["audience"], cands[c]["org_entity"])
                             != (old[c]["label"], old[c]["sightings"], old[c]["audience"], old[c]["org_entity"])),
            "nodes": sum(1 for c in cands.values() if c["sightings"] >= PLACE_SIGHTINGS)}


def apply(p: dict[str, Any], places_db: Any = None) -> int:
    """Write the plan: candidate rows only (confirmed rows are never touched). → rows written or removed."""
    import places
    con = sqlite3.connect(str(places_db or os.environ.get("PLACES_DB") or places.DB_PATH))
    try:
        places.ensure_registry(con)
        now = time.time()
        for cid in p["add"] + p["update"]:
            c = p["candidates"][cid]
            con.execute("INSERT INTO places (id, label, kind, owner, audience, aliases, org_entity, sightings, "
                        "confirmed_by, updated_at) VALUES (?,?,?,?,?,?,?,?,NULL,?) ON CONFLICT(id) DO UPDATE SET "
                        "label=excluded.label, audience=excluded.audience, org_entity=excluded.org_entity, "
                        "sightings=excluded.sightings, updated_at=excluded.updated_at WHERE places.confirmed_by IS NULL "
                        "AND places.archived_at IS NULL",
                        (cid, c["label"], "candidate", c["owner"], c["audience"], "[]", c["org_entity"], c["sightings"], now))
        for cid in p["drop"]:
            con.execute("DELETE FROM places WHERE id=? AND confirmed_by IS NULL AND archived_at IS NULL AND kind='candidate'", (cid,))
        con.commit()
    finally:
        con.close()
    return len(p["add"]) + len(p["update"]) + len(p["drop"])


# ── a person's word: POST /v1/places/confirm ─────────────────────────────────────────────────────────────────────────

class ConfirmError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _may_confirm(viewer: str, row: dict[str, Any], folks: list[dict[str, Any]]) -> bool:
    """The place's owner, or a parent of the kid who owns it."""
    owner = row.get("owner")
    if not owner or not viewer:
        return False
    if owner == viewer:
        return True
    p = next((x for x in folks if x["id"] == owner), {})
    return p.get("role") == "kid" and viewer in (p.get("parents") or [])


def confirm(viewer: str | None, role: str | None, body: dict[str, Any], *, places_db: Any = None,
            household: Any = None) -> dict[str, Any]:
    """{id, label?, kind?, share?: true, same_as?: <another of the owner's places>} → the place as its owner sees it.
    The server decides owner and audience: no body field names either. Someone else's place is a 404 (it isn't theirs
    to know about); a shared screen never gets here (ROUTE_ROLES)."""
    import places
    if not viewer or role not in ("admin", "adult", "kid"):
        raise ConfirmError(403, "a person confirms their own places")
    if not isinstance(body, dict) or set(body) - {"id", "label", "kind", "share", "same_as"}:
        raise ConfirmError(400, "id, and optionally label, kind, share, same_as")
    folks = people(household)
    con = sqlite3.connect(str(places_db or os.environ.get("PLACES_DB") or places.DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        places.ensure_registry(con)
        r = con.execute("SELECT * FROM places WHERE id=?", (str(body.get("id") or ""),)).fetchone()
        row = dict(r) if r else None
        if row is None or row.get("archived_at") or not _may_confirm(viewer, row, folks):
            raise ConfirmError(404, "no such place")
        aliases = json.loads(row.get("aliases") or "[]")
        if body.get("same_as") is not None:
            if row["owner"] != viewer:
                raise ConfirmError(403, "only the owner says two of their places are one")
            o = con.execute("SELECT * FROM places WHERE id=?", (str(body["same_as"]),)).fetchone()
            if o is None or o["owner"] != viewer or o["id"] == row["id"]:
                raise ConfirmError(404, "no such place")
            keep = dict(o)
            merged = sorted({*json.loads(keep.get("aliases") or "[]"), *aliases, row["label"]} - {keep["label"]})
            con.execute("UPDATE places SET aliases=?, sightings=sightings+?, confirmed_by=?, updated_at=? WHERE id=?",
                        (json.dumps(merged), int(row["sightings"] or 0), viewer, time.time(), keep["id"]))
            con.execute("DELETE FROM places WHERE id=?", (row["id"],))
            con.commit()
            row = dict(con.execute("SELECT * FROM places WHERE id=?", (keep["id"],)).fetchone())
            return _public(row)
        label = " ".join(str(body.get("label") or row["label"]).split())[:80]
        if not norm(label):
            raise ConfirmError(400, "a place needs a name")
        kind = str(body.get("kind") or (row["kind"] if row["kind"] != "candidate" else "named"))
        if kind not in KINDS:
            raise ConfirmError(400, "kind is one of " + ", ".join(KINDS))
        audience = row["audience"]
        if body.get("share") is not None:
            if body["share"] is not True:
                raise ConfirmError(400, "share is true, or left out")
            # #1397: only the owner widens, by an explicit share in their own words; a kid never widens, and a parent
            # never widens a kid's place
            if row["owner"] != viewer or role == "kid":
                raise ConfirmError(403, "only an adult owner shares their own place")
            audience = "household"
        if label != row["label"] and row["label"] not in aliases:
            aliases = sorted({*aliases, row["label"]})
        con.execute("UPDATE places SET label=?, kind=?, audience=?, aliases=?, confirmed_by=?, updated_at=? WHERE id=?",
                    (label, kind, audience, json.dumps(aliases), viewer, time.time(), row["id"]))
        con.commit()
        return _public(dict(con.execute("SELECT * FROM places WHERE id=?", (row["id"],)).fetchone()))
    finally:
        con.close()


# ── Settings › Household › Places (#1510): add, edit and remove, on the one registry ─────────────────────────────────
# The registry is the store of record. A name a person gives is their word (confirmed_by): a learned label (a seed
# rerun) never overwrites it. Remove archives the row (archived_at) and the seed never relearns it; undo puts the row
# back exactly as it was. Per-person places stay that person's: audience is the owner alone (plus a kid's parents), and
# widening to the household is only an adult owner's explicit choice.

VISIBILITY = ("private", "household")


def _row(con: sqlite3.Connection, pid: str) -> dict[str, Any] | None:
    r = con.execute("SELECT * FROM places WHERE id=?", (str(pid or ""),)).fetchone()
    return dict(r) if r else None


def _may_see(viewer: str | None, role: str | None, row: dict[str, Any]) -> bool:
    import audience as A
    return bool(viewer) and A.visible(row.get("audience") or "household", viewer, role, owner=row.get("owner"))


def listing(viewer: str | None, role: str | None, *, places_db: Any = None, household: Any = None) -> list[dict[str, Any]]:
    """The places this viewer may see, for Settings: confirmed places and learned ones that earned PLACE_SIGHTINGS,
    never an archived one, never another person's private one. `may_edit`: the owner, or a kid's parent."""
    import places
    if not viewer or role not in ("admin", "adult", "kid"):
        return []
    folks = people(household)
    con = sqlite3.connect(str(places_db or os.environ.get("PLACES_DB") or places.DB_PATH))
    con.row_factory = sqlite3.Row
    try:
        places.ensure_registry(con)
        rows = [dict(r) for r in con.execute("SELECT * FROM places ORDER BY id")]
    finally:
        con.close()
    out = []
    for r in rows:
        if r.get("archived_at") or not _may_see(viewer, role, r):
            continue
        if not r.get("confirmed_by") and int(r.get("sightings") or 0) < PLACE_SIGHTINGS:
            continue
        out.append({**_public(r), "owner": r.get("owner"), "mine": r.get("owner") == viewer,
                    "may_edit": _may_confirm(viewer, r, folks)})
    out.sort(key=lambda p: (norm(p["label"]), p["id"]))
    return out


def _taken(con: sqlite3.Connection, owner: str, label: str, *, skip: str | None = None) -> dict[str, Any] | None:
    """The owner's live place that already goes by this name (its label or an alias)."""
    key = norm(label)
    for r in con.execute("SELECT * FROM places WHERE owner=? AND archived_at IS NULL", (owner,)):
        r = dict(r)
        if r["id"] != skip and key in {norm(r["label"]), *(norm(a) for a in json.loads(r.get("aliases") or "[]"))}:
            return r
    return None


def _clean(body: dict[str, Any], allowed: set[str]) -> tuple[str | None, str | None, str | None]:
    if not isinstance(body, dict) or set(body) - allowed:
        raise ConfirmError(400, "only " + ", ".join(sorted(allowed)))
    name = body.get("name")
    if name is not None:
        name = " ".join(str(name).split())[:80]
        if not norm(name):
            raise ConfirmError(400, "a place needs a name")
    kind, vis = body.get("kind"), body.get("visibility")
    if kind is not None and kind not in KINDS:
        raise ConfirmError(400, "kind is one of " + ", ".join(KINDS))
    if vis is not None and vis not in VISIBILITY:
        raise ConfirmError(400, "visibility is private or household")
    return name, kind, vis


def _audience_for(owner: str, vis: str | None, role: str, folks: list[dict[str, Any]], current: str | None = None) -> str:
    if vis == "household":
        if role == "kid":
            raise ConfirmError(403, "only an adult owner shares their own place")      # #1397: a kid never widens
        return "household"
    if vis == "private":
        return audience_of(owner, folks)
    return current or audience_of(owner, folks)


def _open(places_db: Any):
    import places
    con = sqlite3.connect(str(places_db or os.environ.get("PLACES_DB") or places.DB_PATH))
    con.row_factory = sqlite3.Row
    places.ensure_registry(con)
    return con


def _gate(viewer: str | None, role: str | None) -> None:
    if not viewer or role not in ("admin", "adult"):
        raise ConfirmError(403, "a household place is added, changed or removed by an adult")


def add(viewer: str | None, role: str | None, body: dict[str, Any], *, places_db: Any = None,
        household: Any = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """{name, kind?, visibility?} → (the place, the row it replaced or None). The caller owns it; the server decides
    the audience (the owner alone unless an adult says household). A name the caller's own place already goes by is a
    409; a learned candidate by that name is promoted (their word replaces the learned label); an archived one comes
    back."""
    _gate(viewer, role)
    name, kind, vis = _clean(body, {"name", "kind", "visibility"})
    if not name:
        raise ConfirmError(400, "a place needs a name")
    folks = people(household)
    con = _open(places_db)
    try:
        live = _taken(con, viewer, name)
        if live is not None and (live.get("confirmed_by") or live["kind"] != "candidate"):
            raise ConfirmError(409, "you already have a place called that")
        existing = live
        if existing is None:
            for r in con.execute("SELECT * FROM places WHERE owner=? AND archived_at IS NOT NULL", (viewer,)):
                if norm(r["label"]) == norm(name):
                    existing = dict(r)
                    break
        before = dict(existing) if existing else None
        aud = _audience_for(viewer, vis, role, folks)
        if existing:
            keep = [a for a in json.loads(existing.get("aliases") or "[]") if norm(a) != norm(name)]
            con.execute("UPDATE places SET label=?, kind=?, audience=?, aliases=?, confirmed_by=?, archived_at=NULL, "
                        "updated_at=? WHERE id=?", (name, kind or "named", aud, json.dumps(keep), viewer, time.time(),
                                                    existing["id"]))
            pid = existing["id"]
        else:
            pid = "pl-" + hashlib.sha1(f"{viewer}|{norm(name)}|{time.time()}".encode()).hexdigest()[:12]
            con.execute("INSERT INTO places (id, label, kind, owner, audience, aliases, org_entity, sightings, "
                        "confirmed_by, updated_at) VALUES (?,?,?,?,?,?,NULL,0,?,?)",
                        (pid, name, kind or "named", viewer, aud, "[]", viewer, time.time()))
        con.commit()
        return _public(_row(con, pid)), before
    finally:
        con.close()


def edit(viewer: str | None, role: str | None, pid: str, body: dict[str, Any], *, places_db: Any = None,
         household: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """{name?, kind?, visibility?} → (the place, the row as it was). The owner (or a kid's parent) only; someone
    else's, or an archived one, is a 404. Their word wins and sticks: the old name stays as an alias, and nothing
    learned overwrites it."""
    _gate(viewer, role)
    name, kind, vis = _clean(body, {"name", "kind", "visibility"})
    folks = people(household)
    con = _open(places_db)
    try:
        row = _row(con, pid)
        if row is None or row.get("archived_at") or not _may_see(viewer, role, row) or not _may_confirm(viewer, row, folks):
            raise ConfirmError(404, "no such place")
        if vis is not None and row["owner"] != viewer:
            raise ConfirmError(403, "only the owner shares or unshares their own place")
        aliases = json.loads(row.get("aliases") or "[]")
        label = name or row["label"]
        if name and norm(name) != norm(row["label"]) and _taken(con, row["owner"], name, skip=row["id"]):
            raise ConfirmError(409, "there is already a place called that")
        if label != row["label"] and row["label"] not in aliases:
            aliases = sorted({*aliases, row["label"]})
        aliases = [a for a in aliases if norm(a) != norm(label)]
        k = kind or (row["kind"] if row["kind"] != "candidate" else "named")
        aud = _audience_for(row["owner"], vis, role, folks, row["audience"])
        con.execute("UPDATE places SET label=?, kind=?, audience=?, aliases=?, confirmed_by=?, updated_at=? WHERE id=?",
                    (label, k, aud, json.dumps(aliases), viewer, time.time(), row["id"]))
        con.commit()
        return _public(_row(con, row["id"])), row
    finally:
        con.close()


def remove(viewer: str | None, role: str | None, pid: str, *, places_db: Any = None,
           household: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """→ (the place, the row as it was). An archive, never a delete: the row stays, hidden everywhere, and the seed
    never relearns its name. Owner (or a kid's parent) only; else 404."""
    _gate(viewer, role)
    folks = people(household)
    con = _open(places_db)
    try:
        row = _row(con, pid)
        if row is None or row.get("archived_at") or not _may_see(viewer, role, row) or not _may_confirm(viewer, row, folks):
            raise ConfirmError(404, "no such place")
        con.execute("UPDATE places SET archived_at=?, updated_at=? WHERE id=?", (time.time(), time.time(), row["id"]))
        con.commit()
        return _public(row), row
    finally:
        con.close()


def restore(row: dict[str, Any] | None, pid: str, *, places_db: Any = None) -> bool:
    """Undo: put a place back as it was (`row`), or, for an add that made a new row, archive it. → whether a row changed."""
    con = _open(places_db)
    try:
        if row is None:
            n = con.execute("UPDATE places SET archived_at=? WHERE id=? AND archived_at IS NULL",
                            (time.time(), pid)).rowcount
        else:
            cols = [r[1] for r in con.execute("PRAGMA table_info(places)")]
            con.execute(f"INSERT OR REPLACE INTO places ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [row.get(c) for c in cols])
            n = 1
        con.commit()
        return bool(n)
    finally:
        con.close()


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "label": row["label"], "kind": row["kind"], "aliases": json.loads(row.get("aliases") or "[]"),
            "shared": row["audience"] == "household", "sightings": int(row.get("sightings") or 0),
            "confirmed": bool(row.get("confirmed_by"))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    p = plan()
    by_owner = Counter(c["owner"] for c in p["candidates"].values())
    print(json.dumps({"candidates": len(p["candidates"]), "by_owner": dict(sorted(by_owner.items())),
                      "reach_3_sightings": p["nodes"], "add": len(p["add"]), "update": len(p["update"]),
                      "drop": len(p["drop"]), "org_linked": sum(1 for c in p["candidates"].values() if c["org_entity"]),
                      "apply": a.apply}, indent=1))
    if a.apply:
        print(json.dumps({"written": apply(p)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
