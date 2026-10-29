#!/usr/bin/env python3
"""The relational FE graph, phase 2: typed and measured edges (docs/relational-fe-graph.md §5; #1399).

Read from the stores that already hold each relation (Box's extraction map on #1399, with Coordination's decisions),
never parsed from text and never invented:

  from / to      hooks party:* (a mail's From header; party:to once hooks.py mints it), resolved by key through the
                 world model's hard keys (domain, email, phone) or an entity id; a text's party is its counterparty,
                 so it stays candidate, unless the device said its direction (locator dir from/to, #1436): accepted
  mentions       hooks mention:entity; mention:place (a registry place id the device resolved, #1436: observed);
                 mention:person (a household person id the owner's device resolved from its text, #1396: measured,
                 accepted, the owner's own evidence only)
  (an item attr) when: a date:mention's time of day (locator time, #1436), item.attrs.when (stated_times)
  related        links rows (typed when the linker's reason is an identifier, else measured); link_statements win;
                 one owner's items naming the same registry place on the same household day: measured, accepted (#1396);
                 one owner's texts carrying the same thread:device (one conversation) on the same household day:
                 measured, accepted (#1650)
  replies_to     mail_server in_reply_to (one parent); chat_links lineage/continues (owner-only); a device text's
                 reply:device (an inline reply to the poster's own item, #1650: measured, accepted, owner-only)
  attends        calendar attendees (an email by key: accepted; a name: candidate); event_proposals participants
  organises      calendar organizer_is_me (the calendar's owner: verified); a name: candidate; a proposal's owner:
                 candidate
  at             calendar location, a proposal's place → the place registry (label or alias); unresolved: none
                 (the event keeps location_text)
  located_at     people.places; a registry place's org_entity (org → place); place_events (person → place, with
                 valid_from/valid_to, the person's own plus their parents', places.viewable)
  parent_of      household people.parents; world relation claims "parent"; other relations: knows
  member_of      world org.serves
  sees           world doctor / dentist / vet claims (health: the person and their parents only)
  depends_on     scenarios.deps
  about          an item whose date:mention day and resolved org (or the place it names) match a calendar event's day
                 and place (measured)

  decided       meeting_notes.decisions_for (JEV's meeting_decisions rows, #1421): the meeting's item → one item per
                 decision (md-<sig>-<idx>), owner-only, never parsed from the note

Not here: `paid`/`owes` and item→obligation `about` (per viewer, read
time, §3.1), `part_of`/threads (per viewer), `same_as` (the cross-walk keeps it).

Every edge carries its provenance: (source, ref, extractor, observed_at, owner, audience) per source row. A viewer
sees an edge only if they see both ends and every source (graph_sync.visible_edges).
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Iterable

EXTRACTOR = "graph_sync@p2"
STATES = ("rejected", "verified", "accepted", "candidate")      # merge precedence: a person's "no" always wins
RUNGS = ("observed", "attributed", "explained", "predicted")
TYPES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "from": (("item",), ("person", "org")),
    "to": (("item",), ("person", "org")),
    "mentions": (("item",), ("person", "org", "place", "event")),
    "about": (("item",), ("event", "obligation", "scenario")),
    "replies_to": (("item",), ("item",)),
    "attends": (("person",), ("event",)),
    "organises": (("person", "org"), ("event",)),
    "at": (("event",), ("place",)),
    "located_at": (("org", "person"), ("place",)),
    "member_of": (("person",), ("org", "person")),
    "parent_of": (("person",), ("person",)),
    "knows": (("person",), ("person", "org")),
    "sees": (("person",), ("org",)),
    "depends_on": (("scenario",), ("person", "org", "place", "item", "event", "obligation", "scenario")),
    "related": (("item",), ("item",)),
    "decided": (("item", "event"), ("item",)),
}
EDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (id INTEGER PRIMARY KEY, a TEXT NOT NULL, type TEXT NOT NULL, b TEXT NOT NULL,
    plane TEXT NOT NULL, state TEXT NOT NULL, rung TEXT NOT NULL, conf REAL NOT NULL, valid_from TEXT, valid_to TEXT,
    owner TEXT, audience TEXT NOT NULL, why TEXT NOT NULL, asserts TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_key ON edges(a, type, b, plane, ifnull(valid_from, ''), ifnull(valid_to, ''));
CREATE INDEX IF NOT EXISTS idx_edges_a ON edges(a);
CREATE INDEX IF NOT EXISTS idx_edges_b ON edges(b);
CREATE TABLE IF NOT EXISTS edge_prov (edge_id INTEGER NOT NULL, source TEXT NOT NULL, ref TEXT NOT NULL,
    extractor TEXT NOT NULL, observed_at TEXT, owner TEXT, audience TEXT NOT NULL, item TEXT, fence TEXT);
CREATE INDEX IF NOT EXISTS idx_edge_prov ON edge_prov(edge_id);
"""
_AUD_RANK = {"household": 4, "adults": 3, "admins": 2}       # anything else (private, a list) is narrowest


def _norm(s: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(s or "").lower()))


def prov(source: str, ref: str, owner: str | None, audience: str | None, observed_at: Any = None, *,
         item: str | None = None, fence: dict[str, Any] | None = None) -> dict[str, Any]:
    """One source row. item: the item it belongs to (checked like that item's node: audience, kb_acl, fences);
    fence: what a world claim's fences need ({classes, entities, subject}), checked with fences.admits."""
    return {"source": source, "ref": str(ref), "extractor": EXTRACTOR,
            "observed_at": None if observed_at is None else str(observed_at), "owner": owner,
            "audience": audience or "private", "item": item,
            "fence": json.dumps(fence, sort_keys=True) if fence else None}


def narrowest(srcs: Iterable[dict[str, Any]]) -> tuple[str | None, str]:
    """The edge's displayed owner and audience: its narrowest source's (§5.5). Visibility itself checks every source,
    so two different narrow audiences never widen each other."""
    best: tuple[int, str | None, str] | None = None
    for s in srcs:
        r = _AUD_RANK.get(s["audience"], 0)
        if best is None or r < best[0]:
            best = (r, s.get("owner"), s["audience"])
    return (best[1], best[2]) if best else (None, "private")


class Edge(dict):
    def __init__(self, a: str, type_: str, b: str, *, plane: str = "typed", state: str, rung: str, conf: float,
                 why: str, src: list[dict[str, Any]], valid_from: Any = None, valid_to: Any = None,
                 asserts: dict[str, Any] | None = None):
        assert type_ in TYPES and state in STATES and rung in RUNGS and plane in ("typed", "measured"), (type_, state)
        super().__init__(a=a, type=type_, b=b, plane=plane, state=state, rung=rung, conf=round(float(conf), 4),
                         valid_from=valid_from, valid_to=valid_to, why=why, asserts=asserts or None, prov=list(src))


class Ctx:
    """What resolution reads: the built nodes and cross-walk, the world's hard keys and the place registry."""

    def __init__(self, nodes: list[dict[str, Any]], xw: dict[tuple[str, str], str], world_rows: dict[str, Any],
                 registry: list[dict[str, Any]], people: list[dict[str, Any]]):
        import hooks
        self.nodes = {n["id"]: n for n in nodes}
        self.xw = xw
        self.people = {p["id"]: p for p in people}
        self.keys: dict[tuple[str, str], set[str]] = {}
        # #1380 (KB's review): who holds each key. A key only a person's own bubble holds (a contact's phone) may resolve
        # a hook, but the edge then carries that holder as a source, so only they see it (visible_edges): a private
        # word or key never reaches another person's graph. A household entity or a member's own key is public.
        self.key_public: set[tuple[str, str]] = set()
        self.key_private: dict[tuple[str, str], set[tuple[str, str | None, str]]] = {}
        for bubble, w in world_rows.items():
            for e in w["entities"]:
                nid = self.world(f"{bubble}:{e['id']}")
                if not nid:
                    continue
                holder = None if bubble == "household" else (f"{bubble}:{e['id']}", e.get("owner") or bubble,
                                                             e.get("audience") or "private")
                vals = [(c["attr"], c["value"]) for c in w["claims"] if c["entity_id"] == e["id"]]
                vals += [("domain", a["alias"]) for a in w["aliases"] if a["entity_id"] == e["id"] and "." in a["alias"]
                         and "@" not in a["alias"] and " " not in a["alias"].strip()]
                for attr, v in vals:
                    v = str(v or "").strip()
                    if not v:
                        continue
                    if attr == "domain":
                        d = hooks.registrable(v.split("@")[-1].split("/")[0])
                        if d not in hooks.PUBLIC_MAIL:
                            self._hold(holder, ("party:domain", d), nid)
                    elif attr == "email" and "@" in v:
                        self._hold(holder, ("party:addr", hooks.keyed("addr", v.lower())), nid)
                    elif attr == "phone":
                        digits = re.sub(r"\D", "", v)
                        if len(digits) >= 7:
                            self._hold(holder, ("party:phone", hooks.keyed("phone", digits[-10:])), nid)
                    elif attr == "phone_key":       # #1380: already keyed (a contact's phone, never kept raw)
                        self._hold(holder, ("party:phone", v), nid)
                    elif attr == "email_key":
                        self._hold(holder, ("party:addr", v), nid)
        # a member's own addresses, keyed (#1424, member_addresses): a mail party, a recipient or an emailed
        # attendee resolves to them; a key a world entity also holds for someone else stays ambiguous (never guessed)
        self.member_keys: set[str] = set()
        for p in people:
            for k in p.get("addr_keys") or []:
                self.keys.setdefault(("party:addr", str(k)), set()).add(p["id"])
                self.key_public.add(("party:addr", str(k)))
                self.member_keys.add(str(k))
        self.places: dict[str, str] = {}
        self.place_rows = {r["id"]: r for r in registry}
        for r in registry:
            for name in [r["label"], *(r.get("aliases") or [])]:
                k = _norm(name)
                if k:
                    self.places.setdefault(k, r["id"])

    def _hold(self, holder: tuple[str, str | None, str] | None, key: tuple[str, str], nid: str) -> None:
        self.keys.setdefault(key, set()).add(nid)
        if holder is None:
            self.key_public.add(key)
        else:
            self.key_private.setdefault(key, set()).add(holder)

    def key_sources(self, kind: str, key: str) -> list[dict[str, Any]]:
        """The private holders a hook's key resolved through, as sources (#1380): none when a public holder has it."""
        k = ("party:addr" if kind == "party:to" else kind, key)
        if kind in ("party:entity", "mention:entity") or k in self.key_public:
            return []
        return [prov("world", f"{wid}#key", owner, audience) for wid, owner, audience in sorted(self.key_private.get(k, ()))]

    def world(self, wid: str) -> str | None:
        """A world entity id (bubble:entity) → its node, through fusion; None when it isn't a node."""
        nid = self.xw.get(("world", wid))
        hops = 0
        while nid and nid not in self.nodes and ("world", nid) in self.xw and hops < 4:
            nid, hops = self.xw[("world", nid)], hops + 1
        return nid if nid in self.nodes else None

    def party(self, kind: str, key: str) -> str | None:
        if kind in ("party:entity", "mention:entity"):
            return self.world(key)
        hits = self.keys.get(("party:addr" if kind == "party:to" else kind, key)) or set()
        return next(iter(hits)) if len(hits) == 1 else None     # two people behind one key: never guess

    def place(self, text: Any) -> str | None:
        """A free-text location → a place node (label or alias); None when unresolved or only a candidate."""
        pid = self.places.get(_norm(text))
        return pid if pid and pid in self.nodes else None

    def label(self, nid: str) -> str:
        n = self.nodes.get(nid) or {}
        return str(n.get("label") or nid)

    def item(self, iid: str) -> dict[str, Any] | None:
        n = self.nodes.get(iid)
        return n if n and n["kind"] == "item" else None


def _ok(ctx: Ctx, e: Edge) -> bool:
    ka, kb = TYPES[e["type"]]
    a, b = ctx.nodes.get(e["a"]), ctx.nodes.get(e["b"])
    return bool(a and b and a["kind"] in ka and b["kind"] in kb and e["a"] != e["b"])


def _rows(con: sqlite3.Connection | None, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    if con is None:
        return []
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []      # a store without that table (yet): no edges from it


def _loc(r: Any) -> dict[str, Any]:
    """A hook row's locator ({part, line}, a device's dir and time), {} when unreadable."""
    try:
        v = json.loads(r["locator"] or "{}")
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}


def stated_times(links: sqlite3.Connection | None, items: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Each item's stated times of day (#1436): a date:mention whose locator carries a time → "YYYY-MM-DDTHH:MM",
    household-zone local, sorted. Only from a hook row owned and shared exactly as its item, since a node attr carries
    no sources of its own (device text hooks are owner-only, as their items are)."""
    out: dict[str, set[str]] = {}
    for r in _rows(links, "SELECT item_id, key, locator, owner, audience FROM hooks WHERE kind='date:mention' "
                          "ORDER BY item_id"):
        it = items.get(r["item_id"])
        t = _loc(r).get("time")
        if not it or (r["owner"], r["audience"] or "private") != (it["owner"], it["audience"]):
            continue
        if isinstance(t, str) and re.fullmatch(r"\d{2}:\d{2}", t) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(r["key"])):
            out.setdefault(r["item_id"], set()).add(f"{r['key']}T{t}")
    return {k: sorted(v) for k, v in sorted(out.items())}


def _item_src(ctx: Ctx, iid: str, source: str, ref: str | None = None) -> dict[str, Any]:
    n = ctx.nodes[iid]
    return prov(source, ref or iid, n["owner"], n["audience"], (n.get("attrs") or {}).get("observed_at"), item=iid)


# ── items → people, orgs (hooks) ────────────────────────────────────────────────────────────────────────────────────

_MEMBER_ROLES = ("admin", "adult", "kid")


def _member(ctx: Ctx, nid: str) -> bool:
    """A household member's person node (people_nodes: admin, adult or kid; a shared screen is never one)."""
    n = ctx.nodes.get(nid) or {}
    return n.get("kind") == "person" and (n.get("attrs") or {}).get("role") in _MEMBER_ROLES


def hook_edges(ctx: Ctx, links: sqlite3.Connection | None) -> list[Edge]:
    out: list[Edge] = []
    by_item: dict[str, list[sqlite3.Row]] = {}
    for r in _rows(links, "SELECT item_id, source, kind, key, at, locator, owner, audience FROM hooks "
                          "ORDER BY item_id, kind, key"):
        by_item.setdefault(r["item_id"], []).append(r)
    for iid, rows in by_item.items():
        it = ctx.item(iid)
        if not it:
            continue
        device = it["src"].startswith("links:")               # a device text or chat turn: no direction known
        for r in rows:
            src = prov("hooks", f"{iid}:{r['kind']}", r["owner"] if r["owner"] is not None else it["owner"],
                       r["audience"] or it["audience"], item=iid)
            if r["kind"] in ("party:addr", "party:domain", "party:phone", "party:entity", "party:to"):
                who = ctx.party(r["kind"], r["key"])
                if not who:
                    continue
                src = [src] if isinstance(src, dict) else src
                src = src + ctx.key_sources(r["kind"], r["key"])   # a privately held key: its holder's edge only
                # a device's directed party (#1436): the device knows its text's sender or recipient
                d = _loc(r).get("dir") if r["kind"] in ("party:phone", "party:addr") else None
                if r["kind"] == "party:to" or d == "to":
                    out.append(Edge(iid, "to", who, state="accepted", rung="attributed", conf=1.0,
                                    why=f"sent to {ctx.label(who)}", src=src))
                elif d == "from":
                    out.append(Edge(iid, "from", who, state="accepted", rung="attributed", conf=1.0,
                                    why=f"sent by {ctx.label(who)}", src=src))
                elif device:
                    out.append(Edge(iid, "from", who, state="candidate", rung="attributed", conf=0.5,
                                    why=f"in a text thread with {ctx.label(who)}", src=src))
                else:
                    out.append(Edge(iid, "from", who, state="accepted", rung="attributed", conf=1.0,
                                    why=f"sent by {ctx.label(who)}", src=src))
            elif r["kind"] == "mention:entity":
                who = ctx.party(r["kind"], r["key"])
                if who:
                    out.append(Edge(iid, "mentions", who, state="accepted", rung="attributed", conf=0.8,
                                    why=f"mentions {ctx.label(who)}", src=[src]))
            elif r["kind"] == "mention:place":
                # a registry place id the device resolved from the text's own words (#1436): never free text here
                pl = r["key"] if (ctx.nodes.get(r["key"]) or {}).get("kind") == "place" else None
                if pl:
                    what = "the text" if (it.get("attrs") or {}).get("kind") == "text" else "it"
                    out.append(Edge(iid, "mentions", pl, state="accepted", rung="observed", conf=0.9,
                                    why=f"{what} names {ctx.label(pl)}", src=[src]))
            elif r["kind"] == "mention:person":
                # a household member the owner's device found named in its own text (#1396): an id, never a name. The
                # owner's evidence only: the hook row must be the item's owner's (a device posts only on its own
                # items), the person a member, and the edge stands on the hook row and the item, so only the owner
                # sees it. Measured (the device's reading of words), never typed and never upgraded here.
                who = r["key"] if _member(ctx, r["key"]) else None
                if who and it["owner"] and r["owner"] == it["owner"]:
                    what = "the text" if (it.get("attrs") or {}).get("kind") == "text" else "it"
                    out.append(Edge(iid, "mentions", who, plane="measured", state="accepted", rung="attributed",
                                    conf=0.7, why=f"{what} names {ctx.label(who)}",
                                    src=[src, _item_src(ctx, iid, "items")]))
    return out


# ── items → items ───────────────────────────────────────────────────────────────────────────────────────────────────

_LINK_CONF = {"verified": 1.0, "accepted": 0.7, "candidate": 0.3, "rejected": 0.0}


def link_edges(ctx: Ctx, links: sqlite3.Connection | None) -> list[Edge]:
    out: list[Edge] = []
    said: dict[tuple[str, str], sqlite3.Row] = {}
    # a person's own word only, as linker.statements reads it: the local judge's labels (source "judge", #1016/#1125,
    # 75% agreement, not trusted) never verify or reject an edge
    cols = {r[1] for r in _rows(links, "PRAGMA table_info(link_statements)")}
    judge = (" WHERE COALESCE(source, '') <> 'judge' AND COALESCE(by, '') NOT LIKE 'judge:%'" if "source" in cols
             else " WHERE COALESCE(by, '') NOT LIKE 'judge:%'")
    for s in _rows(links, "SELECT a, b, related, by, at FROM link_statements" + judge + " ORDER BY at"):
        said[tuple(sorted((s["a"], s["b"])))] = s                 # the newest statement per pair wins
    seen: set[tuple[str, str]] = set()
    for r in _rows(links, "SELECT a, b, state, typed, why FROM links ORDER BY a, b"):
        a, b = sorted((r["a"], r["b"]))
        if not (ctx.item(a) and ctx.item(b)):
            continue
        seen.add((a, b))
        plane = "typed" if str(r["typed"] or "").startswith("id:") else "measured"
        state = r["state"]
        src = [_item_src(ctx, a, "links", f"{a}|{b}"), _item_src(ctx, b, "links", f"{b}|{a}")]   # as its two items
        s = said.get((a, b))
        if s is not None:
            state = "verified" if s["related"] else "rejected"
            src.append(prov("link_statements", f"{a}|{b}", s["by"], "private", s["at"]))
        out.append(Edge(a, "related", b, plane=plane, state=state, rung="observed" if plane == "typed" else "attributed",
                        conf=_LINK_CONF.get(state, 0.3), why=str(r["why"] or ""), src=src))
    for (a, b), s in said.items():
        if (a, b) in seen or not (ctx.item(a) and ctx.item(b)):
            continue
        state = "verified" if s["related"] else "rejected"
        out.append(Edge(a, "related", b, plane="measured", state=state, rung="observed", conf=_LINK_CONF[state],
                        why=f"{'related' if s['related'] else 'not related'}, said by its owner",
                        src=[prov("link_statements", f"{a}|{b}", s["by"], "private", s["at"])]))
    for r in _rows(links, "SELECT a, b, type, state, owner, why FROM chat_links WHERE type IN ('lineage','continues') "
                          "AND state IN ('accepted','verified') ORDER BY a, b"):
        if ctx.item(r["a"]) and ctx.item(r["b"]):
            out.append(Edge(r["b"], "replies_to", r["a"], state=r["state"], rung="observed", conf=1.0,
                            why=str(r["why"] or "continues the chat"),
                            src=[prov("chat_links", f"{r['a']}|{r['b']}", r["owner"], "private", item=r["a"]),
                                 prov("chat_links", f"{r['b']}|{r['a']}", r["owner"], "private", item=r["b"])]))
    return out


MAX_SAME_PLACE = 8      # more of one owner's items naming one place on one day is a hub (Home), not a plan: no links


def _item_day(ctx: Ctx, iid: str) -> str | None:
    """The household day an item belongs to: its observed_at (a ledger timestamp, or the day its device posted) read in
    the household zone through local_time, never the host's UTC day (#1572). None when it has none."""
    import local_time
    raw = ((ctx.nodes.get(iid) or {}).get("attrs") or {}).get("observed_at")
    t = local_time.parse_iso(raw) if raw else None
    if t is None:
        return None
    z = local_time.zone()
    return (t.astimezone(z) if (t.tzinfo and z is not None) else t).date().isoformat()


def same_place_day_edges(ctx: Ctx, links: sqlite3.Connection | None, edges: list[Edge]) -> list[Edge]:
    """`related`, measured (#1396): one owner's items that name the same registry place on the same household day. Two
    independent matches (a place the device resolved, a day), so accepted; never typed and never stronger. Only one
    owner's own items, only from accepted or verified mentions (a candidate never links), and the edge stands on both
    items' mention rows, both items and the place, so its owner and audience are the narrowest of them and a viewer
    sees it only with every one of them. A pair someone has already linked or judged (any plane, any state) keeps
    that link: a person's "no" is never overridden."""
    known = {tuple(sorted((e["a"], e["b"]))) for e in edges if e["type"] == "related"}
    groups: dict[tuple[str, str, str], dict[str, Edge]] = {}
    for e in edges:
        if e["type"] != "mentions" or e["state"] not in ("accepted", "verified") or not ctx.item(e["a"]):
            continue
        if (ctx.nodes.get(e["b"]) or {}).get("kind") != "place":
            continue
        owner = ctx.nodes[e["a"]].get("owner")
        day = _item_day(ctx, e["a"])
        if owner and day:
            groups.setdefault((owner, e["b"], day), {})[e["a"]] = e
    out: list[Edge] = []
    for (owner, pl, day), members in sorted(groups.items()):
        ids = sorted(members)
        if len(ids) < 2 or len(ids) > MAX_SAME_PLACE:
            continue
        place = ctx.nodes[pl]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if (a, b) in known:
                    continue
                src = [*members[a]["prov"], *members[b]["prov"], _item_src(ctx, a, "items"), _item_src(ctx, b, "items"),
                       prov("places", pl, place.get("owner"), place.get("audience"))]
                out.append(Edge(a, "related", b, plane="measured", state="accepted", rung="attributed", conf=0.6,
                                why=f"both name {ctx.label(pl)} on {day}", src=src))
    return out


MAX_SAME_THREAD = 8     # more of one owner's texts in one conversation on one day is a busy chat, not a plan: no links


def _own_hooks(ctx: Ctx, links: sqlite3.Connection | None, kind: str) -> list[tuple[sqlite3.Row, str]]:
    """(hook row, owner) for one kind's rows that are their item's owner's own word: the item is a node with an owner
    and the hook row is that owner's (a device posts only on its own items, so another owner's row is never read)."""
    out = []
    for r in _rows(links, "SELECT item_id, kind, key, at, locator, owner, audience FROM hooks WHERE kind=? "
                          "ORDER BY item_id, key", (kind,)):
        it = ctx.item(r["item_id"])
        if it and it.get("owner") and r["owner"] == it["owner"]:
            out.append((r, it["owner"]))
    return out


def _hook_src(ctx: Ctx, r: sqlite3.Row) -> dict[str, Any]:
    return prov("hooks", f"{r['item_id']}:{r['kind']}", r["owner"], r["audience"] or "private", item=r["item_id"])


def _judged(edges: list[Edge]) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(pairs already related in any plane or state, pairs someone said are not related)."""
    known = {tuple(sorted((e["a"], e["b"]))) for e in edges if e["type"] == "related"}
    no = {tuple(sorted((e["a"], e["b"]))) for e in edges if e["type"] == "related" and e["state"] == "rejected"}
    return known, no


def same_thread_day_edges(ctx: Ctx, links: sqlite3.Connection | None, edges: list[Edge]) -> list[Edge]:
    """`related`, measured (#1650): one owner's texts that carry the same thread:device value (one conversation on
    their device) on the same household day. The device's own grouping plus a day, so accepted like a same-place pair
    (#1396); never typed and never stronger. Only one owner's own hook rows on their own items (a value two owners
    happen to share never joins them), and the edge stands on both hook rows and both items, so its audience is the
    narrowest of them. A pair already linked or judged (any plane, any state) keeps that link; a busy conversation
    (more than MAX_SAME_THREAD of one owner's texts on one day) links nothing."""
    known, _ = _judged(edges)
    groups: dict[tuple[str, str, str], dict[str, sqlite3.Row]] = {}
    for r, owner in _own_hooks(ctx, links, "thread:device"):
        day = _item_day(ctx, r["item_id"])
        if day:
            groups.setdefault((owner, str(r["key"]), day), {}).setdefault(r["item_id"], r)
    out: list[Edge] = []
    for (owner, _ref, day), members in sorted(groups.items()):
        ids = sorted(members)
        if len(ids) < 2 or len(ids) > MAX_SAME_THREAD:
            continue
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if (a, b) in known:
                    continue
                src = [_hook_src(ctx, members[a]), _hook_src(ctx, members[b]), _item_src(ctx, a, "items"),
                       _item_src(ctx, b, "items")]
                out.append(Edge(a, "related", b, plane="measured", state="accepted", rung="attributed", conf=0.6,
                                why=f"one text thread on {day}", src=src))
    return out


def device_reply_edges(ctx: Ctx, links: sqlite3.Connection | None, edges: list[Edge]) -> list[Edge]:
    """`replies_to`, measured (#1650): a device text's reply:device, an inline reply the device read from its own
    message store, to an item of the same owner (hooks.put_device keeps only those; checked again here: both items
    one owner's, the hook row theirs). Accepted, never verified (the device's word, not a server header). Never over a
    pair someone said is not related, nor over a reply the graph already holds. Stands on the hook row and both
    items, so only their owner sees it."""
    known = {(e["a"], e["b"]) for e in edges if e["type"] == "replies_to"}
    _, no = _judged(edges)
    out: list[Edge] = []
    for r, owner in _own_hooks(ctx, links, "reply:device"):
        child, parent = r["item_id"], str(r["key"])
        p = ctx.item(parent)
        if not p or p.get("owner") != owner or parent == child:
            continue
        if (child, parent) in known or tuple(sorted((child, parent))) in no:
            continue
        out.append(Edge(child, "replies_to", parent, plane="measured", state="accepted", rung="observed", conf=0.9,
                        why="an inline reply", src=[_hook_src(ctx, r), _item_src(ctx, child, "items"),
                                                    _item_src(ctx, parent, "items")]))
    return out


def mail_edges(ctx: Ctx, mail: sqlite3.Connection | None) -> list[Edge]:
    out: list[Edge] = []
    by_mid: dict[tuple[str, str], str] = {}
    rows = _rows(mail, "SELECT account, owner, item_id, message_id, in_reply_to FROM messages ORDER BY item_id")
    for r in rows:
        by_mid[(r["account"], str(r["message_id"]).lower())] = r["item_id"]
    for r in rows:
        if not r["in_reply_to"]:
            continue
        parent = by_mid.get((r["account"], str(r["in_reply_to"]).lower()))
        if parent and ctx.item(r["item_id"]) and ctx.item(parent):
            out.append(Edge(r["item_id"], "replies_to", parent, state="verified", rung="observed", conf=1.0,
                            why="reply", src=[prov("mail_server", r["item_id"], r["owner"], "private", item=r["item_id"]),
                                              prov("mail_server", parent, r["owner"], "private", item=parent)]))
    return out


# ── events ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def _attendee(ctx: Ctx, s: Any) -> tuple[str | None, str, list[dict[str, Any]]]:
    """A calendar attendee string → (person node, state, extra provenance): an email by key (accepted), a member's id
    (accepted), a member's full name or alias (candidate: a name never resolves a person, §7.1). A privately held
    key's holder rides as provenance, as in hook_edges."""
    import hooks
    t = str(s or "").strip()
    if not t:
        return None, "", []
    if "@" in t:
        k = hooks.keyed("addr", t.lower())
        who = ctx.party("party:addr", k)
        # a member's own keyed address: their own account says it's them (verified); a world contact's: accepted
        return who, ("verified" if who in ctx.people and k in ctx.member_keys else "accepted"), \
            (ctx.key_sources("party:addr", k) if who else [])
    if t in ctx.people:
        return t, "accepted", []
    k = _norm(t)
    hits = [pid for pid, p in ctx.people.items()
            if k and k in {_norm(p.get("name")), *(_norm(a) for a in p.get("aliases") or [])}]
    return (hits[0], "candidate", []) if len(hits) == 1 else (None, "", [])


def calendar_edges(ctx: Ctx, ledger_rows: list[dict[str, Any]]) -> list[Edge]:
    out: list[Edge] = []
    for r in ledger_rows:
        if r["source"] != "calendar":
            continue
        ev, iid = f"event:{r['item_id']}", r["item_id"]
        if ev not in ctx.nodes:
            continue
        f = r["fields"]
        src = [prov("ledger", iid, r["owner"], r["audience"], r["observed_at"], item=iid)]
        for s in f.get("attendees") or []:
            pid, state, held = _attendee(ctx, s)
            if pid:
                out.append(Edge(pid, "attends", ev, state=state, rung="observed" if state == "verified" else "attributed",
                                conf=0.5 if state == "candidate" else 1.0,
                                why="on the invite", src=src + held))
        org = f.get("organiser") or f.get("organizer")
        if f.get("organizer_is_me") and r["owner"]:
            out.append(Edge(r["owner"], "organises", ev, state="verified", rung="observed", conf=1.0,
                            why=f"organised by {ctx.label(r['owner'])}", src=src))
        elif org:
            pid, state, held = _attendee(ctx, org)
            if pid:
                out.append(Edge(pid, "organises", ev, state=state, rung="observed" if state == "verified" else "attributed",
                                conf=0.5 if state == "candidate" else 1.0,
                                why=f"organised by {ctx.label(pid)}", src=src + held))
        pl = ctx.place(f.get("location"))
        if pl:
            out.append(Edge(ev, "at", pl, state="accepted", rung="attributed", conf=1.0, why=f"at {ctx.label(pl)}", src=src))
    return out


def proposal_edges(ctx: Ctx, links: sqlite3.Connection | None) -> list[Edge]:
    out: list[Edge] = []
    for r in _rows(links, "SELECT owner, sig_id, place, participants, confidence, made_at FROM event_proposals "
                          "ORDER BY sig_id"):
        ev = f"event:ep-{r['sig_id']}"
        if ev not in ctx.nodes:
            continue
        src = [prov("event_proposals", f"{r['owner']}:{r['sig_id']}", r["owner"], "private", r["made_at"])]
        conf = float(r["confidence"] or 0.5)
        try:
            parts = json.loads(r["participants"] or "[]")
        except ValueError:
            parts = []
        for pid in parts:
            if pid in ctx.people:
                out.append(Edge(pid, "attends", ev, state="accepted", rung="attributed", conf=conf, why="in the plan",
                                src=src))
        if r["owner"] in ctx.people:
            out.append(Edge(r["owner"], "organises", ev, state="candidate", rung="attributed", conf=min(conf, 0.6),
                            why=f"proposed in {ctx.label(r['owner'])}'s texts", src=src))
        pl = ctx.place(r["place"])
        if pl:
            out.append(Edge(ev, "at", pl, state="accepted", rung="attributed", conf=conf, why=f"at {ctx.label(pl)}",
                            src=src))
    return out


# ── people, orgs, places ────────────────────────────────────────────────────────────────────────────────────────────

def _kin_audience(ctx: Ctx, pid: str) -> str:
    """A person's own facts shown to them and, for a kid, their parents (places.viewable's rule, inverted)."""
    p = ctx.people.get(pid) or {}
    ids = [pid] + (list(p.get("parents") or []) if p.get("role") == "kid" else [])
    return json.dumps(sorted({str(x).lower() for x in ids}))


def people_edges(ctx: Ctx) -> list[Edge]:
    out: list[Edge] = []
    for pid, p in sorted(ctx.people.items()):
        src = [prov("household", pid, None, "household")]
        for par in p.get("parents") or []:
            if par in ctx.nodes:
                out.append(Edge(par, "parent_of", pid, state="verified", rung="observed", conf=1.0, why="parent", src=src))
        for name in p.get("places") or []:
            pl = ctx.place(name)
            if pl:
                out.append(Edge(pid, "located_at", pl, state="verified", rung="observed", conf=1.0,
                                why=f"{ctx.label(pid).split(' ')[0]}'s {ctx.label(pl)}", src=src))
    for pl_id, r in sorted(ctx.place_rows.items()):
        org = r.get("org_entity")
        onode = ctx.world(org) if org else None
        if onode and pl_id in ctx.nodes:
            out.append(Edge(onode, "located_at", pl_id, state="accepted", rung="attributed", conf=1.0,
                            why=f"{ctx.label(onode)} is at {ctx.label(pl_id)}",
                            src=[prov("places", pl_id, r.get("owner"), r.get("audience") or "household")]))
    return out


_PARENT = {"parent", "mother", "father", "mom", "dad", "mum"}
_SEES = {"doctor": "patient", "dentist": "patient", "pediatrician": "patient", "vet": "client"}


def world_edges(ctx: Ctx, world_rows: dict[str, Any]) -> list[Edge]:
    out: list[Edge] = []
    for bubble, w in sorted(world_rows.items()):
        ents = {e["id"]: e for e in w["entities"]}
        for c in w["claims"]:
            e = ents.get(c["entity_id"])
            if not e or not c.get("value_entity"):
                continue
            a, b = ctx.world(f"{bubble}:{e['id']}"), ctx.world(f"{bubble}:{c['value_entity']}")
            if not (a and b):
                continue
            import fences
            subj = ctx.world(f"{bubble}:{e['id']}")
            src = [prov("world", f"{bubble}:{e['id']}:{c['attr']}", e.get("owner"), e.get("audience") or "private",
                        fence={"classes": sorted(fences.attr_classes(c["attr"])), "entities": [f"{bubble}:{e['id']}"],
                               "subject": subj if subj in ctx.people else None})]   # the claim's own fences
            attr, val = c["attr"], _norm(c["value"])
            if attr == "relation" and val in _PARENT:
                out.append(Edge(a, "parent_of", b, state="accepted", rung="attributed", conf=1.0, why="parent", src=src))
            elif attr == "relation":
                out.append(Edge(a, "knows", b, state="accepted", rung="attributed", conf=0.8, why=str(c["value"]), src=src))
            elif attr == "serves":
                out.append(Edge(b, "member_of", a, state="accepted", rung="attributed", conf=0.8,
                                why=f"{ctx.label(a)} serves {ctx.label(b)}", src=src))
            elif attr in _SEES and a in ctx.people:
                src = [dict(src[0], owner=a, audience=_kin_audience(ctx, a))]   # health: theirs and their parents'
                out.append(Edge(a, "sees", b, state="accepted", rung="attributed", conf=0.8,
                                why=f"{ctx.label(a).split(' ')[0]}'s {attr}", src=src))
    return out


def place_event_edges(ctx: Ctx, places_db: sqlite3.Connection | None) -> list[Edge]:
    """Where a person was: their own place events, for them and (a kid's) their parents only, never the place's
    household audience (Coordination, 10-10)."""
    out: list[Edge] = []
    cols = {r[1] for r in _rows(places_db, "PRAGMA table_info(place_events)")}
    if not cols:
        return out
    region = " AND region IS NULL" if "region" in cols else ""
    for r in _rows(places_db, f"SELECT id, person, event, place, at FROM place_events WHERE 1=1{region} ORDER BY id"):
        pl = ctx.place(r["place"])
        if not pl or r["person"] not in ctx.people:
            continue
        arrived = str(r["event"]).lower() in ("arrived", "arrive", "enter", "entered")
        src = [prov("place_events", r["id"], r["person"], _kin_audience(ctx, r["person"]), r["at"])]
        out.append(Edge(r["person"], "located_at", pl, state="verified", rung="observed", conf=1.0,
                        valid_from=r["at"] if arrived else None, valid_to=None if arrived else r["at"],
                        why=f"{'arrived at' if arrived else 'left'} {ctx.label(pl)}", src=src))
    return out


def scenario_edges(ctx: Ctx, tasks: sqlite3.Connection | None) -> list[Edge]:
    out: list[Edge] = []
    for r in _rows(tasks, "SELECT task_id, owner, deps FROM scenarios ORDER BY task_id"):
        if r["task_id"] not in ctx.nodes:
            continue
        try:
            deps = json.loads(r["deps"] or "{}")
        except ValueError:
            deps = {}
        src = [prov("tasks", r["task_id"], r["owner"], "private")]
        for wid in deps.get("entities") or []:
            n = ctx.world(wid) or (wid if wid in ctx.nodes else None)
            if n:
                out.append(Edge(r["task_id"], "depends_on", n, state="verified", rung="observed", conf=1.0,
                                why=f"the task needs {ctx.label(n)}", src=src))
        for iid in deps.get("items") or []:
            if ctx.item(iid):
                out.append(Edge(r["task_id"], "depends_on", iid, state="verified", rung="observed", conf=1.0,
                                why=f"the task needs {ctx.label(iid)}", src=src))
    return out


def about_edges(ctx: Ctx, links: sqlite3.Connection | None, edges: list[Edge]) -> list[Edge]:
    """An item about a calendar event, measured: it names a day (date:mention) the event falls on AND it's from or
    mentions the org located at the event's place, or names that place itself (mention:place, #1436). Two independent
    matches, so accepted; never typed (§5.2)."""
    out: list[Edge] = []
    at = {e["a"]: e["b"] for e in edges if e["type"] == "at"}
    org_at = {}
    for e in edges:
        if e["type"] == "located_at" and ctx.nodes[e["a"]]["kind"] == "org":
            org_at.setdefault(e["b"], set()).add(e["a"])
    parties: dict[str, set[str]] = {}
    for e in edges:
        if e["type"] in ("from", "mentions") and e["state"] in ("accepted", "verified"):
            parties.setdefault(e["a"], set()).add(e["b"])
    days: dict[str, set[str]] = {}
    for r in _rows(links, "SELECT item_id, key FROM hooks WHERE kind='date:mention'"):
        days.setdefault(r["item_id"], set()).add(str(r["key"])[:10])
    for ev, n in sorted(ctx.nodes.items()):
        if n["kind"] != "event" or not n["src"].startswith("ledger"):
            continue
        start = str((n.get("attrs") or {}).get("start") or "")[:10]
        orgs = org_at.get(at.get(ev, ""), set()) | ({at[ev]} if ev in at else set())   # its place's orgs, or the place
        if not start or not orgs:
            continue
        for iid, ds in sorted(days.items()):
            hit = (parties.get(iid) or set()) & orgs
            if start in ds and hit and ctx.item(iid) and iid != n.get("fence_item"):
                o = sorted(hit)[0]
                out.append(Edge(iid, "about", ev, plane="measured", state="accepted", rung="attributed", conf=0.7,
                                why=f"same day and {ctx.label(o)} as {ctx.label(ev)}",
                                src=[_item_src(ctx, iid, "hooks", f"{iid}:date:mention")]))
    return out


def decisions(links: sqlite3.Connection | None, owners: Iterable[str]) -> tuple[list[dict[str, Any]], list[Edge]]:
    """Each owner's meeting decisions, read only through meeting_notes.decisions_for (never by SQL here): a decision
    item per row, owner-only and fenced as its meeting is, and a `decided` edge from the meeting's item. → (node
    specs, edges); the edges are checked against the nodes like any other."""
    import meeting_notes
    specs: list[dict[str, Any]] = []
    out: list[Edge] = []
    if links is None:
        return specs, out
    for owner in sorted(set(owners)):
        try:
            rows = meeting_notes.decisions_for(links, owner)
        except sqlite3.Error:
            return [], []          # no meeting tables in this store yet (read-only: never created here)
        for r in rows:
            nid = f"md-{r['sig_id']}-{r['idx']}"
            specs.append({"id": nid, "owner": owner, "label": r["text"], "meeting": r["kb_id"],
                          "attrs": {"title": r["text"], "source": "meeting", "kind": "decision", "observed_at": r["day"],
                                    "meeting_title": r["title"]}})
            out.append(Edge(r["kb_id"], "decided", nid, state="verified", rung="observed", conf=1.0,
                            why=f"decided at {r['title']}",
                            src=[prov("meeting_decisions", f"{r['sig_id']}:{r['idx']}", owner, "private", r["day"],
                                      item=r["kb_id"])]))
    return specs, out


# ── merge and store ─────────────────────────────────────────────────────────────────────────────────────────────────

def merge(ctx: Ctx, edges: list[Edge]) -> list[Edge]:
    """One edge per (a, type, b, plane, valid_from, valid_to): sources pooled, the strongest state (a person's "no" first), the highest
    confidence, every typing checked (§5.1); sorted, so a rebuild is byte-identical."""
    by: dict[tuple[str, str, str, str], Edge] = {}
    for e in edges:
        if not _ok(ctx, e) or not e["prov"]:
            continue                                   # no sources, no edge (§5.4)
        k = (e["a"], e["type"], e["b"], e["plane"], e["valid_from"] or "", e["valid_to"] or "")   # a sighting
        # is its own edge: never pooled with a standing fact ("Ava's school"), whose audience is wider
        cur = by.get(k)
        if cur is None:
            by[k] = e
            continue
        if STATES.index(e["state"]) < STATES.index(cur["state"]):
            cur.update(state=e["state"], rung=e["rung"], why=e["why"])
        cur["conf"] = max(cur["conf"], e["conf"])
        cur["prov"] += [p for p in e["prov"] if p not in cur["prov"]]
    out = []
    for k in sorted(by):
        e = by[k]
        e["prov"] = sorted(e["prov"], key=lambda p: (p["source"], p["ref"]))
        e["owner"], e["audience"] = narrowest(e["prov"])
        out.append(e)
    return out


def write(con: sqlite3.Connection, edges: list[Edge]) -> None:
    con.executescript(EDGE_SCHEMA)
    for e in edges:
        if not e.get("prov"):
            raise ValueError(f"an edge with no sources: {e['a']} {e['type']} {e['b']}")   # never written
        cur = con.execute("INSERT INTO edges (a, type, b, plane, state, rung, conf, valid_from, valid_to, owner, "
                          "audience, why, asserts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (e["a"], e["type"], e["b"], e["plane"], e["state"], e["rung"], e["conf"], e["valid_from"],
                           e["valid_to"], e["owner"], e["audience"], e["why"],
                           json.dumps(e["asserts"], sort_keys=True) if e["asserts"] else None))
        con.executemany("INSERT INTO edge_prov VALUES (?,?,?,?,?,?,?,?,?)",
                        [(cur.lastrowid, p["source"], p["ref"], p["extractor"], p["observed_at"], p["owner"],
                          p["audience"], p.get("item"), p.get("fence")) for p in e["prov"]])


def collect(ctx: Ctx, *, links: Any, mail: Any, places_db: Any, tasks: Any, ledger_rows: list[dict[str, Any]],
            world_rows: dict[str, Any], extra: list[Edge] | None = None) -> list[Edge]:
    raw: list[Edge] = list(extra or [])
    raw += hook_edges(ctx, links)
    raw += link_edges(ctx, links)
    raw += same_place_day_edges(ctx, links, [e for e in raw if _ok(ctx, e)])
    raw += mail_edges(ctx, mail)
    raw += same_thread_day_edges(ctx, links, [e for e in raw if _ok(ctx, e)])
    raw += device_reply_edges(ctx, links, [e for e in raw if _ok(ctx, e)])
    raw += calendar_edges(ctx, ledger_rows)
    raw += proposal_edges(ctx, links)
    raw += people_edges(ctx)
    raw += world_edges(ctx, world_rows)
    raw += place_event_edges(ctx, places_db)
    raw += scenario_edges(ctx, tasks)
    raw += about_edges(ctx, links, [e for e in raw if _ok(ctx, e)])
    return merge(ctx, raw)
