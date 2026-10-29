#!/usr/bin/env python3
"""Todos: the household's list of things to do, and the list for the Claude sessions.

A todo belongs to a person (their own list), to the household (everyone's), or to Claude: the
work queue the Claude Code sessions building titan and Jellybean read (GET /v1/todos?for=claude),
so "tell the Claude working on features…" works from the phone, where send_to_claude can't
reach the Mac. Reminders with a time stay Apple Reminders (create_reminder); a todo has no alarm.

09-30: "this is a todo" became a plan_task that titan tried to run, and "tell Claude" became a
knowledge-base note that nothing read.

Store: SQLite at $TODOS_DB (default /ai/datasets/connectors/todos.db).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("TODOS_DB", "/ai/datasets/connectors/todos.db"))
HOUSEHOLD, CLAUDE = "household", "claude"
_lock = threading.Lock()


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("TODOS_DB") or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS todos (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, detail TEXT, owner TEXT NOT NULL, created_by TEXT,
        created_at REAL NOT NULL, done_at REAL, done_by TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_todos_owner ON todos(owner, done_at)")
    cols = [r[1] for r in con.execute("PRAGMA table_info(todos)")]
    for col, decl in (("repo", "TEXT"), ("issue_number", "INTEGER"), ("issue_url", "TEXT"), ("created_by_id", "TEXT"),
                      ("place", "TEXT")):   # place: a todo for when they're there (#284)
        if col not in cols:   # Claude's todos mirrored to GitHub issues (todo_issues.py)
            con.execute(f"ALTER TABLE todos ADD COLUMN {col} {decl}")
    return con


def _row(r: sqlite3.Row) -> dict[str, Any]:
    keys = r.keys()
    return {"id": r["id"], "title": r["title"], "detail": r["detail"] or "", "for": r["owner"],
            "created_by": r["created_by"], "created_at": r["created_at"], "done_at": r["done_at"],
            "done_by": r["done_by"], "repo": r["repo"] if "repo" in keys else None,
            "issue_url": r["issue_url"] if "issue_url" in keys else None,
            "issue_number": r["issue_number"] if "issue_number" in keys else None,
            "created_by_id": r["created_by_id"] if "created_by_id" in keys else None,
            "place": r["place"] if "place" in keys else None}


def _changed(row: dict[str, Any]) -> None:
    """Tell the list's devices (the continuity feed, jellybean#138): its owner's list, or the household's."""
    try:
        import continuity
        continuity.emit(row.get("for"), "todo", {"id": row["id"], "title": row["title"], "for": row.get("for"),
                                                  "done": bool(row.get("done_at"))})
    except Exception:  # noqa: BLE001 - a todo is saved whatever the feed does
        pass


def add(title: str, owner: str, *, detail: str = "", created_by: str | None = None,
        db: Path | None = None, repo: str | None = None, created_by_id: str | None = None,
        place: str | None = None) -> dict[str, Any]:
    """`created_by`: the requester's name, for display; `created_by_id`: their person id, so news about the
    todo (a Claude todo's Shipped card, #383) goes back to them."""
    title = " ".join(str(title or "").split())[:200]
    if not title:
        raise ValueError("a todo needs a title")
    tid = "td-" + uuid.uuid4().hex[:10]
    with _lock:
        con = _db(db)
        try:
            where = _place(place)
            con.execute("INSERT INTO todos (id, title, detail, owner, created_by, created_at, repo, created_by_id, place) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (tid, title, str(detail or "").strip()[:2000], owner, created_by, time.time(), repo, created_by_id,
                         where or None))
            con.commit()
            row = _row(con.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone())
            _changed(row)
            return row
        finally:
            con.close()


def list_for(owners: list[str], *, include_done: bool = False, db: Path | None = None) -> list[dict[str, Any]]:
    if not owners:
        return []
    con = _db(db)
    try:
        q = f"SELECT * FROM todos WHERE owner IN ({','.join('?' * len(owners))})"
        if not include_done:
            q += " AND done_at IS NULL"
        return [_row(r) for r in con.execute(q + " ORDER BY done_at IS NOT NULL, created_at", owners)]
    finally:
        con.close()


def find(ref: str, owners: list[str], db: Path | None = None) -> list[dict[str, Any]]:
    """Open todos matching an id or a title (exact, then contains), among these lists."""
    ref = " ".join(str(ref or "").split()).lower()
    open_ = list_for(owners, db=db)
    exact = [t for t in open_ if t["id"] == ref or t["title"].lower() == ref]
    return exact or [t for t in open_ if ref and ref in t["title"].lower()]


def complete(tid: str, done_by: str | None = None, db: Path | None = None) -> bool:
    with _lock:
        con = _db(db)
        try:
            cur = con.execute("UPDATE todos SET done_at=?, done_by=? WHERE id=? AND done_at IS NULL",
                              (time.time(), done_by, tid))
            con.commit()
            if cur.rowcount:
                _changed(_row(con.execute("SELECT * FROM todos WHERE id=?", (tid,)).fetchone()))
            return cur.rowcount > 0
        finally:
            con.close()


def waiting_at(person: str, role: str | None, place: str, db: Path | None = None) -> list[dict[str, Any]]:
    """The open todos for this place on the lists this person sees (their own and the household's; never Claude's),
    oldest first (#284: what's waiting when they get there)."""
    where = (_place(place) or "").lower()
    lists = [x for x in lists_visible(person, role) if x != CLAUDE]
    return [t for t in list_for(lists, db=db) if (t.get("place") or "").lower() == where] if where else []


def _place(place: str | None) -> str | None:
    """A place's name as places.py keeps it ('home', or the person's own name for it); None when there's none."""
    import places
    try:
        return places.clean_place(place) if place else None
    except ValueError:
        return None


def lists_visible(person_id: str | None, role: str | None) -> list[str]:
    """Which lists a person sees: their own and the household's; the admin also sees Claude's."""
    if role == "household":
        return [HOUSEHOLD]
    out = [x for x in (person_id, HOUSEHOLD) if x]
    if role in (None, "admin"):
        out.append(CLAUDE)
    if person_id:   # each group the person is in has its own list, seen by its members (jellybean#240)
        try:
            import spaces
            out += [g["id"] for g in spaces.groups_for(person_id, role)]
        except Exception:  # noqa: BLE001 - no spaces store here: just the usual lists
            pass
    return out


def set_issue(tid: str, repo: str, number: int, url: str, db: Path | None = None) -> None:
    with _lock:
        con = _db(db)
        try:
            con.execute("UPDATE todos SET repo=?, issue_number=?, issue_url=? WHERE id=?", (repo, number, url, tid))
            con.commit()
        finally:
            con.close()
