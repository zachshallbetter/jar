#!/usr/bin/env python3
"""The threads view of a person's chat history (titan-node#1016): GET /v1/conversations?view=threads. Contract in
docs/chat-threads-api.md.

Threads are components of a person's own conversations joined by the linker's strong edges (chat_links: lineage,
continues, record; verified or accepted). Entity edges are only ever fringe ("possibly related"). Bundle, never
merge: every conversation keeps its own row, and `recent` always lists them all.

A thread is `bundled` only when every edge type it rests on measures at least BUNDLE_PRECISION against the person's
own baseline labels (link_statements, source "baseline"), over at least BUNDLE_MIN_LABELLED labelled pairs; until then
it's a suggestion. A name is earned (linker._name: an entity two chats share, or a phrase their titles share that's
rare in the person's own titles) above the cohesion floor; otherwise `name` is null and `description` describes it.

Only the asker's own stream (household_identity.conv_id_for). A shared screen has no history (403). A kid sees only
their own, never bundled.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

SINCE_DAYS = 30
MAX_THREADS = 30
BUNDLE_PRECISION = 0.90
BUNDLE_MIN_LABELLED = 20
STRONG_TYPES = ("lineage", "continues", "record")
AXES = ("kind", "person", "device", "project", "place", "room")
RESERVED_AXES: tuple[str, ...] = ()


def room_keys(http: Any = None) -> dict[str, str]:
    """The house's rooms, keyed the way a turn's home targets are (chat_links.targets: hooks.keyed("ha", …)): each HA
    entity id and each area name → its area. Matched against a conversation's own targets at read time, so links.db
    keeps no room text (#1016). {} when Home Assistant can't be read."""
    try:
        import hooks
        import home_assistant as ha
        areas = ha._areas(http or ha._http)
    except Exception:  # noqa: BLE001 - no Home Assistant: no rooms
        return {}
    out: dict[str, str] = {}
    for ent, area in areas.items():
        if area:
            out[hooks.keyed("ha", chat_links_norm(ent))] = area
            out[hooks.keyed("ha", chat_links_norm(area))] = area
    return out


def chat_links_norm(v: Any) -> str:
    import chat_links
    return chat_links._norm(v)


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()


def wilson_low(k: int, n: int, z: float = 1.96) -> float | None:
    if not n:
        return None
    p = k / n
    return round((p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n), 3)


def baseline(con: sqlite3.Connection, ids: set[str]) -> dict[tuple[str, str], bool]:
    """The person's labelling pass on their own pairs: the last baseline word per pair."""
    import linker
    con.executescript(linker.SCHEMA)
    if "source" not in {r[1] for r in con.execute("PRAGMA table_info(link_statements)")}:
        return {}
    out: dict[tuple[str, str], bool] = {}
    for a, b, rel in con.execute("SELECT a, b, related FROM link_statements WHERE source='baseline' ORDER BY at"):
        if a in ids and b in ids:
            out[linker._pair(a, b)] = bool(rel)
    return out


def precision(code: list[Any], labels: dict[tuple[str, str], bool]) -> dict[str, dict[str, Any]]:
    """Per edge type, the code's own edges (statements left out) judged against the baseline labels."""
    out = {}
    for typ in ("continues", "record", "entity"):
        pairs = {(e.a, e.b) for e in code if e.type == typ}
        judged = [labels[p] for p in pairs if p in labels]
        k, n = sum(judged), len(judged)
        p = round(k / n, 3) if n else None
        out[typ] = {"labelled": n, "related": k, "precision": p, "low95": wilson_low(k, n),
                    "bundles": bool(n >= BUNDLE_MIN_LABELLED and p is not None and p >= BUNDLE_PRECISION)}
    out["lineage"] = {"labelled": 0, "related": 0, "precision": None, "low95": None, "bundles": True}  # a branch is one
    return out


def project_ref(pid: str) -> str:
    """The statement side for a project: "not this project" is (conversation item, project_ref) said unrelated."""
    return "project:" + str(pid)


def _norm_name(name: str) -> set[str]:
    """A project's name as a repo or folder would spell it: "Harbor App" -> {"harbor app", "harbor-app", "harborapp"}."""
    n = " ".join(str(name or "").lower().split())
    return {n, n.replace(" ", "-"), n.replace(" ", "")} - {""}


def auto_project(item: str, keys: set[tuple[str, str]], projects: list[dict[str, Any]],
                 said: dict[tuple[str, str], bool]) -> dict[str, Any] | None:
    """The one visible project a conversation's project key names (a file's project, a repo, @code's run_agent),
    unless the person said it isn't; None when none or more than one matches."""
    import hooks
    import linker
    mine = {k for kd, k in keys if kd == "party:project"}
    if not mine:
        return None
    hits = [p for p in projects if {hooks.keyed("project", v) for v in _norm_name(p.get("name") or "")} & mine
            and said.get(linker._pair(item, project_ref(p.get("id")))) is not False]
    return {"id": hits[0]["id"], "name": hits[0].get("name") or "", "auto": True} if len(hits) == 1 else None


def place_at(events: list[dict[str, Any]], ts: float) -> str | None:
    """Where the asker was when a chat began: their own latest settled place event at or before it; an arrival names
    the place, a departure (or nothing) is unknown."""
    import places
    before = [e for e in events if places.event_ts(e) <= ts]
    return before[0]["place"] if before and before[0]["event"] == "arrived" else None


def reject_auto(pid: str, role: str | None, mem: sqlite3.Connection, cid: str, con: sqlite3.Connection | None = None,
                projects: list[dict[str, Any]] | None = None) -> str | None:
    """Undo an automatic project (PATCH /v1/conversations/{cid} with project null): the person's correction, kept as a
    statement so the same project is never offered for that chat again. → the project id rejected, or None."""
    import chat_links
    import hooks
    import household_identity as hh
    import linker
    con = con or hooks.connect()
    if projects is None:
        import spaces
        projects = spaces.projects_for(pid, role)
    iid = chat_links.item_id(cid)
    keys = set(_stored_keys(con, {iid}).get(iid, set()))
    noted = {r[1]: {"groups": r[4], "targets": r[5]} for r in con.execute("SELECT * FROM chat_turns WHERE person=?",
                                                                           (str(pid).lower(),))}
    for c in chat_links.load_convs(mem, hh.conv_id_for(pid), str(pid), 0, None, None, None, noted):
        if c.item_id == iid:
            keys |= c.keys
    got = auto_project(iid, keys, projects, linker.statements(con))
    if not got:
        return None
    linker.say(con, iid, project_ref(got["id"]), False, str(pid), "correction")
    return got["id"]


def _strength(e: Any) -> float:
    if e.state == "verified":
        return 1.0
    if e.state != "accepted":
        return 0.0
    return 0.8 if e.measured.get("local") == "yes" else 0.9


def _stored_local(con: sqlite3.Connection, owner: str, ids: set[str]) -> list[Any]:
    """The local model's continues edges from the last indexing run (the read path never asks a model)."""
    import chat_links
    chat_links.ensure(con)
    out = []
    for a, b, typ, state, typed, measured, why in con.execute(
            "SELECT a, b, type, state, typed, measured, why FROM chat_links WHERE owner=? AND type='continues'", (owner,)):
        m = json.loads(measured or "{}")
        if m.get("local") == "yes" and a in ids and b in ids:
            out.append(chat_links.Edge(a, b, typ, state, owner, json.loads(typed or "[]"), m, why))
    return out


def _stored_keys(con: sqlite3.Connection, ids: set[str]) -> dict[str, set[tuple[str, str]]]:
    """Hooks the indexing run stored (the backfill's facets from the turn log, among them)."""
    import chat_links
    out: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for iid, kind, key in con.execute("SELECT item_id, kind, key FROM hooks WHERE source=?", (chat_links.SOURCE,)):
        if iid in ids:
            out[iid].add((kind, key))
    return out


def build(pid: str, role: str | None, mem: sqlite3.Connection, *, limit: int = 50, kind: str = "", person: str = "",
          device: str = "", project: str = "", place: str = "", since: str = "", now: float | None = None,
          room: str = "", q: str = "", rooms: dict[str, str] | None = None,
          con: sqlite3.Connection | None = None, entities: Any = None, people: list[dict[str, Any]] | None = None,
          tags: dict[str, Any] | None = None, lineage: dict[str, Any] | None = None,
          projects: list[dict[str, Any]] | None = None, place_events: list[dict[str, Any]] | None = None
          ) -> dict[str, Any]:
    import chat_links
    import conversations
    import hooks
    import household_identity as hh
    import linker
    if role == "household" or not pid:
        raise Refused(403, "a shared screen has no history")
    now = time.time() if now is None else now
    con = con or hooks.connect()
    chat_links.ensure(con)
    conv = hh.conv_id_for(pid)
    rooms_by_key = room_keys() if rooms is None else rooms
    rows = conversations.list_for(mem, conv, 200)
    try:
        cut = datetime.fromisoformat(since).timestamp() if since else now - SINCE_DAYS * 86400
    except ValueError:
        raise Refused(400, "since is an ISO date or time")
    if people is None:
        people = chat_links._people()
    if entities is None:
        entities = hooks.entity_finder(pid, role)
    if tags is None:
        import spaces
        tags = spaces.tags_for(pid)
    if lineage is None:
        import spaces
        lineage = spaces.lineage_for(pid)
    if projects is None:
        import spaces
        projects = spaces.projects_for(pid, role)
    if place_events is None:
        try:   # the asker's own arrivals and departures, never anyone else's
            import places
            place_events = places.settled(places.recent(str(pid), limit=1000))
        except Exception:  # noqa: BLE001 - no place store: no place
            place_events = []
    others = [p for p in people if str(p.get("id")) != str(pid)]
    members = chat_links.members_finder(others)
    noted = {r[1]: {"groups": r[4], "targets": r[5]} for r in con.execute(
        "SELECT * FROM chat_turns WHERE person=? AND at >= ?", (str(pid).lower(), cut - 3600))}
    convs = chat_links.load_convs(mem, conv, str(pid), cut, entities, members, None, noted, lineage)
    ids = {c.item_id for c in convs}
    for iid, keys in _stored_keys(con, ids).items():
        c = next(c for c in convs if c.item_id == iid)
        c.keys |= keys
        c.groups |= {k for kd, k in keys if kd == "facet:kind"}
    code = chat_links.score(convs, None, None) + _stored_local(con, str(pid), ids)
    labels = baseline(con, ids)
    import chat_judge   # the local model's labels (#1016, 10-06): they count once they agree with the person's own
    judge_labels = chat_judge.judged(con, ids)
    agree = chat_judge.agreement(labels, judge_labels)
    prec = precision(code, {**judge_labels, **labels} if agree["trusted"] else labels)
    said = linker.statements(con)
    edges: dict[tuple[str, str], Any] = {}
    for e in code:
        stmt = said.get((e.a, e.b))
        if stmt is not None:
            e.state = "verified" if stmt else "rejected"
        edges[(e.a, e.b)] = e
    for (a, b), rel in said.items():
        if rel and a in ids and b in ids and (a, b) not in edges:
            edges[(a, b)] = chat_links.Edge(a, b, "continues", "verified", str(pid), [], {}, "you said they're related")

    # rows, with what the client filters and draws by
    pkey = {hooks.keyed("person", str(p.get("id"))): (str(p.get("id")), str(p.get("name") or "").split(" ")[0])
            for p in others}
    pnames = {str(p.get("id")): str(p.get("name") or "") for p in projects}
    by_item = {c.item_id: c for c in convs}
    out_rows = []
    for r in rows:
        iid = chat_links.item_id(r["id"])
        c = by_item.get(iid)
        tag = (tags.get(r["id"]) or {})
        # a manual tag always wins; otherwise the one project the chat's own project key names, Undo-able
        proj = ({"id": tag["project"], "name": pnames.get(str(tag["project"]), ""), "auto": False} if tag.get("project")
                else (auto_project(iid, c.keys, projects, said) if c else None))
        started = datetime.fromisoformat(r["started_at"]).timestamp()
        out_rows.append({**r, "item_id": iid, "project": proj, "group": tag.get("group"),
                         "kinds": sorted(c.groups) if c else [],
                         "people": sorted({pkey[k][0] for kd, k in (c.keys if c else ()) if kd == "party:person" and k in pkey}),
                         "place": place_at(place_events, started),
                         # the rooms this person's own turns targeted (a home_* tool on a device or an area there)
                         "rooms": sorted({rooms_by_key[k] for kd, k in (c.keys if c else ()) if kd == "target:home"
                                          and k in rooms_by_key})})
    row_of = {r["item_id"]: r for r in out_rows}
    # q: the viewer's own titles and own messages (their stream only, conversations.search), every word
    q_hits: set[str] | None = None
    if q.strip():
        words = conversations.terms(q)
        q_hits = {h["conversation"] for h in conversations.search(mem, conv, q, limit=100)}
        q_hits |= {r["id"] for r in out_rows if words and all(w in str(r.get("title") or "").lower() for w in words)}

    def matches(r: dict[str, Any]) -> bool:
        return ((not kind or kind in r["kinds"]) and (not person or person in r["people"])
                and (not device or (r.get("device") or "") == device)
                and (not project or (r.get("project") or {}).get("id") == project)
                and (not place or (r.get("place") or "").lower() == place.lower())
                and (not room or room.lower() in {x.lower() for x in r.get("rooms") or []})
                and (q_hits is None or r["id"] in q_hits))
    windowed = [r for r in out_rows if r["item_id"] in ids]

    # threads: components over strong edges
    # the code's strong types, and anything the person said is the same thread
    strong = [e for e in edges.values() if (e.type in STRONG_TYPES and e.state in ("verified", "accepted"))
              or (e.state == "verified" and said.get((e.a, e.b)) is True)]
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for e in strong:
        parent[find(e.a)] = find(e.b)
    comps: dict[str, list[str]] = defaultdict(list)
    for i in ids:
        comps[find(i)].append(i)
    titles = {r["item_id"]: r.get("title") or "" for r in out_rows}
    items = {}
    for c in convs:
        it = linker.Item(c.item_id, chat_links.SOURCE, datetime.fromtimestamp(c.start, timezone.utc).date(), c.owner, "private")
        for kd, k in c.keys:
            it.keys[kd].add(k)
        items[c.item_id] = it
    ent_names: dict[str, str] = {}
    try:
        import world
        w = world.get()
        for c in convs:
            for kd, k in c.keys:
                if kd == "party:entity" and k not in ent_names:
                    row = w.entity(k)
                    if row is not None:
                        ent_names[k] = str(dict(row).get("name") or "")
    except Exception:  # noqa: BLE001 - no world model: names from titles only
        pass
    corpus = linker._corpus(titles) if titles else None
    try:
        model_titles = chat_judge.titles(str(pid))
    except Exception:  # noqa: BLE001 - no titles store: names and descriptions only
        model_titles = {}
    threads = []
    for members_ in comps.values():
        if len(members_) < 2:
            continue
        ms = set(members_)
        es = [e for e in strong if e.a in ms and e.b in ms]
        cohesion = round(sum(_strength(e) for e in es) / len(es), 3) if es else 0.0
        order = sorted(members_, key=lambda i: row_of[i]["started_at"] if i in row_of else "")
        name = linker._name(order, items, cohesion, titles, ent_names, None, corpus)
        tid = "ct-" + hashlib.sha1("\0".join(sorted(order)).encode()).hexdigest()[:12]
        name_source = "earned" if name else ("model" if model_titles.get(tid) else None)
        name = name or model_titles.get(tid)
        kinds = Counter(k for i in order for k in (row_of.get(i) or {}).get("kinds", []))
        who = Counter(p for i in order for p in (row_of.get(i) or {}).get("people", []))
        label = {v[0]: v[1] for v in pkey.values()}
        desc = " · ".join([f"{len(order)} chats"] + [label.get(p, "") for p, _ in who.most_common(1)]
                          + [k for k, _ in kinds.most_common(1)])
        types = {e.type for e in es}
        bundled = role != "kid" and bool(types) and all(prec.get(t, {}).get("bundles") for t in types)
        fringe = [e for e in edges.values() if e.type == "entity" and e.state == "candidate" and ((e.a in ms) != (e.b in ms))]
        projs = {json.dumps((row_of.get(i) or {}).get("project"), sort_keys=True) for i in order}
        tproj = json.loads(projs.pop()) if len(projs) == 1 else None   # the thread's, when all its chats share one
        threads.append({
            "id": tid,   # same members, same id
            "name_source": name_source,
            "name": name, "description": desc, "cohesion": cohesion, "bundled": bundled,
            "started_at": min(row_of[i]["started_at"] for i in order if i in row_of),
            "last_at": max(row_of[i]["last_at"] for i in order if i in row_of),
            "kinds": [k for k, _ in kinds.most_common()], "people": [p for p, _ in who.most_common()], "project": tproj,
            "conversations": [row_of[i] for i in order if i in row_of],
            "edges": [{"a": e.a, "b": e.b, "type": e.type, "state": e.state, "why": e.why} for e in es],
            "fringe": [{"item_id": e.b if e.a in ms else e.a, "edge": {"a": e.a, "b": e.b, "type": e.type,
                                                                        "state": e.state, "why": e.why}} for e in fringe]})
    threads = [t for t in threads if any(matches(r) for r in t["conversations"])]
    threads.sort(key=lambda t: t["last_at"], reverse=True)

    facets: dict[str, list[dict[str, Any]]] = {}
    facets["kind"] = [{"value": k, "count": n} for k, n in Counter(k for r in windowed for k in r["kinds"]).most_common()]
    labels_p = {v[0]: v[1] for v in pkey.values()}
    facets["person"] = [{"value": p, "label": labels_p.get(p, ""), "count": n}
                        for p, n in Counter(p for r in windowed for p in r["people"]).most_common()]
    facets["device"] = [{"value": d, "count": n} for d, n in Counter(r.get("device") for r in windowed if r.get("device")).most_common()]
    facets["project"] = [{"value": p, "label": pnames.get(p, ""), "count": n}
                         for p, n in Counter((r.get("project") or {}).get("id") for r in windowed if r.get("project")).most_common()]
    facets["place"] = [{"value": p, "count": n} for p, n in Counter(r["place"] for r in windowed if r["place"]).most_common()]
    facets["room"] = [{"value": x, "count": n} for x, n in Counter(x for r in windowed for x in r.get("rooms") or []).most_common()]
    for ax in RESERVED_AXES:
        facets.setdefault(ax, [])
    lim = max(1, min(int(limit), 200))
    shown_threads = threads[:MAX_THREADS]
    shown_recent = [r for r in out_rows if matches(r)][:lim]
    # a spot-check pair is one the app can show: both chats among this view's rows (Desktop, jellybean#941)
    on_view = {r["item_id"] for r in shown_recent} | {r["item_id"] for t in shown_threads for r in t["conversations"]}
    checkable = {p: v for p, v in judge_labels.items() if p[0] in on_view and p[1] in on_view}
    return {"view": "threads",
            "filters": [{"axis": a, "value": v} for a, v in (("kind", kind), ("person", person), ("device", device),
                                                              ("project", project), ("place", place), ("room", room),
                                                              ("q", q.strip())) if v],
            "threads": shown_threads,
            "recent": shown_recent,
            "facets": facets,
            "precision": prec,
            "labels": {"yours": len(labels), "judge": len(judge_labels), "agreement": agree},
            "spotcheck": [] if role == "kid" else chat_judge.spotcheck(
                {(e.a, e.b): e.type for e in code}, checkable, labels),
            "since": _iso(cut)}
