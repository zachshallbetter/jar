#!/usr/bin/env python3
"""Projects are threads that earned a name (#1085, epic #1088; threads from #951).

A project is a thread that holds together, or one a person made. titan **proposes**, never creates silently:
  proposals(person, role)        the viewer's threads (linker.threads_for over the items they may see) of at least
                                 MIN_ITEMS items that hold together, not already a project, filed, or dismissed
  keep(proposal) / dismiss(...)  keep makes the project (name earned by the thread, else its machine description;
                                 private by default; suggested_audience offered: the other adults its items
                                 involve) and files its items; sharing a titan-named project renames it from what every
                                 audience member can see; a dismissed cluster stays dismissed as it grows (overlap ≥
                                 SAME_CLUSTER)
  file / unfile / split / merge  each returns one undo token; undo(token) puts back exactly what it changed
Membership lives in `filings` (item → project, one project per item); a chat's filing stays in spaces' tags, as the
Mac files it today. Items keep their own audience inside a project: a project page shows an item only to a viewer
who may see it (items.py's readers), so an adult's own texts in a household project stay theirs.

Groups (the Mac's Spaces) are projects too: `project:<group id>` resolves for the group's members. /v1/projects and
/v1/groups keep working as before.

Store: the spaces DB ($SPACES_DB).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

import spaces

MIN_ITEMS = 3
SAME_CLUSTER = 0.5          # a thread this much like a kept or dismissed one is that one
UNDO_KEEP_S = 7 * 86400


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


NAME_WORDS, NAME_CHARS = 6, 48     # a titan-made name (#1497): a few words, never a mail subject's whole line
_TAGS_RX = re.compile(r"^(?:\[[^\]]{1,24}\]\s*)+")
_shortened: set[str] = set()
_FILLER = frozenset("a an and as at by for from in of on or re the to with".split())


def short_name(name: Any) -> str:
    """A name titan made, short (#1497: Zach's project page carried a whole "Re: …" subject): Re:/Fwd: and leading
    [tags] gone, at most NAME_WORDS words and NAME_CHARS characters, cut at a word, no trailing punctuation. A name
    a person gave never comes here."""
    import items
    raw = " ".join(str(name or "").split())
    t, prev = raw, None
    while t != prev:                     # "[EXT] Re: [x] Fwd: …": tags and reply prefixes in any order
        prev, t = t, _TAGS_RX.sub("", items.mail_subject(t)).strip()
    words = t.split()[:NAME_WORDS]
    while len(" ".join(words)) > NAME_CHARS and len(words) > 1:
        words.pop()
    while len(words) > 1 and (words[-1].lower().strip(",.;:") in _FILLER or not words[-1].strip("-\u2013\u2014:;,.")):
        words.pop()                      # never ends on "and", "for", a dash
    t = " ".join(words)[:NAME_CHARS].rstrip(" -\u2013\u2014:;,.")
    return t if t and t != "(no subject)" else (raw[:NAME_CHARS] or raw)


def _shorten_stored(con: sqlite3.Connection) -> None:
    """Names titan made before #1497, shortened once: only earned or description names (a person's stays as typed)."""
    for pid, name in con.execute("SELECT id, name FROM projects WHERE name_source IN (?, ?)", TITAN_NAMED).fetchall():
        short = short_name(name)
        if short and short != name:
            con.execute("UPDATE projects SET name=? WHERE id=? AND name_source IN (?, ?)", (short, pid, *TITAN_NAMED))
    con.commit()


def _db(db: Path | None = None) -> sqlite3.Connection:
    con = spaces._db(db)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS filings (item TEXT PRIMARY KEY, project TEXT NOT NULL, by TEXT NOT NULL,
            at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_filings_project ON filings(project);
        CREATE TABLE IF NOT EXISTS proposal_marks (person TEXT NOT NULL, cluster TEXT NOT NULL, state TEXT NOT NULL,
            project TEXT, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS project_undo (token TEXT PRIMARY KEY, person TEXT NOT NULL, ops TEXT NOT NULL,
            at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS related_marks (project TEXT NOT NULL, thread TEXT NOT NULL, by TEXT NOT NULL,
            at REAL NOT NULL, PRIMARY KEY (project, thread));
    """)
    cols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
    for col, decl in (("origin", "TEXT"), ("name_source", "TEXT")):
        if col not in cols:
            try:
                con.execute(f"ALTER TABLE projects ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
    key = str(db or "")
    if key not in _shortened:
        _shortened.add(key)
        try:
            _shorten_stored(con)
        except sqlite3.Error:
            _shortened.discard(key)
    return con


def _overlap(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else 0.0


def filings(db: Path | None = None) -> dict[str, str]:
    con = _db(db)
    try:
        return {r["item"]: r["project"] for r in con.execute("SELECT item, project FROM filings")}
    finally:
        con.close()


def chat_key(item: str) -> str | None:
    """The conversation id of a chat item, in either spelling (items-API "chat:<cid>", linker "chat-<cid>")."""
    for p in ("chat:", "chat-"):
        if item.startswith(p):
            return item[len(p):]
    return None


# ── proposals ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _threads(person: str, role: str) -> tuple[list[Any], dict[str, Any], Callable[[Any], bool]]:
    import hooks
    import linker
    import threadview
    items, edges = linker.scored(hooks.connect())
    if not items:
        return [], {}, lambda it: False
    visible = threadview._visible_fn(person, role)
    titles, vendors, names = linker.labels(items)
    return linker.threads_for(items, edges, visible, titles, names, vendors), items, visible


def proposals(person: str | None, role: str | None, *, threads: Callable[[], tuple] | None = None,
              db: Path | None = None) -> list[dict[str, Any]]:
    """The viewer's proposals, built only from items they may see (the threads are theirs, #951 rule 8)."""
    if not person or role in ("kid", "household"):
        return []
    ths, items, _vis = (threads or (lambda: _threads(person, role)))()
    import threadview
    con = _db(db)
    try:
        marks = [(json.loads(r["cluster"]), r["state"]) for r in
                 con.execute("SELECT cluster, state FROM proposal_marks WHERE person=?", (person.lower(),))]
        filed = {r["item"] for r in con.execute("SELECT item FROM filings")}
        tagged = {f"chat-{r['conversation']}" for r in con.execute("SELECT conversation FROM tags WHERE person=? AND "
                                                                    "(project IS NOT NULL OR grp IS NOT NULL)",
                                                                    (person.lower(),))}
    finally:
        con.close()
    out = []
    for t in ths:
        members = list(t.items)
        if len(members) < MIN_ITEMS or any(_overlap(members, c) >= SAME_CLUSTER for c, _ in marks):
            continue
        if len(set(members) & (filed | tagged)) * 2 >= len(members):
            continue                      # mostly filed already: it has a home
        out.append({"id": threadview.thread_id(members), "name": short_name(t.name or t.description),
                    "name_source": "earned" if t.name else "description", "count": len(members),
                    "items": members, "cohesion": t.cohesion,
                    "reason": {"key": "projects.proposal.holds_together", "count": len(members)}})
    return out


def _find(person: str, role: str, pid: str, threads: Callable[[], tuple] | None, db: Path | None) -> dict[str, Any]:
    p = next((x for x in proposals(person, role, threads=threads, db=db) if x["id"] == pid), None)
    if not p:
        raise Refused(404, "no such proposal")
    return p


def _mark(person: str, cluster: list[str], state: str, project: str | None, db: Path | None) -> None:
    con = _db(db)
    try:
        con.execute("INSERT INTO proposal_marks VALUES (?,?,?,?,?)",
                    (person.lower(), json.dumps(sorted(cluster)), state, project, time.time()))
        con.commit()
    finally:
        con.close()


def dismiss(person: str, role: str, pid: str, *, threads: Callable[[], tuple] | None = None,
            db: Path | None = None) -> dict[str, Any]:
    p = _find(person, role, pid, threads, db)
    _mark(person, p["items"], "dismissed", None, db)
    return {"dismissed": pid}


def suggested_audience(person: str, owners: Iterable[str], people: list[dict[str, Any]]) -> list[str]:
    """Who the app may offer to share with in one tap ("Share with Robin?"): the other adults its items involve.
    A suggestion only: a kept project is private until the keeper shares it (sharing is explicit, Coordination 10-06)."""
    adults = {str(p["id"]).lower() for p in people if p.get("role") in ("admin", "adult")}
    return sorted({str(o).lower() for o in owners if o} & adults - {person.lower()})


def _op(oid: str, verb: str, kind: str, sid: str, contract: str, method: str, path: str,
        params: dict[str, Any] | None = None, *, recovery: bool = False, variant: str | None = None) -> dict[str, Any]:
    op = {"id": oid, "verb": verb, "subject": {"kind": kind, "id": sid}, "exposure": "available",
          "contract": contract, "route": {"method": method, "path": path}, "verification": "none",
          "reversibility": "reversible"}
    if params:
        op["params"] = params
    if recovery:
        op["recovery"] = ["undo"]
    if variant:
        op["variant"] = variant
    return op


def proposal_ops(prop: dict[str, Any], person: str | None, role: str | None) -> list[dict[str, Any]]:
    """#1177: what this viewer may do with a proposal (jb-1 operations): Keep, Not a project. Only an adult or the
    admin gets proposals at all; anyone else, none."""
    if not person or role not in ("admin", "adult"):
        return []
    pid = prop["id"]
    return [_op(f"op-keep-{pid}", "add", "proposal", pid, "jellybean.project.keep@1", "POST",
                f"/v1/projects/proposals/{pid}/keep", recovery=True, variant="primary"),
            _op(f"op-dismiss-{pid}", "remove", "proposal", pid, "jellybean.project.dismiss@1", "POST",
                f"/v1/projects/proposals/{pid}/dismiss", variant="quiet")]


def manage_ops(project: dict[str, Any] | None, person: str | None, role: str | None,
               suggested: list[str] | None = None, undo: str | None = None) -> list[dict[str, Any]]:
    """#1177: what this viewer may do to a kept project (jb-1 operations): Rename and Share for its owner or the admin
    (spaces.update_project's rule), Share offering only the suggested people; Undo with the keep's own token. A
    group, a kid, a shared screen or anyone else: none (the routes refuse them anyway)."""
    if not project or not person or role not in ("admin", "adult"):
        return []
    pid = str(project["id"])
    ops: list[dict[str, Any]] = []
    if str(project.get("owner") or "").lower() == str(person).lower() or role == "admin":
        ops.append(_op(f"op-rename-{pid}", "edit", "project", pid, "jellybean.project.rename@1", "PATCH",
                       f"/v1/projects/{pid}"))
        if suggested:
            ops.append(_op(f"op-share-{pid}", "share", "project", pid, "jellybean.project.share@1", "PATCH",
                           f"/v1/projects/{pid}", {"audience": list(suggested)}))
    if undo:
        ops.append(_op(f"op-undo-{undo}", "undo", "project", pid, "jellybean.project.undo@1", "POST",
                       "/v1/projects/undo", {"token": undo}, variant="quiet"))
    return ops


TITAN_NAMED = ("earned", "description")
NEUTRAL_NAME = "Shared project"


def _members_of(audience: Any, owner: str, people: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(person, role) for everyone a project's audience admits."""
    import audience as aud
    a = json.dumps(audience) if isinstance(audience, list) else str(audience or "private")
    return [(str(p["id"]).lower(), str(p.get("role") or "")) for p in people if p.get("role") != "household"
            and aud.visible(a, str(p["id"]).lower(), p.get("role"), owner=owner)]


def name_for_audience(item_ids: list[str], members: list[tuple[str, str]],
                      namer: Callable[[list[str]], str | None] | None = None,
                      loader: Callable[[], tuple] | None = None) -> tuple[str, str]:
    """A titan-made name built only from items every member may see; else the neutral NEUTRAL_NAME (never one built
    from anyone's private items). → (name, name_source)."""
    import linker
    import threadview
    if loader is None:
        def loader():
            import hooks
            return linker.scored(hooks.connect())
    items, edges = loader()
    common = [i for i in item_ids if i in items and members and
              all(threadview._visible_fn(m, r)(items[i]) for m, r in members)]
    if len(common) >= 2:
        if namer is None:
            titles, vendors, names = linker.labels(items)
            ths = linker.threads_for({i: items[i] for i in common}, edges, lambda it: True, titles, names, vendors)
            best = max(ths, key=lambda t: len(t.items), default=None)
            got = best.name if best else None
        else:
            got = namer(common)
        if got:
            return short_name(got), "earned"
    return NEUTRAL_NAME, "description"


def reshare(pid: str, people: list[dict[str, Any]], db: Path | None = None, **kw: Any) -> dict[str, Any]:
    """A project's audience changed: if titan named it, name it again from what everyone it's shared with can see.
    A name a person gave stays as typed (they chose to share it)."""
    p = get(pid, db)
    if not p or p.get("name_source") not in TITAN_NAMED or p.get("audience") == "private":
        return p
    con = _db(db)
    try:
        ids = [r["item"] for r in con.execute("SELECT item FROM filings WHERE project=?", (pid,))]
    finally:
        con.close()
    name, src = name_for_audience(ids, _members_of(p["audience"], p["owner"], people), **kw)
    con = _db(db)
    try:
        con.execute("UPDATE projects SET name=?, name_source=? WHERE id=?", (name, src, pid))
        con.commit()
    finally:
        con.close()
    return get(pid, db)


def keep(person: str, role: str, pid: str, *, name: Any = None, audience: Any = None,
         threads: Callable[[], tuple] | None = None, people: list[dict[str, Any]] | None = None,
         db: Path | None = None, rename_kw: dict[str, Any] | None = None) -> dict[str, Any]:
    ths = threads or (lambda: _threads(person, role))
    _ths, items, _vis = ths()
    p = _find(person, role, pid, lambda: (_ths, items, _vis), db)
    owners = [getattr(items.get(i), "owner", None) for i in p["items"]]
    aud = audience if audience is not None else "private"
    proj = spaces.create_project(person, role, name or p["name"], audience=aud, db=db)
    con = _db(db)
    try:
        con.execute("UPDATE projects SET origin='proposal', name_source=? WHERE id=?",
                    ("person" if name else p["name_source"], proj["id"]))
        con.commit()
    finally:
        con.close()
    token = _file_many(person, role, p["items"], proj["id"], db, check=False)
    _mark(person, p["items"], "kept", proj["id"], db)
    if not name and get(proj["id"], db).get("audience") != "private":     # shared at keep: a name all of them can see
        reshare(proj["id"], people or [], db, **(rename_kw or {}))
    sug = suggested_audience(person, owners, people or [])
    names = {str(x["id"]).lower(): str(x.get("name") or "").split(" ")[0] for x in (people or [])}
    # each suggested person with the name to show (privacy review, 10-07: the iPhone resolved names from the viewer's
    # own groups, missed some, and fell back to "Share as titan suggests" with unnamed people in it)
    named = [{"id": i, "name": names.get(i) or ""} for i in sug if names.get(i)]
    kept = get(proj["id"], db)
    return {**kept, "undo": token, "suggested_audience": sug, "suggested_people": named,
            # #1177: what this keeper may do now; Share offers exactly the people named above
            "operations": manage_ops(kept, person, role, [x["id"] for x in named], token)}


# ── membership, with one undo token per change ───────────────────────────────────────────────────────────────────────

def get(pid: str, db: Path | None = None) -> dict[str, Any]:
    p = spaces.get_project(pid, db) or {}
    if not p:
        return {}
    con = _db(db)
    try:
        r = con.execute("SELECT origin, name_source FROM projects WHERE id=?", (pid,)).fetchone()
        n = con.execute("SELECT COUNT(*) FROM filings WHERE project=?", (pid,)).fetchone()[0]
    finally:
        con.close()
    return {**p, "origin": (r["origin"] if r else None) or "person", "name_source": (r["name_source"] if r else None)
            or "person", "filed": n}


def named_by_person(pid: str, db: Path | None = None) -> None:
    con = _db(db)
    try:
        con.execute("UPDATE projects SET name_source='person' WHERE id=?", (pid,))
        con.commit()
    finally:
        con.close()


def _target_ok(person: str, role: str, project: str, db: Path | None) -> None:
    try:
        if project in spaces.all_groups(db):     # groups are projects too
            spaces.check_target(person, role, group=project, db=db)
        else:
            spaces.check_target(person, role, project=project, db=db)
    except spaces.SpaceError as exc:
        raise Refused(exc.status if exc.status != 403 else 404, "no such project") from None


def _state_of(con: sqlite3.Connection, person: str, item: str) -> dict[str, Any]:
    cid = chat_key(item)
    if cid:
        r = con.execute("SELECT project, grp FROM tags WHERE person=? AND conversation=?", (person.lower(), cid)).fetchone()
        return {"item": item, "chat": cid, "project": r["project"] if r else None, "grp": r["grp"] if r else None}
    r = con.execute("SELECT project, by FROM filings WHERE item=?", (item,)).fetchone()
    return {"item": item, "project": r["project"] if r else None, "by": r["by"] if r else None}


def _apply(con: sqlite3.Connection, person: str, st: dict[str, Any]) -> None:
    if st.get("chat"):
        if st.get("project") or st.get("grp"):
            con.execute("INSERT INTO tags (person, conversation, project, grp, at) VALUES (?,?,?,?,?) ON CONFLICT(person, "
                        "conversation) DO UPDATE SET project=excluded.project, grp=excluded.grp, at=excluded.at",
                        (person.lower(), st["chat"], st.get("project"), st.get("grp"), time.time()))
        else:
            con.execute("DELETE FROM tags WHERE person=? AND conversation=?", (person.lower(), st["chat"]))
    elif st.get("project"):
        con.execute("INSERT OR REPLACE INTO filings VALUES (?,?,?,?)",
                    (st["item"], st["project"], st.get("by") or person.lower(), time.time()))
    else:
        con.execute("DELETE FROM filings WHERE item=?", (st["item"],))


def _change(person: str, changes: list[tuple[str, str | None]], db: Path | None,
            extra: list[dict[str, Any]] | None = None) -> str:
    """Set each item's project (None: unfiled), remembering what was there; → the undo token."""
    token = "pu-" + uuid.uuid4().hex[:16]
    with spaces._lock:
        con = _db(db)
        try:
            before = [_state_of(con, person, i) for i, _ in changes]
            groups = set(spaces.all_groups(db))
            for (i, proj), was in zip(changes, before):
                if not was.get("chat"):
                    _apply(con, person, {**was, "project": proj, "by": person.lower()})
                elif proj in groups:      # a chat filed under a group: its group tag, as the Mac files it
                    _apply(con, person, {**was, "project": None, "grp": proj})
                else:
                    _apply(con, person, {**was, "project": proj, "grp": None})
            con.execute("INSERT INTO project_undo VALUES (?,?,?,?)",
                        (token, person.lower(), json.dumps({"items": before, "extra": extra or []}), time.time()))
            con.execute("DELETE FROM project_undo WHERE at < ?", (time.time() - UNDO_KEEP_S,))
            con.commit()
        finally:
            con.close()
    return token


def _file_many(person: str, role: str, items: list[str], project: str, db: Path | None, check: bool = True) -> str:
    if check:
        _target_ok(person, role, project, db)
    return _change(person, [(i, project) for i in items], db)


def file(person: str, role: str, item: str, project: str, db: Path | None = None) -> dict[str, Any]:
    if not person or role == "household":
        raise Refused(403, "a shared screen files nothing")
    _target_ok(person, role, project, db)
    return {"item": item, "project": project, "undo": _change(person, [(item, project)], db)}


def unfile(person: str, role: str, item: str, db: Path | None = None) -> dict[str, Any]:
    if not person or role == "household":
        raise Refused(403, "a shared screen files nothing")
    con = _db(db)
    try:
        was = _state_of(con, person, item)
    finally:
        con.close()
    if was.get("project"):
        _target_ok(person, role, was["project"], db)
    return {"item": item, "project": None, "undo": _change(person, [(item, None)], db)}


def _owned(person: str, role: str, project: str, db: Path | None) -> dict[str, Any]:
    p = spaces.get_project(project, db)
    if not p or not spaces.project_visible(p, person, role, spaces.all_groups(db)):
        raise Refused(404, "no such project")
    if p["owner"] != person.lower() and role != "admin":
        raise Refused(403, "only its owner can change a project")
    return p


def split(person: str, role: str, project: str, items: list[str], name: Any, db: Path | None = None) -> dict[str, Any]:
    """Move these of its items into a new project (same owner and audience); one undo token for all of it."""
    p = _owned(person, role, project, db)
    con = _db(db)
    try:
        mine = [i for i in items if _state_of(con, person, i).get("project") == project]
    finally:
        con.close()
    if not mine:
        raise Refused(400, "none of those items are in that project")
    new = spaces.create_project(person, role, name, audience=p["audience"], group=p.get("group"), db=db)
    token = _change(person, [(i, new["id"]) for i in mine], db, extra=[{"archive": new["id"]}])
    return {"project": get(new["id"], db), "moved": len(mine), "undo": token}


def merge(person: str, role: str, project: str, into: str, db: Path | None = None) -> dict[str, Any]:
    _owned(person, role, project, db)
    _owned(person, role, into, db)
    con = _db(db)
    try:
        items = [r["item"] for r in con.execute("SELECT item FROM filings WHERE project=?", (project,))]
        items += [f"chat:{r['conversation']}" for r in con.execute("SELECT conversation FROM tags WHERE person=? AND "
                                                                    "project=?", (person.lower(), project))]
    finally:
        con.close()
    spaces.update_project(project, person, role, db=db, archived=True)
    token = _change(person, [(i, into) for i in items], db, extra=[{"unarchive": project}])
    return {"project": get(into, db), "moved": len(items), "undo": token}


def undo(person: str, token: str, db: Path | None = None) -> dict[str, Any]:
    with spaces._lock:
        con = _db(db)
        try:
            r = con.execute("SELECT ops FROM project_undo WHERE token=? AND person=?", (token, person.lower())).fetchone()
            if not r:
                raise Refused(404, "nothing to undo")
            ops = json.loads(r["ops"])
            for st in ops["items"]:
                _apply(con, person, st)
            for x in ops["extra"]:
                if x.get("archive"):
                    con.execute("UPDATE projects SET archived_at=? WHERE id=?", (time.time(), x["archive"]))
                if x.get("unarchive"):
                    con.execute("UPDATE projects SET archived_at=NULL WHERE id=?", (x["unarchive"],))
            con.execute("DELETE FROM project_undo WHERE token=?", (token,))
            con.commit()
        finally:
            con.close()
    return {"undone": token, "items": len(ops["items"])}


def membership(person: str | None, db: Path | None = None) -> dict[str, list[str]]:
    """item id (items-API spelling) → [project or group ids] this viewer's view files it under: everyone's filings
    (an item still shows only to who may see it) and the viewer's own chat tags."""
    out: dict[str, list[str]] = {}
    con = _db(db)
    try:
        for r in con.execute("SELECT item, project FROM filings"):
            k = f"chat:{chat_key(r['item'])}" if chat_key(r["item"]) else r["item"]
            out.setdefault(k, []).append(r["project"])
        if person:
            for r in con.execute("SELECT conversation, project, grp FROM tags WHERE person=?", (person.lower(),)):
                out.setdefault(f"chat:{r['conversation']}", []).extend(x for x in (r["project"], r["grp"]) if x)
    finally:
        con.close()
    return out


# ── related threads on a project page (#1109) ─────────────────────────────────────────────────────────────────────────

MAX_RELATED = 3
_STATE_RANK = {"verified": 0, "accepted": 1, "candidate": 2}


def _linker_view(person: str, role: str) -> tuple[dict, list, Callable[[Any], bool], tuple]:
    import hooks
    import linker
    import threadview
    items, edges = linker.scored(hooks.connect())
    return items, edges, threadview._visible_fn(person, role), linker.labels(items) if items else ({}, {}, {})


def related(project: str, person: str | None, role: str | None, *, view: Callable[[], tuple] | None = None,
            db: Path | None = None) -> list[dict[str, Any]]:
    """Threads #951's linker scores against this project, as this viewer sees them: at most MAX_RELATED, each
    {thread, name, count, why, state}. One is offered only when the viewer can see every item behind it: the thread
    is theirs (threads_for over what they may see) and the edge that relates it joins two items they can see, so its
    why cites only those. Kids and shared screens: none."""
    if not person or role in ("kid", "household"):
        return []
    import linker
    import threadview
    items, edges, visible, (titles, vendors, names) = (view or (lambda: _linker_view(person, role)))()
    if not items:
        return []
    seen = {i for i, it in items.items() if visible(it)}
    con = _db(db)
    try:
        here = {r["item"] for r in con.execute("SELECT item FROM filings WHERE project=?", (project,))}
        here |= {f"chat-{r['conversation']}" for r in con.execute("SELECT conversation FROM tags WHERE person=? AND "
                                                                   "project=?", (person.lower(), project))}
        said_no = {r["thread"] for r in con.execute("SELECT thread FROM related_marks WHERE project=?", (project,))}
    finally:
        con.close()
    members = here & seen
    if not members:
        return []
    out = []
    # threads among what the viewer sees outside the project (an accepted edge into the project would otherwise
    # make the thread part of the project's own component)
    for t in linker.threads_for(items, edges, lambda it: visible(it) and it.item_id not in here, titles, names, vendors):
        tid = threadview.thread_id(t.items)
        if tid in said_no or not set(t.items) <= seen:
            continue
        ti = set(t.items)
        links = [e for e in edges if e.state in _STATE_RANK and e.a in seen and e.b in seen
                 and ((e.a in members and e.b in ti) or (e.b in members and e.a in ti))]
        if not links:
            continue
        best = min(links, key=lambda e: (_STATE_RANK[e.state], -linker.strength(e), e.a, e.b))
        out.append({"thread": tid, "name": t.name or t.description, "count": len(t.items), "why": best.why,
                    "state": best.state, "items": list(t.items)})
    out.sort(key=lambda r: (_STATE_RANK[r["state"]], -r["count"], r["thread"]))
    return out[:MAX_RELATED]


def relate(project: str, thread: str, person: str, role: str, link: bool, *, view: Callable[[], tuple] | None = None,
           db: Path | None = None) -> dict[str, Any]:
    """Link: file the thread's items under the project (one undo token). Not related: it stays off this project's
    page. Only a thread this viewer is offered."""
    _target_ok(person, role, project, db)
    hit = next((r for r in related(project, person, role, view=view, db=db) if r["thread"] == thread), None)
    if not hit:
        raise Refused(404, "no such related thread")
    if link:
        return {"linked": thread, "undo": _file_many(person, role, hit["items"], project, db, check=False)}
    con = _db(db)
    try:
        con.execute("INSERT OR REPLACE INTO related_marks VALUES (?,?,?,?)", (project, thread, person.lower(), time.time()))
        con.commit()
    finally:
        con.close()
    return {"not_related": thread}
