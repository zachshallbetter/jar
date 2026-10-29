#!/usr/bin/env python3
"""The relational FE graph, phases 1 and 2: nodes and edges (docs/relational-fe-graph.md §4, §5, §7, §9; #1399).

graph.db is a projection: it is rebuilt from the stores that hold the truth and holds viewer-independent rows only.
Deleting it loses nothing. People's own decisions (merges, "related / not related", a confirmed place) stay in
their kept stores (world merges, links.db link_statements, places.db) and are only read here.

  nodes   (id, kind)                     person · org · place · item · event · obligation · scenario
  node_sources (node_id, source, ref, owner, audience, fence_item, label, attrs)    #1461: what each node is made of.
                                         A node has its own source row, plus one for every private record fused into
                                         it (a person's own "Harbor Mobile Billing" into the household's Harbor Mobile).
                                         A viewer sees a node when any source is theirs, and its label and attrs are
                                         merged at read time from only the sources they may see.
  xwalk   (store, native_id, node_id)    every id a store already uses → its node (fusion: one node, many records)
  meta    (key, value)                   the last build's hash, counts and time
  edges   (a, type, b, plane, state, rung, conf, valid_from, valid_to, owner, audience, why, asserts)
  edge_prov (edge_id, source, ref, extractor, observed_at, owner, audience)    P2: graph_edges.py says which store
                                         field makes which edge; every edge cites the rows it stands on

Sources (read-only):
  people     household_identity: members, as person nodes p_…, visible to everyone in the house
  world      every bubble under WORLD_DIR: person entities with a person_id fuse into that member; other people,
             orgs: <bubble>:<entity>. An org in a person's bubble fuses into a household org only with a shared
             alias AND a shared hard key (a domain, phone, email or address); a name alone never merges (§7.1).
  places     places.db `places` (the registry, §7.2): a candidate becomes a node only once seen 3 times
  items      ledger.db items (connectors), kb_acl.db rows (notes), links.db hook rows (device texts, chat turns); a
             text's stated times of day (timed date:mention hooks, #1436) as attrs.when
  events     calendar items (event:<item id>) and links.db event_proposals (event:ep-<sig id>, the owner's own)
  obligation the obligations lens over bill/receipt records (obligation:<account>:<cycle>): the account and cycle
             only; its state depends on who asks and is computed at read time, never stored (§4)
  scenario   tasks.db scenarios: the owner's own

Safety: every source store is opened read-only (mode=ro); a deploy or an import writes nothing, and a dry run writes
nothing anywhere. The only writes are graph.db itself and, on the first real build, the empty `places` table in
places.db (CREATE TABLE IF NOT EXISTS, no other table touched). No timer runs it and AGENT_FE_GRAPH stays off: it's
run by hand, the dry run first, until P3's reader is proven.

Visibility (§5.5) is the stores' own rule, applied when reading: audience.visible on the node's owner and audience,
and for an item (and an event made from one) the fences too (fences.admits with the item's carried fences).
An edge (visible_edges) needs both ends visible and every source row visible, so a place event's edge is the person's
and their parents' whatever the place's audience; a kid's view and a shared screen get no edge between two other people.

  python3 scripts/graph_sync.py --full [--dry-run]     rebuild; the dry run prints counts per kind, nothing written
  python3 scripts/graph_sync.py --since [--dry-run]    the change gate (#1426): build in memory, write only when the
                                                        graph's own digest differs from graph.db's (else "unchanged")
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
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_time  # noqa: E402
DEFAULT_DB = Path("/ai/datasets/connectors/graph.db")   # beside the stores (spec §9): backed up, never a deploy path
DB = Path(os.environ.get("GRAPH_DB") or DEFAULT_DB)
KINDS = ("person", "org", "place", "item", "event", "obligation", "scenario")
HARD_KEYS = ("domain", "phone", "email", "address")
SCHEMA_VERSION = "2"          # 2: nodes(id, kind) + node_sources (#1461); 1: label, attrs and the rest on nodes
PLACE_SIGHTINGS = 3          # a candidate place is a node once seen this often (§7.2)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE TABLE IF NOT EXISTS node_sources (node_id TEXT NOT NULL, source TEXT NOT NULL, ref TEXT NOT NULL, owner TEXT,
    audience TEXT NOT NULL, fence_item TEXT, label TEXT NOT NULL DEFAULT '', attrs TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (node_id, source, ref));
CREATE INDEX IF NOT EXISTS idx_node_sources_node ON node_sources(node_id);
CREATE TABLE IF NOT EXISTS xwalk (store TEXT NOT NULL, native_id TEXT NOT NULL, node_id TEXT NOT NULL,
    PRIMARY KEY (store, native_id));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fused (world_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, name TEXT NOT NULL, owner TEXT,
    audience TEXT NOT NULL, why TEXT NOT NULL);
"""


def _norm(s: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def _key(v: Any) -> str:
    """A hard key's comparable form: a domain from an address or URL, digits from a phone, else the words."""
    s = str(v or "").strip().lower()
    m = re.search(r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)$", s.split("@")[-1].split("/")[0])
    if m and "." in m.group(1):
        return m.group(1)
    digits = re.sub(r"\D", "", s)
    return digits[-10:] if len(digits) >= 7 else _norm(s)


# ── sources (each reads its own store; viewer-independent) ─────────────────────────────────────────────────────────

def _ro(path: Path | str | None) -> sqlite3.Connection | None:
    """A store opened read-only (mode=ro): graph_sync never creates, migrates or writes a source store (a deploy or a
    dry run writes nothing anywhere). None when the store isn't there yet."""
    p = Path(path) if path else None
    if not p or not p.exists():
        return None
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _has(con: sqlite3.Connection | None, table: str) -> bool:
    return bool(con) and con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


class Node(dict):
    """One node: its own record (owner, audience, label, attrs, src, fence_item) and `extra`, the further sources
    fused into it (#1461): each {source, ref, owner, audience, fence_item, label, attrs}."""
    def __init__(self, nid: str, kind: str, owner: str | None, audience: str | None, label: str, src: str,
                 attrs: dict[str, Any] | None = None, fence_item: str | None = None):
        super().__init__(id=nid, kind=kind, owner=owner, audience=audience or "private", label=label or "",
                         attrs=attrs or {}, src=src, fence_item=fence_item, extra=[])


def people_nodes(path: Path | None = None) -> tuple[list[Node], dict[tuple[str, str], str]]:
    """Household members, read straight from the people table (read-only)."""
    con = _ro(path or os.environ.get("HOUSEHOLD_DB") or "/ai/datasets/connectors/household.db")
    out, xw = [], {}
    if not _has(con, "people"):
        return out, xw
    for r in con.execute("SELECT * FROM people ORDER BY id"):
        pid = str(r["id"])
        parents = json.loads(r["parents"] or "[]") if "parents" in r.keys() and r["parents"] else []
        if r["role"] == "household":
            continue                                            # a shared screen is a device, not a person
        aliases = json.loads(r["aliases"] or "[]") if "aliases" in r.keys() and r["aliases"] else []
        out.append(Node(pid, "person", None, "household", str(r["name"] or ""), "household",
                        {"name": str(r["name"] or ""), "aliases": sorted(aliases), "role": r["role"],
                         "parents": sorted(parents)}))
        xw[("household", pid)] = pid
    con.close()
    return out, xw


def people_rows(path: Path | None = None) -> list[dict[str, Any]]:
    """The members as the edges read them: id, name, role, parents, aliases, places (read-only)."""
    con = _ro(path or os.environ.get("HOUSEHOLD_DB") or "/ai/datasets/connectors/household.db")
    out: list[dict[str, Any]] = []
    if not _has(con, "people"):
        return out
    for r in con.execute("SELECT * FROM people ORDER BY id"):
        if r["role"] == "household":
            continue
        row = {"id": str(r["id"]), "name": str(r["name"] or ""), "role": r["role"]}
        for k in ("parents", "aliases", "places", "addr_keys"):
            try:
                row[k] = json.loads(r[k] or "[]") if k in r.keys() and r[k] else []
            except ValueError:
                row[k] = []
        out.append(row)
    con.close()
    return out


def _world_rows(root: Path) -> dict[str, dict[str, Any]]:
    """Every bubble's entities, aliases and current claims, read straight from its file (read-only)."""
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(root.glob("*.db")):
        try:
            con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            ents = [dict(r) for r in con.execute("SELECT * FROM entities")]
            al = [dict(r) for r in con.execute("SELECT entity_id, alias, alias_norm FROM aliases")]
            ve = ", value_entity" if "value_entity" in {r[1] for r in con.execute("PRAGMA table_info(claims)")} else ""
            cl = [dict(r) for r in con.execute(f"SELECT id, entity_id, attr, value, value_norm{ve} FROM claims "
                                               "WHERE status='current'")]
            ev = [dict(r) for r in con.execute("SELECT id, claim_id, owner, audience FROM evidence")] \
                if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence'").fetchone() else []
            con.close()
        except sqlite3.Error:
            continue
        out[f.stem] = {"entities": ents, "aliases": al, "claims": cl, "evidence": ev}
    return out


def _claim_sources(bubble: str, w: dict[str, Any], eid: str) -> list[dict[str, Any]]:
    """An entity's hard-key and role claims (domain, phone, email, address, role) as node sources, one per piece of
    evidence, each with that evidence's own owner and audience (#1461 review): an entity a viewer sees through one
    shared source never carries a value only private evidence supports. A claim with no evidence is no one's."""
    evs: dict[int, list[dict[str, Any]]] = {}
    for e in w.get("evidence") or []:
        evs.setdefault(e["claim_id"], []).append(e)
    out = []
    for c in sorted((c for c in w["claims"] if c["entity_id"] == eid and c["value"]
                     and (c["attr"] in HARD_KEYS or c["attr"] == "role")), key=lambda c: (c["attr"], str(c["value"]))):
        val: Any = str(c["value"]) if c["attr"] == "role" else [str(c["value"])]
        for e in sorted(evs.get(c["id"], []), key=lambda e: e["id"]):
            out.append({"source": "world-claim", "ref": f"{bubble}:{c['id']}:{e['id']}", "owner": e["owner"],
                        "audience": e["audience"] or "private", "fence_item": None, "label": "",
                        "attrs": {c["attr"]: val}})
    return out


def _world_attrs(w: dict[str, Any], e: dict[str, Any]) -> dict[str, Any]:
    """An entity's own row: its name and aliases only. Claim values come in as their own sources (_claim_sources)."""
    return {"name": e["name"], "aliases": sorted(a["alias"] for a in w["aliases"] if a["entity_id"] == e["id"])}


def world_nodes(root: Path | None = None) -> tuple[list[Node], dict[tuple[str, str], str]]:
    out, xw, _fused = world_fusion(root)
    return out, xw


def _keyed_of(attr: str, value: Any) -> tuple[str, str] | None:
    """A claim's hard key as hooks key it: a phone's last ten digits, an address lowercased; a *_key claim is already
    keyed (#1380). None for anything else."""
    import hooks
    v = str(value or "").strip()
    if not v:
        return None
    if attr == "phone_key":
        return ("phone", v)
    if attr == "email_key":
        return ("addr", v)
    if attr == "phone":
        digits = re.sub(r"\D", "", v)
        return ("phone", hooks.keyed("phone", digits[-10:])) if len(digits) >= 7 else None
    if attr == "email" and "@" in v:
        return ("addr", hooks.keyed("addr", v.lower()))
    return None


def _member_keys(rows: dict[str, dict[str, Any]], people: list[dict[str, Any]] | None = None
                 ) -> tuple[dict[tuple[str, str], set[str]], set[str]]:
    """Each household member's hard keys, keyed: their household entity's phone and email claims, and their own
    addresses (people.addr_keys, #1424). → (key → the members holding it, every member id)."""
    out: dict[tuple[str, str], set[str]] = {}
    ids: set[str] = set()
    for e in (rows.get("household") or {}).get("entities", []):
        if e["type"] == "person" and e.get("person_id"):
            ids.add(str(e["person_id"]))
            for c in rows["household"]["claims"]:
                if c["entity_id"] == e["id"]:
                    k = _keyed_of(c["attr"], c["value"])
                    if k:
                        out.setdefault(k, set()).add(str(e["person_id"]))
    try:
        people = people if people is not None else people_rows()
    except Exception:  # noqa: BLE001 - no people store (a test without one): the world's keys only
        people = []
    for p in people:
        ids.add(str(p["id"]))
        for k in p.get("addr_keys") or []:
            out.setdefault(("addr", str(k)), set()).add(p["id"])
    return out, ids


def _person_same_as(e: dict[str, Any], w: dict[str, Any],
                    member: tuple[dict[tuple[str, str], set[str]], set[str]]) -> tuple[str | None, str]:
    """A person in someone's own bubble → the household member it is, with why; (None, "") when nothing says so."""
    members, ids = member
    claims = [c for c in w["claims"] if c["entity_id"] == e["id"]]
    word = sorted({str(c["value"]) for c in claims if c["attr"] == "same_as_member" and str(c["value"]) in ids})
    if len(word) == 1:
        return word[0], "the owner's word"
    keys = {k for c in claims if (k := _keyed_of(c["attr"], c["value"]))}
    hit = sorted({m for k in keys for m in members.get(k, set())})
    if len(hit) == 1:
        kind = sorted({k[0] for k in keys if hit[0] in members.get(k, set())})[0]
        return hit[0], f"the same {'phone' if kind == 'phone' else 'email'}"
    return None, ""


def world_fusion(root: Path | None = None
                 ) -> tuple[list[Node], dict[tuple[str, str], str], list[dict[str, Any]]]:
    """World nodes, their cross-walk, and each private org fused into a household one (same_as, §7.1): kept with the
    private entity's own name, owner and audience, so only its owner learns it was merged (rel-3)."""
    import world
    root = Path(root or os.environ.get("WORLD_DIR") or world.WORLD_DIR)
    rows = _world_rows(root)
    out: list[Node] = []
    xw: dict[tuple[str, str], str] = {}
    fused: list[dict[str, Any]] = []
    # the household bubble's orgs: what a person's own org may fuse into
    hh_orgs: dict[str, dict[str, set[str]]] = {}
    for bubble, w in rows.items():
        for e in w["entities"]:
            if e.get("merged_into"):
                continue
            names = {_norm(e["name"])} | {_norm(a["alias"]) for a in w["aliases"] if a["entity_id"] == e["id"]}
            keys = {_key(c["value"]) for c in w["claims"] if c["entity_id"] == e["id"] and c["attr"] in HARD_KEYS}
            keys |= {_key(a["alias"]) for a in w["aliases"] if a["entity_id"] == e["id"] and "." in a["alias"]}
            e["_names"], e["_keys"] = names - {""}, keys - {""}
            if bubble == "household" and e["type"] == "org":
                hh_orgs[e["id"]] = {"names": e["_names"], "keys": e["_keys"]}
    members = _member_keys(rows)
    for bubble in sorted(rows):
        w = rows[bubble]
        for e in sorted(w["entities"], key=lambda x: x["id"]):
            nid = f"{bubble}:{e['id']}"
            if e.get("merged_into"):
                xw[("world", nid)] = f"{bubble}:{e['merged_into']}"
                continue
            if e["type"] == "person" and e.get("person_id"):
                xw[("world", nid)] = str(e["person_id"])          # a household member: one node (§7.1)
                continue
            if e["type"] not in ("person", "org"):
                continue                                        # activities and the rest: P2's edges, not nodes
            if e["type"] == "person" and bubble != "household":
                # #1380: a person's own contact is a household member only on the owner's word or a shared hard key
                # (the same keyed phone or email as exactly one member's), never by name. Only its owner learns it.
                pid, why = _person_same_as(e, w, members)
                if pid:
                    xw[("world", nid)] = pid
                    fused.append({"world_id": nid, "node_id": pid, "name": e["name"], "owner": e.get("owner"),
                                  "audience": e.get("audience") or "private", "why": why,
                                  "attrs": _world_attrs(w, e), "claims": _claim_sources(bubble, w, e["id"])})
                    continue
            if e["type"] == "org" and bubble != "household":
                hit = sorted(h for h, v in hh_orgs.items() if v["names"] & e["_names"] and v["keys"] & e["_keys"])
                if len(hit) == 1:
                    xw[("world", nid)] = f"household:{hit[0]}"   # same_as: a shared alias and a shared hard key
                    shared = sorted(hh_orgs[hit[0]]["keys"] & e["_keys"])
                    fused.append({"world_id": nid, "node_id": f"household:{hit[0]}", "name": e["name"],
                                  "owner": e.get("owner"), "audience": e.get("audience") or "private",
                                  "why": f"a shared alias and the same {'domain' if '.' in shared[0] else 'key'}",
                                  "attrs": _world_attrs(w, e), "claims": _claim_sources(bubble, w, e["id"])})
                    continue
            node = Node(nid, e["type"], e.get("owner"), e.get("audience") or "private", e["name"], "world",
                        _world_attrs(w, e))
            node["extra"] += _claim_sources(bubble, w, e["id"])
            out.append(node)
            xw[("world", nid)] = nid
    return out, xw, fused


def place_nodes(path: Path | None = None) -> tuple[list[Node], dict[tuple[str, str], str]]:
    import places
    con = _ro(path or os.environ.get("PLACES_DB") or places.DB_PATH)
    out, xw = [], {}
    if not _has(con, "places"):
        return out, xw                                          # no registry yet (created by the first real build)
    for r in con.execute("SELECT * FROM places ORDER BY id"):
        r = dict(r)
        if r.get("archived_at"):
            continue                                            # removed in Settings (#1510): archived, not a node
        if r["kind"] == "candidate" and int(r.get("sightings") or 0) < PLACE_SIGHTINGS:
            continue                                            # not earned yet: a candidate, not a node
        out.append(Node(r["id"], "place", r.get("owner"), r.get("audience") or "household", r["label"], "places",
                        {"org": r.get("org_entity"), "label": r["label"], "kind": r["kind"],
                         "aliases": sorted(_aliases(r.get("aliases"))), "sightings": int(r.get("sightings") or 0),
                         "candidate": r["kind"] == "candidate"}))
        xw[("places", r["id"])] = r["id"]
    con.close()
    return out, xw


def _aliases(v: Any) -> list[str]:
    try:
        return [str(x) for x in (json.loads(v) if isinstance(v, str) else (v or []))]
    except ValueError:
        return []


def registry_rows(path: Path | None = None) -> list[dict[str, Any]]:
    """Every registry row, candidates included (a calendar location that names a candidate stays text)."""
    import places
    con = _ro(path or os.environ.get("PLACES_DB") or places.DB_PATH)
    if not _has(con, "places"):
        return []
    out = [dict(r, aliases=_aliases(r["aliases"])) for r in con.execute("SELECT * FROM places ORDER BY id")
           if not dict(r).get("archived_at")]
    con.close()
    return out


def ensure_places(path: Path | None = None) -> None:
    """The only write graph_sync makes outside graph.db: the empty place registry (CREATE TABLE IF NOT EXISTS, no
    change to any existing table), on the first real build, never at import or in a dry run."""
    import places
    con = sqlite3.connect(str(path or os.environ.get("PLACES_DB") or places.DB_PATH))
    try:
        places.ensure_registry(con)
        con.commit()
    finally:
        con.close()


_ITEM_KIND = {"mail": "mail", "calendar": "event", "meeting": "meeting_notes", "text": "text",
              "conversation": "chat_turn", "chat": "chat_turn", "kb": "note"}
SUMMARY_MAX = 400


def summaries(path: Path | None = None) -> dict[str, str]:
    """Each mail item's envelope summary (already redacted by envelopes.py), capped; read-only."""
    con = _ro(path or os.environ.get("MAIL_SERVER_DB") or "/ai/datasets/connectors/mail_server.db")
    out: dict[str, str] = {}
    if not _has(con, "messages"):
        return out
    for iid, env in con.execute("SELECT item_id, envelope FROM messages WHERE envelope IS NOT NULL ORDER BY item_id"):
        try:
            s = " ".join(str((json.loads(env).get("interpretation") or {}).get("summary") or "").split())
        except (ValueError, AttributeError):
            continue
        if s:
            out[iid] = s[:SUMMARY_MAX]
    con.close()
    return out


def ledger_rows(ledger: Path | None = None) -> list[dict[str, Any]]:
    import connectors_runner as cx
    con = _ro(ledger or os.environ.get("CONNECTORS_LEDGER") or cx.LEDGER_PATH)
    out: list[dict[str, Any]] = []
    if not _has(con, "items"):
        return out
    for r in con.execute("SELECT item_id, source, owner, observed_at FROM items WHERE deleted_at IS NULL ORDER BY item_id"):
        v = con.execute("SELECT title, fields, audience FROM versions WHERE item_id=? ORDER BY at DESC LIMIT 1",
                        (r["item_id"],)).fetchone()
        try:
            fields = json.loads(v["fields"] or "{}") if v and v["fields"] else {}
        except ValueError:
            fields = {}
        out.append({"item_id": r["item_id"], "source": r["source"], "owner": r["owner"], "observed_at": r["observed_at"],
                    "title": v["title"] if v else "", "fields": fields if isinstance(fields, dict) else {},
                    "audience": (v["audience"] if v and v["audience"] else "private")})
    con.close()
    return out


def item_nodes(ledger: Path | None = None, acl: Path | None = None, links: Path | None = None
               ) -> tuple[list[Node], list[Node], dict[tuple[str, str], str]]:
    """Items from the connectors' ledger, the KB's sharing table (notes) and the link index (device and chat items);
    calendar items are also events. → (items, events, xwalk)."""
    import kb_privacy
    import hooks
    items: dict[str, Node] = {}
    events: list[Node] = []
    xw: dict[tuple[str, str], str] = {}
    summ = summaries()
    for r in ledger_rows(ledger):
        aud, fields = r["audience"], r["fields"]
        attrs = {"title": r["title"] or "", "source": r["source"], "kind": _ITEM_KIND.get(r["source"], r["source"]),
                 "observed_at": r["observed_at"]}
        if summ.get(r["item_id"]):
            attrs["summary"] = summ[r["item_id"]]     # the item's own audience and fence: only with the item
        n = Node(r["item_id"], "item", r["owner"], aud, r["title"] or "", f"ledger:{r['source']}", attrs,
                 fence_item=r["item_id"])
        items[n["id"]] = n
        xw[("ledger", r["item_id"])] = n["id"]
        if r["source"] == "calendar":
            ea = {"title": r["title"] or "", "start": fields.get("start"), "end": fields.get("end"),
                  "all_day": bool(fields.get("all_day"))}
            if fields.get("location"):
                ea["location"] = str(fields["location"])     # → location_text unless it resolves (finish())
            events.append(Node(f"event:{r['item_id']}", "event", r["owner"], aud, n["label"], "ledger:calendar", ea,
                               fence_item=r["item_id"]))
            xw[("calendar", r["item_id"])] = f"event:{r['item_id']}"
    acon = _ro(kb_privacy._db_path())
    if _has(acon, "acl"):
        for sid, owner, aud in acon.execute("SELECT source_id, owner, audience FROM acl ORDER BY source_id"):
            if sid in items:
                continue
            items[sid] = Node(sid, "item", owner, aud, "", "kb", {"source": "kb", "kind": "note"}, fence_item=sid)
            xw[("kb", sid)] = sid
    hcon = _ro(links or os.environ.get("LINKS_DB") or hooks.LINKS_DB)
    if _has(hcon, "hooks"):
        for iid, src, owner, aud in hcon.execute("SELECT item_id, source, owner, audience FROM hooks WHERE source IN "
                                                 "('text','conversation','chat') GROUP BY item_id ORDER BY item_id"):
            if iid not in items:
                items[iid] = Node(iid, "item", owner, aud, "", f"links:{src}",
                                  {"source": src, "kind": _ITEM_KIND.get(src, src)}, fence_item=iid)
                xw[("links", iid)] = iid
        import graph_edges as GE
        for iid, when in GE.stated_times(hcon, items).items():   # a text's stated time of day (#1436)
            items[iid]["attrs"]["when"] = when
        for iid, day in hcon.execute("SELECT item_id, MIN(key) FROM hooks WHERE kind='date:item' GROUP BY item_id"):
            if iid in items and str(items[iid]["src"]).startswith("links:") and "observed_at" not in items[iid]["attrs"]:
                items[iid]["attrs"]["observed_at"] = str(day)[:10]   # a device item's own day, as it posted it (#1396)
    if _has(hcon, "event_proposals"):
        for r in hcon.execute("SELECT owner, sig_id, title, start, end_at, place FROM event_proposals ORDER BY sig_id"):
            nid = f"event:ep-{r[1]}"
            ea = {"title": r[2] or "", "start": r[3], "end": r[4], "all_day": False}
            if r[5]:
                ea["location"] = str(r[5])
            events.append(Node(nid, "event", r[0], "private", r[2], "links:event_proposals", ea))
            xw[("event_proposals", f"{r[0]}:{r[1]}")] = nid
    if hcon:
        hcon.close()
    return list(items.values()), events, xw


def obligation_nodes(records: Callable[[], list[dict[str, Any]]] | None = None, today: date | None = None
                     ) -> tuple[list[Node], dict[tuple[str, str], str]]:
    """Each bill cycle as an account+cycle node, owned and shared as its bill record is. Its state (due, paid,
    contested…) depends on who asks (their word, words shared with them, their own device's texts): computed at read
    time by obligations.from_links / threads, never stored here."""
    import obligations as O
    if records is None:
        import agent_runtime_lib as lib
        records = lambda: [n for t in ("bill", "receipt") for n in lib.record_nodes(t)[0]]  # noqa: E731
    out, xw = [], {}
    try:
        recs = records()
    except Exception as exc:  # noqa: BLE001 - the record runtime unreachable: no obligation nodes this build, said
        print(f"[graph] bill records unreadable ({type(exc).__name__}): no obligation nodes", file=sys.stderr)
        return out, xw
    for t in O.threads(recs, today or local_time.today()):
        nid = f"obligation:{t.account}:{t.cycle.isoformat()}"
        out.append(Node(nid, "obligation", t.owner, t.audience or "private", t.vendor, "records:bill",
                        {"account": t.account, "account_label": t.vendor, "cycle": t.cycle.isoformat(),
                         "amount": t.amount, "due": t.due.isoformat() if t.due else None}))
        for p in t.pieces:
            if p.ref and not str(p.ref).endswith("said"):
                xw[("records", str(p.ref))] = nid
    return out, xw


def scenario_nodes(path: Path | None = None) -> tuple[list[Node], dict[tuple[str, str], str]]:
    import scenarios as sc
    con = _ro(path or os.environ.get("TASKS_DB") or sc.DB)
    out, xw = [], {}
    if _has(con, "scenarios"):
        for tid, owner, goal, state in con.execute("SELECT task_id, owner, goal, state FROM scenarios ORDER BY task_id"):
            out.append(Node(tid, "scenario", owner, "private", goal or "", "tasks", {"goal": goal or "", "state": state}))
            xw[("tasks", tid)] = tid
        con.close()
    return out, xw


# ── the build ──────────────────────────────────────────────────────────────────────────────────────────────────────

def collect(**src: Any) -> tuple[list[Node], dict[tuple[str, str], str]]:
    nodes: list[Node] = []
    xw: dict[tuple[str, str], str] = {}
    wn, wx, fused = world_fusion(src.get("world_root"))
    for part in (people_nodes(), (wn, wx), place_nodes(src.get("places"))):
        nodes += part[0]
        xw.update(part[1])
    items, events, x = item_nodes(src.get("ledger"), None, src.get("links"))
    nodes += items + events
    xw.update(x)
    for part in (obligation_nodes(src.get("records"), src.get("today")), scenario_nodes(src.get("tasks"))):
        nodes += part[0]
        xw.update(part[1])
    seen: set[str] = set()
    uniq = []
    for n in sorted(nodes, key=lambda n: n["id"]):
        if n["id"] not in seen:
            seen.add(n["id"])
            uniq.append(n)
    by_id = {n["id"]: n for n in uniq}
    for f in sorted(fused, key=lambda f: f["world_id"]):      # a fused private record is one more source of its node
        if f["node_id"] in by_id:
            by_id[f["node_id"]]["extra"].append({
                "source": "world", "ref": f["world_id"], "owner": f["owner"], "audience": f["audience"] or "private",
                "fence_item": None, "label": f["name"], "attrs": f.get("attrs") or {}})
            by_id[f["node_id"]]["extra"] += f.get("claims") or []
    return uniq, xw


def finish(nodes: list[Node], registry: list[dict[str, Any]]) -> None:
    """An event's location: kept as location_text only when it doesn't name a place node (the `at` edge says it)."""
    import graph_edges as GE
    ids = {n["id"] for n in nodes}
    names = {GE._norm(x): r["id"] for r in registry for x in [r["label"], *r["aliases"]] if GE._norm(x)}
    for n in nodes:
        loc = n["attrs"].pop("location", None) if n["kind"] == "event" else None
        if loc and names.get(GE._norm(loc)) not in ids:
            n["attrs"]["location_text"] = loc


def build(**src: Any) -> tuple[list[Node], dict[tuple[str, str], str], list[dict[str, Any]], list[dict[str, Any]]]:
    """P2: the nodes and cross-walk (collect), then the edges between them (graph_edges), every store read-only.
    → (nodes, xwalk, edges, fused): fused is each private org merged into a household one (same_as)."""
    import graph_edges as GE
    import hooks
    import places
    import scenarios as sc
    nodes, xw = collect(**src)
    _w, _x, fused = world_fusion(src.get("world_root"))
    registry = registry_rows(src.get("places"))
    finish(nodes, registry)
    import world
    wr = _world_rows(Path(src.get("world_root") or os.environ.get("WORLD_DIR") or world.WORLD_DIR))
    people = people_rows()
    links = _ro(src.get("links") or os.environ.get("LINKS_DB") or hooks.LINKS_DB)
    specs, decided = GE.decisions(links, [p["id"] for p in people])
    have = {n["id"]: n for n in nodes}
    for d in specs:
        if d["meeting"] in have and d["id"] not in have:     # a decision exists only with its meeting's item
            m = have[d["meeting"]]
            nodes.append(Node(d["id"], "item", d["owner"], "private", d["label"], "links:meeting_decisions", d["attrs"],
                              fence_item=m.get("fence_item") or d["meeting"]))
    nodes.sort(key=lambda n: n["id"])
    ctx = GE.Ctx(nodes, xw, wr, registry, people)
    mail = _ro(os.environ.get("MAIL_SERVER_DB") or "/ai/datasets/connectors/mail_server.db")
    pdb = _ro(src.get("places") or os.environ.get("PLACES_DB") or places.DB_PATH)
    tasks = _ro(src.get("tasks") or os.environ.get("TASKS_DB") or sc.DB)
    try:
        edges = GE.collect(ctx, links=links, mail=mail, places_db=pdb, tasks=tasks,
                           ledger_rows=ledger_rows(src.get("ledger")), world_rows=wr, extra=decided)
    finally:
        for c in (links, mail, pdb, tasks):
            if c is not None:
                c.close()
    return nodes, xw, edges, fused


def digest(nodes: list[Node], xw: dict[tuple[str, str], str], edges: list[dict[str, Any]] | None = None,
           fused: list[dict[str, Any]] | None = None) -> str:
    """Deterministic: the same stores give the same hash (two runs, same hash: P1's done-criterion)."""
    body = json.dumps({"nodes": [dict(n) for n in nodes], "xwalk": sorted(f"{s}|{i}|{n}" for (s, i), n in xw.items()),
                       **({"edges": [dict(e) for e in edges]} if edges else {}),
                       **({"fused": fused} if fused else {})}, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def write(nodes: list[Node], xw: dict[tuple[str, str], str], path: Path | None = None,
          edges: list[dict[str, Any]] | None = None, fused: list[dict[str, Any]] | None = None) -> str:
    p = Path(path or os.environ.get("GRAPH_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    # 0600 from the moment it exists (labels carry private titles), never the umask's mode until the replace; a
    # rollback journal, which SQLite creates with the file's own mode, so no -wal/-shm siblings at the umask's
    os.close(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=DELETE")
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO nodes VALUES (?,?)", [(n["id"], n["kind"]) for n in nodes])
    dump = lambda a: json.dumps(a, sort_keys=True, default=str)   # noqa: E731
    con.executemany("INSERT INTO node_sources VALUES (?,?,?,?,?,?,?,?)",
                    [row for n in nodes for row in
                     [(n["id"], n["src"], n["id"], n["owner"], n["audience"], n["fence_item"], n["label"], dump(n["attrs"]))]
                     + [(n["id"], x["source"], x["ref"], x["owner"], x["audience"], x["fence_item"], x["label"],
                         dump(x["attrs"])) for x in n["extra"]]])
    con.executemany("INSERT INTO xwalk VALUES (?,?,?)", [(s, i, n) for (s, i), n in sorted(xw.items())])
    if edges is not None:
        import graph_edges as GE
        GE.write(con, edges)
    con.executemany("INSERT INTO fused VALUES (?,?,?,?,?,?)", [(f["world_id"], f["node_id"], f["name"], f["owner"],
                                                                f["audience"], f["why"]) for f in fused or []])
    h = digest(nodes, xw, edges, fused)
    counts = {k: sum(1 for n in nodes if n["kind"] == k) for k in KINDS}
    if edges is not None:
        counts["edges"] = len(edges)
    con.executemany("INSERT INTO meta VALUES (?,?)", [("hash", h), ("counts", json.dumps(counts)),
                                                       ("built_at", str(time.time())), ("schema", SCHEMA_VERSION)])
    con.commit()
    con.close()
    os.replace(tmp, p)       # the old projection stays readable until the new one is whole
    return h


# ── reading it, per viewer ──────────────────────────────────────────────────────────────────────────────────────────

def visible_fn(viewer: str | None, role: str | None) -> Callable[[dict[str, Any]], bool]:
    """A node exists for a viewer only as its stores show it: audience, then the fences for items (§5.5)."""
    import audience as A
    import fences
    fs = fences.get()

    def ok(n: dict[str, Any]) -> bool:
        who = None if role == "household" else viewer
        if not A.visible(n["audience"], who, role, owner=n["owner"]):
            return False
        if n.get("fence_item"):
            return fs.admits(who, role, n["owner"], item=n["fence_item"])
        return True
    return ok


def _ro_graph(path: Path | None) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{Path(path or os.environ.get('GRAPH_DB') or DB)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _node_sources(con: sqlite3.Connection) -> dict[str, tuple[str, list[dict[str, Any]]]]:
    """Every node's kind and sources, in the order they were written (its own record first). A graph.db still in the
    first shape (label, attrs and the rest on `nodes`, before the migration or the next sync) reads as one source per
    node, so a deploy never leaves a window where the reader can't open it."""
    if _has(con, "node_sources"):
        rows = con.execute("SELECT n.id AS id, n.kind AS kind, s.source, s.ref, s.owner, s.audience, s.fence_item, "
                           "s.label, s.attrs FROM nodes n JOIN node_sources s ON s.node_id = n.id "
                           "ORDER BY n.id, s.rowid")
    else:
        rows = con.execute("SELECT id, kind, src AS source, id AS ref, owner, audience, fence_item, label, attrs "
                           "FROM nodes ORDER BY id")
    out: dict[str, tuple[str, list[dict[str, Any]]]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d["id"], (d["kind"], []))[1].append(d)
    return out


def _merge_attrs(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Attrs from the sources a viewer may see, first source first: a list is the union of the lists, anything else
    is its first source's value. One source reads back exactly as stored."""
    out: dict[str, Any] = {}
    for a in parts:
        for k, v in a.items():
            if k not in out:
                out[k] = v
            elif isinstance(out[k], list) and isinstance(v, list):
                both = out[k] + [x for x in v if x not in out[k]]
                out[k] = sorted(both) if all(isinstance(x, str) for x in both) else both
    return out


def _view(nid: str, kind: str, seen: list[dict[str, Any]]) -> dict[str, Any]:
    """One node as a viewer sees it: built only from the sources `seen` (a non-empty list, own record first). The
    owner, audience, src and fence_item are the first source's; the label is the first non-empty one."""
    head = seen[0]
    return {"id": nid, "kind": kind, "owner": head["owner"], "audience": head["audience"],
            "label": next((s["label"] for s in seen if s["label"]), ""),
            "attrs": _merge_attrs([json.loads(s["attrs"] or "{}") for s in seen]), "src": head["source"],
            "fence_item": head["fence_item"]}


def visible_nodes(viewer: str | None, role: str | None, path: Path | None = None) -> dict[str, dict[str, Any]]:
    """The nodes this viewer may see, by id (#1461): a node is theirs when any of its sources is, and its label and
    attrs are merged from only the sources they may see (an item's summary only ever with its item: the same row,
    the same audience and fence)."""
    con = _ro_graph(path)
    ok = visible_fn(viewer, role)
    out = {}
    for nid, (kind, srcs) in _node_sources(con).items():
        seen = [s for s in srcs if ok(s)]
        if seen:
            out[nid] = _view(nid, kind, seen)
    con.close()
    return out


def project(viewer: str | None, role: str | None, path: Path | None = None) -> dict[str, list[str]]:
    """The node ids this viewer may see, by kind."""
    out: dict[str, list[str]] = {k: [] for k in KINDS}
    for nid, n in sorted(visible_nodes(viewer, role, path).items()):
        out[n["kind"]].append(nid)
    return out


def same_as(viewer: str | None, role: str | None, node_id: str | None = None,
            path: Path | None = None) -> list[dict[str, Any]]:
    """The private records this viewer's view merged into a node (rel-3: "is Harbor Mobile Billing the same as Harbor
    Mobile?"): only the merged entity's owner (or its audience) learns it, and only for a node they can see."""
    import audience as A
    con = _ro_graph(path)
    seen = visible_nodes(viewer, role, path)
    who = None if role == "household" else viewer
    q, args = "SELECT * FROM fused", ()
    if node_id:
        q, args = q + " WHERE node_id=?", (node_id,)
    out = [dict(r) for r in con.execute(q + " ORDER BY world_id", args)
           if r["node_id"] in seen and A.visible(r["audience"], who, role, owner=r["owner"])]
    con.close()
    return out


def obligation_state(viewer: str | None, role: str | None, node_id: str, *, records: Any = None, today: date | None = None,
                     path: Path | None = None) -> dict[str, Any] | None:
    """An obligation's state for this viewer, at read time (§3.1: never stored): the agent's own bill lens
    (agent_runtime_lib._obligation_threads, the path "what's waiting on me" takes) over the bill records this viewer
    may see, with their own word and their own device's signals. → {state: open | paid, lens: the lens's state, on: the
    due day while open or the day it was settled, chain, records: the bill record ids in it, items: the mail each was
    filed from, when this viewer sees it}; None when the node isn't theirs to see. Item → obligation edges are this
    read's, never stored (§3.1: which records make a cycle depends on the viewer's threads)."""
    if node_id not in visible_nodes(viewer, role, path):
        return None
    import agent_runtime_lib as lib
    import obligations as O
    recs = records() if records else [n for t in ("bill", "receipt") for n in lib.record_nodes(t)[0]]
    recs = [n for n in recs if lib._node_visible(n, viewer, role)]
    src = {str(n.get("node_id")): str(((n.get("data") or {}).get("provenance") or {}).get("source_id") or "") for n in recs}
    for t in lib._obligation_threads(recs, today or lib._local_now().date(), viewer, role):
        if f"obligation:{t.account}:{t.cycle.isoformat()}" != node_id:
            continue
        refs = [p.ref for p in t.pieces if p.ref in src]           # the bill records, never "you said"
        base = {"lens": t.state, "chain": t.chain(), "records": refs,
                "items": sorted({src[r] for r in refs if src[r] and src[r] in visible_nodes(viewer, role, path)})}
        if t.state in O.OPEN:
            return {"state": "open", "on": t.due.isoformat() if t.due else None, **base}
        done = [p for p in t.pieces if p.kind in ("paid", "said_paid", "said_handled")]
        return {"state": "paid", "on": max(p.when for p in done).isoformat() if done else None, **base}
    return None


EDGE_STATES = ("verified", "accepted")


def _source_visible(p: dict[str, Any], who: str | None, role: str | None, seen: dict[str, Any], ids: set[str],
                    fs: Any) -> bool:
    """One source row, as its store shows it: its audience; an item's row only when that item is visible to the
    viewer (its node: audience with kb_acl, then fences), else the item's carried fences; a world claim's fences."""
    import audience as A
    if not A.visible(p["audience"], who, role, owner=p["owner"]):
        return False
    if p.get("item"):
        if p["item"] in ids:
            if p["item"] not in seen:          # the item's node, exactly as visible_nodes decides it
                return False
        elif not fs.admits(who, role, p["owner"], item=p["item"]):
            return False
    if p.get("fence"):
        f = json.loads(p["fence"])
        if not fs.admits(who, role, p["owner"], classes=f.get("classes") or (), entities=f.get("entities") or (),
                         subject=f.get("subject")):
            return False
    return True


def visible_edges(viewer: str | None, role: str | None, path: Path | None = None, *, a: str | None = None,
                  b: str | None = None, types: Iterable[str] | None = None,
                  states: Iterable[str] = EDGE_STATES) -> list[dict[str, Any]]:
    """The edges this viewer may see (§5.5): both ends visible to them, every source visible to them (so a place
    event's edge is the person's and their parents', whatever the place's audience), and on a kid's view or a
    shared screen no edge between two people unless it's the kid's own. Rejected and candidate edges only when
    `states` asks for them."""
    import fences
    con = _ro_graph(path)
    seen = visible_nodes(viewer, role, path)
    fs = fences.get()
    ids = {r[0] for r in con.execute("SELECT id FROM nodes")}
    who = None if role == "household" else viewer
    want_t, want_s = set(types or ()), set(states)
    q, args = "SELECT * FROM edges WHERE 1=1", []
    for col, v in (("a", a), ("b", b)):
        if v:
            q += f" AND {col}=?"
            args.append(v)
    out = []
    for r in con.execute(q + " ORDER BY a, type, b, plane", args).fetchall():
        e = dict(r)
        if e["state"] not in want_s or (want_t and e["type"] not in want_t):
            continue
        if e["a"] not in seen or e["b"] not in seen:
            continue
        if role in ("kid", "household") and seen[e["a"]]["kind"] == seen[e["b"]]["kind"] == "person" \
                and who not in (e["a"], e["b"]):
            continue
        src = [dict(p) for p in con.execute("SELECT source, ref, extractor, observed_at, owner, audience, item, fence "
                                            "FROM edge_prov WHERE edge_id=? ORDER BY source, ref", (e["id"],))]
        if not src or not all(_source_visible(p, who, role, seen, ids, fs) for p in src):
            continue                       # no sources: never shown; every source: as its own store would show it
        e["asserts"] = json.loads(e["asserts"]) if e["asserts"] else None
        e["prov"] = src
        del e["id"]
        out.append(e)
    con.close()
    return out


def migrate(path: Path | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Move an existing graph.db from the first shape (label, attrs, owner, audience, src and fence_item on `nodes`) to
    node_sources (#1461) without loss, in one transaction: every node's old columns become its own source row, the
    node row keeps only id and kind, and nothing commits unless every node's label and attrs are found unchanged in
    its source row. Idempotent: a graph already in the new shape is left alone (it answers "current"). Sources fused
    in from other records aren't in an old graph.db to move; the next sync adds them. Edges, xwalk, fused and meta
    are untouched except meta.schema; the stored hash stays, so the next --since sees the new build and rewrites.
    → {result: absent | current | would migrate | migrated, nodes, sources}."""
    p = Path(path or os.environ.get("GRAPH_DB") or DB)
    if not p.exists():
        return {"result": "absent"}
    con = sqlite3.connect(p, isolation_level=None)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(nodes)")}
        if not cols:
            return {"result": "absent"}
        if "label" not in cols:
            n = con.execute("SELECT count(*) FROM nodes").fetchone()[0]
            return {"result": "current", "nodes": n, "sources": con.execute(
                "SELECT count(*) FROM node_sources").fetchone()[0] if _has(con, "node_sources") else 0}
        n = con.execute("SELECT count(*) FROM nodes").fetchone()[0]
        if dry_run:
            return {"result": "would migrate", "nodes": n, "sources": n}
        con.execute("BEGIN IMMEDIATE")
        for stmt in [x.strip() for x in SCHEMA.split(";") if "node_sources" in x]:
            con.execute(stmt)                                 # node_sources and its index; nodes is rebuilt below
        con.execute("INSERT OR IGNORE INTO node_sources (node_id, source, ref, owner, audience, fence_item, label, attrs) "
                    "SELECT id, src, id, owner, audience, fence_item, label, attrs FROM nodes ORDER BY id")
        lost = con.execute("SELECT count(*) FROM nodes n WHERE NOT EXISTS (SELECT 1 FROM node_sources s WHERE "
                           "s.node_id=n.id AND s.source=n.src AND s.ref=n.id AND s.label=n.label AND s.attrs=n.attrs "
                           "AND s.audience=n.audience AND s.owner IS n.owner AND s.fence_item IS n.fence_item)"
                           ).fetchone()[0]
        if lost:
            con.execute("ROLLBACK")
            raise RuntimeError(f"graph.db migration: {lost} nodes not carried over; nothing changed")
        con.execute("CREATE TABLE nodes_v2 (id TEXT PRIMARY KEY, kind TEXT NOT NULL)")
        con.execute("INSERT INTO nodes_v2 SELECT id, kind FROM nodes")
        con.execute("DROP TABLE nodes")
        con.execute("ALTER TABLE nodes_v2 RENAME TO nodes")
        con.execute("CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind)")
        if _has(con, "meta"):
            con.execute("INSERT OR REPLACE INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
        m = con.execute("SELECT count(*) FROM nodes").fetchone()[0]
        s = con.execute("SELECT count(*) FROM node_sources").fetchone()[0]
        if m != n or s < n:
            con.execute("ROLLBACK")
            raise RuntimeError(f"graph.db migration: counts changed ({n} nodes → {m} nodes, {s} sources); nothing changed")
        con.execute("COMMIT")
        return {"result": "migrated", "nodes": m, "sources": s}
    finally:
        con.close()


def stored_hash(path: Path | None = None) -> str | None:
    """graph.db's recorded digest, read-only; None when there's no graph yet (or it can't be read)."""
    p = Path(path or os.environ.get("GRAPH_DB") or DB)
    if not p.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='hash'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def since(path: Path | None = None, *, dry_run: bool = False, **src: Any) -> dict[str, Any]:
    """The incremental pass (#1426): the change gate is the graph's own content digest, so it can't miss a change the
    graph holds and needs no list of stores to keep in step. Build in memory (every source read-only), compare with
    graph.db's digest, and write (the same atomic 0600 swap as --full) only when they differ. Identical to --full by
    construction. Fences aren't in graph.db (they apply at read time), so a fence change writes nothing yet shows at
    once. Patching only what changed waits until a house is big enough to matter (a full build is ~0.3 s live)."""
    nodes, xw, edges, fused = build(**src)
    h, before = digest(nodes, xw, edges, fused), stored_hash(path)
    out = {"hash": h, "was": before, "changed": h != before, "wrote": False}
    if h != before and not dry_run:
        ensure_places()
        write(nodes, xw, path, edges=edges, fused=fused)
        out["wrote"] = True
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--full", action="store_true")
    g.add_argument("--since", action="store_true")
    g.add_argument("--migrate", action="store_true", help="move an existing graph.db to node_sources (#1461), in place")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", help="with --migrate: the graph.db to migrate (default: GRAPH_DB)")
    a = ap.parse_args(argv)
    if a.migrate:
        print(json.dumps({**migrate(a.db, dry_run=a.dry_run), "mode": "migrate"}, indent=1))
        return 0
    if a.since:
        r = since(dry_run=a.dry_run)
        print(json.dumps({**r, "mode": "since", "result": "written" if r["wrote"] else (
            "would write" if r["changed"] else "unchanged")}, indent=1))
        return 0
    nodes, xw, edges, fused = build()
    counts = {k: sum(1 for n in nodes if n["kind"] == k) for k in KINDS}
    counts["edges"] = len(edges)
    by_type: dict[str, int] = {}
    for e in edges:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    if a.dry_run:
        print(json.dumps({"counts": counts, "edges_by_type": dict(sorted(by_type.items())), "xwalk": len(xw),
                          "hash": digest(nodes, xw, edges, fused)}, indent=1))
        return 0
    ensure_places()
    h = write(nodes, xw, edges=edges, fused=fused)
    print(json.dumps({"counts": counts, "xwalk": len(xw), "hash": h, "mode": "full"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
