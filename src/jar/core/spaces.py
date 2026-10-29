#!/usr/bin/env python3
"""Projects and groups (jellybean#240, #453; design: the handoff's WorkspaceHome.dc.html). Zach, 10-02:
  - a **project** is private to the person who made it until they share it ("Private by default");
  - a **group** is a home with its own page: its chats, projects, calendar, to-dos and people ("A home with its own
    page"), seen by its members and nobody else;
  - seeds: Titan server, Home Projects, Jellybean (projects); Household and Parents (groups), School year in Parents.

  project  {id: "pj_<8 hex>", name, icon, color, owner, audience, group, created_at, archived}
           audience is audience.py's (private · household · adults · admins · [person ids]); private by default.
           A project in a group is also seen by the group's members.
  group    {id: "gr_<8 hex>", name, icon, color, members, created_by, created_at}
           members is an audience too: "household" (everyone), "adults", or [person ids] ("Parents": Zach, Robin).
  tag      (person, conversation id) → project and/or group: the person's own conversation filed under them
           (GET /v1/conversations shows it; PATCH moves it; "New chat here" tags a new thread on its first turn).

Only the person's own conversations are tagged here: a chat filed under a group isn't shown to its other members
yet (that's the next slice: shared group chats, with the person's say).

Store: SQLite at $SPACES_DB (default /ai/datasets/connectors/spaces.db).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import audience as _aud

DB_PATH = Path(os.environ.get("SPACES_DB", "/ai/datasets/connectors/spaces.db"))
ICONS = ("folder", "server", "house", "bean", "book", "briefcase", "heart", "star", "leaf", "wrench", "school", "users")
COLORS = ("red", "orange", "amber", "green", "teal", "blue", "indigo", "violet", "pink", "slate")
ID_RX = re.compile(r"^(?:pj|gr)_[0-9a-f]{8}$")
_lock = threading.Lock()


class SpaceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _path(db: Path | None = None) -> Path:
    return Path(db or os.environ.get("SPACES_DB") or DB_PATH)


def _db(db: Path | None = None) -> sqlite3.Connection:
    p = _path(db)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT, color TEXT,
            owner TEXT NOT NULL, audience TEXT NOT NULL DEFAULT 'private', grp TEXT, created_at REAL NOT NULL,
            archived_at REAL);
        CREATE TABLE IF NOT EXISTS groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT, color TEXT,
            members TEXT NOT NULL, created_by TEXT NOT NULL, created_at REAL NOT NULL, archived_at REAL);
        CREATE TABLE IF NOT EXISTS tags (person TEXT NOT NULL, conversation TEXT NOT NULL, project TEXT, grp TEXT,
            at REAL NOT NULL, PRIMARY KEY (person, conversation));
            CREATE TABLE IF NOT EXISTS lineage (person TEXT NOT NULL, conversation TEXT NOT NULL, parent TEXT NOT NULL,
            upto TEXT, at REAL NOT NULL, PRIMARY KEY (person, conversation));
    """)
    if "calendar" not in [r[1] for r in con.execute("PRAGMA table_info(groups)")]:
        try:   # the calendar a group's page shows: a title the Mac reads with EventKit (titan never guesses it)
            con.execute("ALTER TABLE groups ADD COLUMN calendar TEXT")
        except sqlite3.OperationalError:
            pass
    return con


def _name(v: Any) -> str:
    n = " ".join(str(v or "").split())[:60]
    if not n:
        raise SpaceError(400, "a name is required")
    return n


def _pick(v: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(v or "").strip().lower()
    return v if v in allowed else default


def _project(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "icon": r["icon"], "color": r["color"], "owner": r["owner"],
            "audience": _aud_out(r["audience"]), "group": r["grp"], "created_at": r["created_at"],
            # who named it (#1142, projects.py adds the column): person, or titan (earned, description)
            "name_source": (r["name_source"] if "name_source" in r.keys() else None) or "person"}


def _group(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "icon": r["icon"], "color": r["color"],
            "members": _aud_out(r["members"]), "created_by": r["created_by"], "created_at": r["created_at"],
            "calendar": r["calendar"] if "calendar" in r.keys() else None}


def _aud_out(a: str) -> Any:
    try:
        return json.loads(a) if str(a).startswith("[") else a
    except ValueError:
        return a


# ── who sees what ──────────────────────────────────────────────────────────────────────────────

def in_group(g: dict[str, Any], person: str | None, role: str | None) -> bool:
    """A group's members: its audience. Not its creator by right (a parent who made Parents is in it by listing)."""
    if not person or role == "household":
        return False
    m = g["members"]
    return _aud.visible(json.dumps(m) if isinstance(m, list) else m, person, role, owner=None)


def project_visible(p: dict[str, Any], person: str | None, role: str | None, groups: dict[str, dict] | None = None) -> bool:
    if not person or role == "household":
        return False
    a = p["audience"]
    if _aud.visible(json.dumps(a) if isinstance(a, list) else a, person, role, owner=p["owner"]):
        return True
    g = (groups or {}).get(p.get("group") or "")
    return bool(g) and in_group(g, person, role)


# ── groups ─────────────────────────────────────────────────────────────────────────────────────

def groups_for(person: str | None, role: str | None, db: Path | None = None) -> list[dict[str, Any]]:
    con = _db(db)
    try:
        rows = [_group(r) for r in con.execute("SELECT * FROM groups WHERE archived_at IS NULL ORDER BY created_at")]
    finally:
        con.close()
    return [g for g in rows if in_group(g, person, role)]


def all_groups(db: Path | None = None) -> dict[str, dict[str, Any]]:
    con = _db(db)
    try:
        return {r["id"]: _group(r) for r in con.execute("SELECT * FROM groups WHERE archived_at IS NULL")}
    finally:
        con.close()


def create_group(by: str, role: str | None, name: Any, members: Any, icon: Any = None, color: Any = None,
                 db: Path | None = None) -> dict[str, Any]:
    """An adult (or the admin) makes a group; they're added to its members if they listed only others."""
    if role not in ("admin", "adult"):
        raise SpaceError(403, "only an adult can make a group")
    m = _aud.normalize(members, default="")
    if not m:
        raise SpaceError(400, "say who's in it (household, adults, or people)")
    if m.startswith("["):
        ids = json.loads(m)
        if by.lower() not in ids:
            ids.append(by.lower())
        m = json.dumps(sorted(ids))
    g = {"id": "gr_" + uuid.uuid4().hex[:8], "name": _name(name), "icon": _pick(icon, ICONS, "users"),
         "color": _pick(color, COLORS, "violet"), "members": m, "created_by": by.lower(), "created_at": time.time()}
    with _lock:
        con = _db(db)
        try:
            if con.execute("SELECT 1 FROM groups WHERE lower(name)=lower(?) AND archived_at IS NULL", (g["name"],)).fetchone():
                raise SpaceError(409, f"there's already a group called {g['name']}")
            con.execute("INSERT INTO groups (id, name, icon, color, members, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
                        tuple(g.values()))
            con.commit()
        finally:
            con.close()
    return {**g, "members": _aud_out(m)}


def update_group(gid: str, by: str, role: str | None, db: Path | None = None, **changes: Any) -> dict[str, Any]:
    """A member who's an adult may rename it or change who's in it; the admin may always."""
    g = all_groups(db).get(gid)
    if not g or not (in_group(g, by, role) or role == "admin"):
        raise SpaceError(404, "no such group")
    if role not in ("admin", "adult"):
        raise SpaceError(403, "only an adult can change a group")
    sets: dict[str, Any] = {}
    if "name" in changes:
        sets["name"] = _name(changes["name"])
    if "icon" in changes:
        sets["icon"] = _pick(changes["icon"], ICONS, g["icon"])
    if "color" in changes:
        sets["color"] = _pick(changes["color"], COLORS, g["color"])
    if "calendar" in changes:
        sets["calendar"] = " ".join(str(changes["calendar"] or "").split())[:80] or None
    if "members" in changes:
        m = _aud.normalize(changes["members"], default="")
        if not m:
            raise SpaceError(400, "a group needs members")
        sets["members"] = m
    if sets:
        with _lock:
            con = _db(db)
            try:
                con.execute(f"UPDATE groups SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), gid))
                con.commit()
            finally:
                con.close()
    return all_groups(db)[gid]


# ── projects ───────────────────────────────────────────────────────────────────────────────────

def projects_for(person: str | None, role: str | None, db: Path | None = None) -> list[dict[str, Any]]:
    groups = all_groups(db)
    con = _db(db)
    try:
        rows = [_project(r) for r in con.execute("SELECT * FROM projects WHERE archived_at IS NULL ORDER BY created_at")]
    finally:
        con.close()
    return [p for p in rows if project_visible(p, person, role, groups)]


def archived_projects(person: str, db: Path | None = None) -> list[dict[str, Any]]:
    """The projects this person owns and archived (#1646), newest first: theirs to bring back."""
    con = _db(db)
    try:
        return [_project(r) for r in con.execute("SELECT * FROM projects WHERE owner=? AND archived_at IS NOT NULL "
                                                 "ORDER BY archived_at DESC", (person.lower(),))]
    finally:
        con.close()


def get_project(pid: str, db: Path | None = None, include_archived: bool = False) -> dict[str, Any] | None:
    con = _db(db)
    try:
        r = con.execute("SELECT * FROM projects WHERE id=?" + ("" if include_archived else " AND archived_at IS NULL"),
                        (pid,)).fetchone()
    finally:
        con.close()
    return _project(r) if r else None


def create_project(by: str, role: str | None, name: Any, icon: Any = None, color: Any = None, audience: Any = None,
                   group: Any = None, db: Path | None = None) -> dict[str, Any]:
    """Anyone with their own token makes a project; it's private unless they say otherwise (Zach, 10-02). In a group,
    they must be a member of it."""
    if not by or role == "household":
        raise SpaceError(403, "a shared device has no projects")
    gid = str(group or "").strip() or None
    if gid:
        g = all_groups(db).get(gid)
        if not g or not in_group(g, by, role):
            raise SpaceError(403, "you're not in that group")
    p = {"id": "pj_" + uuid.uuid4().hex[:8], "name": _name(name), "icon": _pick(icon, ICONS, "folder"),
         "color": _pick(color, COLORS, "blue"), "owner": by.lower(),
         "audience": _aud.normalize(audience, default="private"), "grp": gid, "created_at": time.time()}
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT INTO projects (id, name, icon, color, owner, audience, grp, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", tuple(p.values()))
            con.commit()
        finally:
            con.close()
    return get_project(p["id"], db) or {}


def update_project(pid: str, by: str, role: str | None, db: Path | None = None, **changes: Any) -> dict[str, Any]:
    """Its owner changes it (the admin may too). 404 for anyone who can't see it. archived False brings an archived one
    back (#1646: archiving is undone like any other organizing)."""
    unarchive = "archived" in changes and not changes["archived"]
    p = get_project(pid, db, include_archived=unarchive)
    if not p or not project_visible(p, by, role, all_groups(db)):
        raise SpaceError(404, "no such project")
    if p["owner"] != by.lower() and role != "admin":
        raise SpaceError(403, "only its owner can change a project")
    sets: dict[str, Any] = {}
    if "name" in changes:
        sets["name"] = _name(changes["name"])
    if "icon" in changes:
        sets["icon"] = _pick(changes["icon"], ICONS, p["icon"])
    if "color" in changes:
        sets["color"] = _pick(changes["color"], COLORS, p["color"])
    if "audience" in changes:
        sets["audience"] = _aud.normalize(changes["audience"], default="private")
    if "group" in changes:
        gid = str(changes["group"] or "").strip() or None
        if gid and not in_group(all_groups(db).get(gid) or {"members": "private"}, by, role):
            raise SpaceError(403, "you're not in that group")
        sets["grp"] = gid
    if changes.get("archived"):
        sets["archived_at"] = time.time()
    elif unarchive:
        sets["archived_at"] = None
    if sets:
        with _lock:
            con = _db(db)
            try:
                con.execute(f"UPDATE projects SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), pid))
                con.commit()
            finally:
                con.close()
    return get_project(pid, db) or {"id": pid, "archived": True}


# ── conversations filed under them ─────────────────────────────────────────────────────────────

def tags_for(person: str, db: Path | None = None) -> dict[str, dict[str, str | None]]:
    con = _db(db)
    try:
        return {r["conversation"]: {"project": r["project"], "group": r["grp"]}
                for r in con.execute("SELECT * FROM tags WHERE person=?", (person.lower(),))}
    finally:
        con.close()


def check_target(person: str, role: str | None, project: Any = None, group: Any = None,
                 db: Path | None = None) -> tuple[str | None, str | None]:
    """The (project, group) a person may file their conversation under: ids they can see, or 403."""
    pid = str(project or "").strip() or None
    gid = str(group or "").strip() or None
    groups = all_groups(db)
    if pid:
        p = get_project(pid, db)
        if not p or not project_visible(p, person, role, groups):
            raise SpaceError(403, "you can't see that project")
        gid = gid or p.get("group")
    if gid and not in_group(groups.get(gid) or {"members": "private"}, person, role):
        raise SpaceError(403, "you're not in that group")
    return pid, gid


def tag(person: str, conversation: str, project: str | None, group: str | None, *, only_new: bool = False,
        db: Path | None = None) -> bool:
    """File a person's own conversation (already checked with check_target). only_new: leave one that's filed."""
    conv = str(conversation or "").strip()[:80]
    if not conv:
        return False
    with _lock:
        con = _db(db)
        try:
            if only_new and con.execute("SELECT 1 FROM tags WHERE person=? AND conversation=?",
                                        (person.lower(), conv)).fetchone():
                return False
            if not project and not group:
                con.execute("DELETE FROM tags WHERE person=? AND conversation=?", (person.lower(), conv))
            else:
                con.execute("INSERT INTO tags (person, conversation, project, grp, at) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(person, conversation) DO UPDATE SET project=excluded.project, "
                            "grp=excluded.grp, at=excluded.at", (person.lower(), conv, project, group, time.time()))
            con.commit()
            return True
        finally:
            con.close()


# ── where a branch came from (titan-node#895, jellybean#706) ─────────────────────────────────────

_LINE_ID = re.compile(r"^[\w.:@+-]{1,80}$")


def clean_branch(v: Any) -> dict[str, str | None] | None:
    """A turn's `branched_from` ({thread_id, upto_message_id}) → {parent, upto}, or None. Opaque ids: a thread id and
    the Mac's message id; letters, digits and . : @ + - _ only, ≤ 80 each. The parent's messages may be the app's only."""
    if not isinstance(v, dict):
        return None
    parent = str(v.get("thread_id") or "").strip()
    upto = str(v.get("upto_message_id") or "").strip() or None
    if not _LINE_ID.match(parent) or (upto is not None and not _LINE_ID.match(upto)):
        return None
    return {"parent": parent, "upto": upto}


def note_branch(person: str, conversation: str, branch: dict[str, str | None] | None, db: Path | None = None) -> bool:
    """A branch's first turn says where it came from; kept once (a later turn never rewrites it), the person's own."""
    conv = str(conversation or "").strip()[:80]
    if not branch or not conv or branch.get("parent") == conv:
        return False
    with _lock:
        con = _db(db)
        try:
            cur = con.execute("INSERT OR IGNORE INTO lineage (person, conversation, parent, upto, at) VALUES (?,?,?,?,?)",
                              (person.lower(), conv, branch["parent"], branch.get("upto"), time.time()))
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()


def lineage_for(person: str, db: Path | None = None) -> dict[str, dict[str, str | None]]:
    """conversation → {thread_id, upto_message_id}: where each of the person's branches came from."""
    con = _db(db)
    try:
        return {r["conversation"]: {"thread_id": r["parent"], "upto_message_id": r["upto"]}
                for r in con.execute("SELECT * FROM lineage WHERE person=?", (person.lower(),))}
    finally:
        con.close()


# ── the seeds Zach chose (10-02) ───────────────────────────────────────────────────────────────

def home(gid: str, person: str, role: str | None, chats: list[dict[str, Any]] | None = None,
         todo_rows: list[dict[str, Any]] | None = None, people: list[dict[str, Any]] | None = None,
         db: Path | None = None) -> dict[str, Any]:
    """A group's page for one member (jellybean#240, WorkspaceHome): the group, its projects they can see, their own
    chats filed under it or its projects, its open to-dos, its people, and the calendar title the Mac shows.
    SpaceError 404 for anyone not in it."""
    g = all_groups(db).get(gid)
    if not g or not in_group(g, person, role):
        raise SpaceError(404, "no such group")
    projects = [p for p in projects_for(person, role, db) if p.get("group") == gid]
    pids = {p["id"] for p in projects}
    tags = tags_for(person, db)
    mine = [c for c in (chats or []) if (tags.get(c["id"]) or {}).get("group") == gid
            or (tags.get(c["id"]) or {}).get("project") in pids]
    return {"group": g, "projects": projects, "chats": mine,
            "todos": [t for t in (todo_rows or []) if t.get("for") == gid],
            "people": [p for p in (people or []) if p.get("role") != "household" and in_group(g, p["id"], p.get("role"))],
            "calendar": g.get("calendar")}


def seed(admin: str, parents: list[str], db: Path | None = None, household_calendar: str | None = None) -> list[str]:
    """Household and Parents (the admin and `parents`: Robin), School year in Parents; Titan server, Home Projects and Jellybean,
    private to the admin. Idempotent by name. Returns what it made."""
    made = []
    have_g = {g["name"].lower(): g for g in all_groups(db).values()}
    if "household" not in have_g:
        have_g["household"] = create_group(admin, "admin", "Household", "household", icon="house", color="green", db=db)
        if household_calendar:
            update_group(have_g["household"]["id"], admin, "admin", db=db, calendar=household_calendar)
        made.append("group Household")
    if "parents" not in have_g:
        have_g["parents"] = create_group(admin, "admin", "Parents", [admin, *parents], icon="users", color="violet", db=db)
        made.append("group Parents")
    have_p = {p["name"].lower() for p in projects_for(admin, "admin", db)}
    for name, icon, color, grp in (("Titan server", "server", "blue", None), ("Home Projects", "house", "amber", None),
                                   ("Jellybean", "bean", "red", None), ("School year", "school", "indigo", "parents")):
        if name.lower() not in have_p:
            create_project(admin, "admin", name, icon, color, group=have_g[grp]["id"] if grp else None, db=db)
            made.append(f"project {name}")
    return made
