#!/usr/bin/env python3
"""'File it' suggestions for Unsorted (titan-node#1087, the suggestion half), on #951's edges.

Unsorted is every item of the viewer's that isn't in a project. Each gets at most one suggested project, with a reason
templated from the edge evidence (never model prose), and `auto` true only when the link is verified: titan files on
its own then; accepted and candidate links stay suggestions. A rejected pair never suggests.

Privacy (the mock's leak: a kid's mail named in a parent's suggestion): only items the viewer may see count, as the
unsorted item, as the project member it links to, and in every count and word of the reason. A hidden member never
carries a suggestion and is never counted. Kids and shared screens get none (as threads, linker rule 8).

  suggest(items, edges, visible, projects, role=None) → {item_id: {project, reason, state, auto, via}}
  projects: [{"id", "name", "members": [item ids]}], already only those this viewer may see (the store's job, #1085)
  for_items(items, edges, project_names, role) → {items-API id: suggestion}: the items surface (#1084) adapter
  auto_file(person, role, …) → what titan filed on its own: verified suggestions only, through #1085's projects.file
                               (one undo token each); a pair the person undid is never filed again
  recent_auto(person) → the last AUTO_SHOW_S of those, not undone: Now's "Filed in … · Undo"
  python3 scripts/file_suggest.py [--apply]       every adult's chats (counts only); --apply files
"""
from __future__ import annotations

from collections import defaultdict
import json
from typing import Any, Callable, Iterable

RANK = {"verified": 3, "accepted": 2, "candidate": 1}
_KIND_NOUN = {"mail": ("mail", "mails"), "scan": ("scan", "scans"), "receipt": ("receipt", "receipts"),
              "bill": ("bill", "bills"), "file": ("file", "files"), "note": ("note", "notes"),
              "message": ("message", "messages"), "record": ("record", "records"), "chat": ("chat", "chats")}


def _lead(e: Any) -> str:
    """The reason's lead, from the edge's first typed relation: "Same order number as", "Filed from the same mail as";
    the person's own word: "You linked it to"."""
    if str(e.why or "").startswith("you said they're related"):
        return "You linked it to"
    t = next((x for x in e.typed if "(common)" not in x), None) or (e.typed[0] if e.typed else "")
    t = t.replace(" (common)", "")
    if t == "filed from this mail":
        return "Filed from the same mail as"
    if t == "same named":
        return "Names the same person or place as"
    if t:
        return t[0].upper() + t[1:] + " as"
    if (e.measured or {}).get("topic"):
        return "A similar topic to"
    return "Linked to"


def _members_phrase(ids: list[str], items: dict[str, Any]) -> str:
    kinds = {getattr(items[i], "source", None) for i in ids}
    n = len(ids)
    if len(kinds) == 1:
        one, many = _KIND_NOUN.get(next(iter(kinds)) or "", ("item", "items"))
    else:
        one, many = "item", "items"
    return f"{'a' if n == 1 else n} {one if n == 1 else many}"


def suggest(items: dict[str, Any], edges: Iterable[Any], visible: Callable[[Any], bool],
            projects: list[dict[str, Any]], role: str | None = None) -> dict[str, dict[str, Any]]:
    if role in ("kid", "household"):
        return {}
    seen = {i for i, it in items.items() if visible(it)}
    member_of: dict[str, set[str]] = defaultdict(set)
    for p in projects:
        for m in p.get("members") or ():
            member_of[m].add(p["id"])
    names = {p["id"]: str(p.get("name") or "") for p in projects}
    unsorted = {i for i in seen if not member_of.get(i)}
    # per unsorted item, per project: the links to that project's visible members
    links: dict[str, dict[str, list[Any]]] = defaultdict(lambda: defaultdict(list))
    for e in edges:
        if e.state not in RANK or e.a not in seen or e.b not in seen:   # rejected: never
            continue
        for u, m in ((e.a, e.b), (e.b, e.a)):
            if u in unsorted:
                for pid in member_of.get(m, ()):
                    links[u][pid].append((m, e))
    out: dict[str, dict[str, Any]] = {}
    for u, per in links.items():
        scored = []
        for pid, ms in per.items():
            best = max(RANK[e.state] for _m, e in ms)
            n = len({m for m, _e in ms})
            scored.append(((best, n), pid, ms))
        scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            continue                      # two projects hold it equally: no suggestion beats a wrong one
        (best, _n), pid, ms = scored[0]
        state = next(s for s, r in RANK.items() if r == best)
        top = [(m, e) for m, e in ms if e.state == state]
        via = sorted({m for m, _e in top})
        reason = f"{_lead(top[0][1])} {_members_phrase(via, items)} in {names[pid] or 'that project'}"
        if state == "candidate":
            reason = "Possibly: " + reason[0].lower() + reason[1:]
        out[u] = {"project": pid, "reason": reason, "state": state, "auto": state == "verified", "via": via}
    return out


# ── the items surface (#1084): ids are "<kind>:<store id>"; a chat is chat_links' "chat-<cid>" ──────────────────────

def _link_id(item_id: str) -> str | None:
    kind, _, sid = item_id.partition(":")
    return f"chat-{sid}" if kind == "chat" and sid else None


def for_items(view_items: list[dict[str, Any]], edges: Iterable[Any], project_names: dict[str, str],
              role: str | None) -> dict[str, dict[str, Any]]:
    """Suggestions for the viewer's own unfiled items, from items the surface already projected for them (access
    projection first: nothing here is an item they can't see). A project counts only if it's in project_names (the
    ones they may see); its members are the viewer's items filed under it."""
    from types import SimpleNamespace
    by_link: dict[str, dict[str, Any]] = {}
    for it in view_items:
        lid = _link_id(it["id"])
        if lid:
            by_link[lid] = it
    items = {lid: SimpleNamespace(item_id=lid, source=it["kind"]) for lid, it in by_link.items()}
    projects = [{"id": pid, "name": name,
                 "members": [lid for lid, it in by_link.items() if pid in (it.get("projects") or [])]}
                for pid, name in project_names.items()]
    got = suggest(items, [e for e in edges if e.a in items and e.b in items], lambda _it: True, projects, role=role)
    return {by_link[lid]["id"]: {**v, "via": [by_link[m]["id"] for m in v["via"]]} for lid, v in got.items()}


# ── titan files verified links on its own (#1087, the auto-filing half) ───────────────────────────────────────────

AUTO_SHOW_S = 86400


def _adb(db: Any = None) -> Any:
    import projects
    con = projects._db(db)
    con.execute("""CREATE TABLE IF NOT EXISTS auto_filings (person TEXT NOT NULL, item TEXT NOT NULL,
        project TEXT NOT NULL, token TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (person, item, project))""")
    return con


def chat_view(person: str, edges: Iterable[Any], db: Any = None) -> list[dict[str, Any]]:
    """The person's chats that have an edge, as items-API items with the projects they're filed under."""
    import projects
    memb = projects.membership(person, db)
    cids = {x[len("chat-"):] for e in edges for x in (e.a, e.b) if x.startswith("chat-")}
    return [{"id": f"chat:{c}", "kind": "chat", "projects": memb.get(f"chat:{c}", [])} for c in sorted(cids)]


def visible_projects(person: str, role: str | None, db: Any = None) -> dict[str, str]:
    """The projects and groups this person may file into: id → name."""
    import spaces
    out = {p["id"]: str(p.get("name") or "") for p in spaces.projects_for(person, role, db)}
    out.update({g: str(v.get("name") or "") for g, v in spaces.all_groups(db).items() if spaces.in_group(v, person, role)})
    return out


def auto_file(person: str, role: str | None, view_items: list[dict[str, Any]], edges: Iterable[Any],
              project_names: dict[str, str], *, db: Any = None, apply: bool = True,
              now: float | None = None) -> list[dict[str, Any]]:
    """File each unfiled item whose suggestion is verified, once. → [{item, project, token}] (token None on a dry run).
    Never a kid's or a shared screen's; never a pair this person already had filed for them (undone or not)."""
    import projects
    import time as _t
    if not person or role in ("kid", "household"):
        return []
    sg = for_items(view_items, list(edges), project_names, role)
    con = _adb(db)
    try:
        done = {(r[0], r[1]) for r in con.execute("SELECT item, project FROM auto_filings WHERE person=?", (person.lower(),))}
    finally:
        con.close()
    out = []
    for iid, v in sorted(sg.items()):
        if not v["auto"] or (iid, v["project"]) in done:
            continue
        if not apply:
            out.append({"item": iid, "project": v["project"], "token": None})
            continue
        try:
            r = projects.file(person, role or "adult", iid, v["project"], db)
        except projects.Refused:
            continue
        con = _adb(db)
        try:
            con.execute("INSERT OR REPLACE INTO auto_filings VALUES (?,?,?,?,?)",
                        (person.lower(), iid, v["project"], r["undo"], _t.time() if now is None else now))
            con.commit()
        finally:
            con.close()
        out.append({"item": iid, "project": v["project"], "token": r["undo"]})
    return out


def recent_auto(person: str | None, db: Any = None, now: float | None = None) -> list[dict[str, Any]]:
    """What titan filed for this person in the last AUTO_SHOW_S, still filed there and not undone."""
    import projects
    import time as _t
    if not person:
        return []
    now = _t.time() if now is None else now
    memb = projects.membership(person, db)
    con = _adb(db)
    try:
        rows = con.execute("SELECT a.item, a.project, a.token, a.at FROM auto_filings a JOIN project_undo u ON "
                           "u.token = a.token WHERE a.person=? AND a.at >= ?", (person.lower(), now - AUTO_SHOW_S)).fetchall()
    finally:
        con.close()
    return [{"item": r[0], "project": r[1], "token": r[2], "at": r[3]} for r in rows if r[1] in memb.get(r[0], [])]


def run_all(apply: bool = False, con: Any = None, db: Any = None) -> dict[str, int]:
    """Every adult's verified chat links → filings (counts only)."""
    import chat_links
    import hooks
    people = [p for p in chat_links._people() if p.get("role") in ("admin", "adult")]
    con = con or hooks.connect()
    chat_links.ensure(con)
    filed = 0
    for p in people:
        pid = str(p["id"])
        edges = [chat_links.Edge(a, b, t, st, o, json.loads(ty), json.loads(me), w) for a, b, t, st, o, ty, me, w in
                 con.execute("SELECT a, b, type, state, owner, typed, measured, why FROM chat_links WHERE owner=?", (pid,))]
        filed += len(auto_file(pid, p.get("role"), chat_view(pid, edges, db), edges, visible_projects(pid, p.get("role"), db),
                               db=db, apply=apply))
    return {"people": len(people), ("filed" if apply else "would_file"): filed}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="titan files verified chat links on its own (#1087); counts only")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(run_all(apply=a.apply)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
