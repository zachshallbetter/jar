"""Continuity across a person's devices (jellybean#132 + #138): one change feed, read state, and
what each device was last on.

  - changes: what changed for a person, in order (a chat message from another of their devices, a
    conversation read, a todo, a task step), for GET /v1/sync's long-poll. Each row names an owner:
    a person id, "household" (household todos) or "claude". A reader sees only the owners its role
    may (todos.lists_visible), and their own person rows.
  - reads: how far a person has read each conversation (POST /v1/conversations/{id}/read).
  - context: the latest thing each device was on (a page, a document, a song), kept half an hour or
    until another of the person's devices picks it up, one row per device. Never in the KB, memory or a turn's prompt; only shown back to the same person.

The feed's cursor is opaque and bound to its person (#420): the store's sequence is shared by
everyone, so a plain number would show a person how much changed for the rest of the household.
A change made on a person's device nudges their other devices with a content-free push (#419).

Store: SQLite at $CONTINUITY_DB (default /ai/datasets/connectors/continuity.db).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("CONTINUITY_DB", "/ai/datasets/connectors/continuity.db"))
KEEP_S = 7 * 86400          # changes older than a week are dropped (a device that long away re-reads)
CONTEXT_S = 30 * 60         # what a device was on is kept half an hour, or until picked up (#132)
KINDS = ("message", "thread", "todo", "task")
CONTEXT_KINDS = ("web", "document", "media")
_lock = threading.Lock()


_TEST_ENV = ("TODOS_DB", "AGENT_MEMORY_DIR", "HOUSEHOLD_DB", "TASK_LOG_DB", "AGENT_TURN_LOG")


def _path() -> Path:
    """$CONTINUITY_DB; else, when another store points into a temporary directory (a test), a file next
    to it, never the live feed (10-01 review: the deploy gate's todo and turn tests would have written
    their fixtures into every person's real feed); else the default."""
    if os.environ.get("CONTINUITY_DB"):
        return Path(os.environ["CONTINUITY_DB"])
    import tempfile
    tmp = (str(Path(tempfile.gettempdir()).resolve()), "/tmp", "/private/tmp", "/var/folders")
    for k in _TEST_ENV:
        v = os.environ.get(k, "")
        if v and (v.startswith(tmp) or str(Path(v).resolve()).startswith(tmp)):
            return Path(v).resolve().parent / "continuity-test.db"
    return DB_PATH


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or _path())
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
            kind TEXT NOT NULL, data TEXT NOT NULL, at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS changes_owner ON changes (owner, seq);
        CREATE TABLE IF NOT EXISTS reads (person TEXT NOT NULL, conv TEXT NOT NULL, upto REAL NOT NULL,
            PRIMARY KEY (person, conv));
        CREATE TABLE IF NOT EXISTS context (person TEXT NOT NULL, device TEXT NOT NULL, device_name TEXT,
            kind TEXT NOT NULL, title TEXT, url TEXT, path TEXT, app TEXT, position REAL, at REAL NOT NULL,
            PRIMARY KEY (person, device));
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);""")
    if "device" not in {r[1] for r in con.execute("PRAGMA table_info(changes)")}:
        con.execute("ALTER TABLE changes ADD COLUMN device TEXT")   # the device a change came from (#419)
    return con


# ── the change feed ──────────────────────────────────────────────────────────────────────────

def emit(owner: str | None, kind: str, data: dict[str, Any], db: Path | None = None,
         device: str | None = None) -> int | None:
    """Record one change for `owner`. `device`: the device it was made on (its own feed marks it
    `mine`, and it isn't nudged). Never raises: the feed must not break what changed."""
    if not owner or kind not in KINDS:
        return None
    try:
        with _lock:
            con = _db(db)
            try:
                cur = con.execute("INSERT INTO changes (owner, kind, data, at, device) VALUES (?,?,?,?,?)",
                                  (str(owner), kind, json.dumps(data, default=str), time.time(), device))
                con.execute("DELETE FROM changes WHERE at < ?", (time.time() - KEEP_S,))
                con.commit()
                seq = int(cur.lastrowid)
            finally:
                con.close()
    except Exception:  # noqa: BLE001
        return None
    if db is None:
        try:
            import push
            push.nudge_sync_async(str(owner), device)
        except Exception:  # noqa: BLE001 - a nudge never stops the feed
            pass
    return seq


def latest(owners: list[str], db: Path | None = None) -> int:
    """The newest change for these owners (0: none), the long-poll's cheap check."""
    con = _db(db)
    try:
        marks = ",".join("?" * len(owners))
        row = con.execute(f"SELECT MAX(seq) AS s FROM changes WHERE owner IN ({marks})", owners).fetchone()
        return int(row["s"] or 0)
    finally:
        con.close()


def since(owners: list[str], cursor: int, limit: int = 200, db: Path | None = None) -> list[dict[str, Any]]:
    """Changes after `cursor` for these owners, oldest first: [{seq, kind, at, from_device, …data}]
    (`seq` and `from_device` are the store's own: the API turns them into a cursor and `mine`)."""
    con = _db(db)
    try:
        marks = ",".join("?" * len(owners))
        rows = con.execute(f"SELECT seq, kind, data, at, device FROM changes WHERE owner IN ({marks}) AND seq > ? "
                           f"ORDER BY seq LIMIT ?", (*owners, int(cursor), int(limit))).fetchall()
        return [{**json.loads(r["data"]), "seq": r["seq"], "kind": r["kind"], "at": r["at"], "from_device": r["device"]}
                for r in rows]
    finally:
        con.close()


# ── the cursor (#420) ─────────────────────────────────────────────────────────────────────────

def _key(db: Path | None = None) -> bytes:
    """This store's cursor key, made once (a new store makes every old cursor unknown: a resync)."""
    with _lock:
        con = _db(db)
        try:
            row = con.execute("SELECT v FROM meta WHERE k='cursor_key'").fetchone()
            if row:
                return bytes.fromhex(row["v"])
            k = secrets.token_bytes(32)
            con.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('cursor_key', ?)", (k.hex(),))
            con.commit()
            return bytes.fromhex(con.execute("SELECT v FROM meta WHERE k='cursor_key'").fetchone()["v"])
        finally:
            con.close()


def _prf(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"\x00".join(parts), hashlib.sha256).digest()


def encode_cursor(person: str, seq: int, db: Path | None = None) -> str:
    """A position in `person`'s feed that says nothing else: the sequence number encrypted and
    authenticated for that person (a fresh nonce each time, so two cursors can't be compared)."""
    key, nonce, who = _key(db), secrets.token_bytes(8), str(person).encode()
    ct = bytes(a ^ b for a, b in zip(int(seq).to_bytes(8, "big"), _prf(key, b"pad", who, nonce)[:8]))
    tag = _prf(key, b"tag", who, nonce, ct)[:12]
    return base64.urlsafe_b64encode(nonce + ct + tag).decode().rstrip("=")


def decode_cursor(person: str, cursor: str, db: Path | None = None) -> int | None:
    """The sequence number in a cursor made for `person`; None for anything else (made for someone
    else, by an older store, altered, or not a cursor at all)."""
    try:
        raw = base64.urlsafe_b64decode(str(cursor) + "=" * (-len(str(cursor)) % 4))
    except (ValueError, TypeError):
        return None
    if len(raw) != 28:
        return None
    key, who = _key(db), str(person).encode()
    nonce, ct, tag = raw[:8], raw[8:16], raw[16:]
    if not hmac.compare_digest(tag, _prf(key, b"tag", who, nonce, ct)[:12]):
        return None
    return int.from_bytes(bytes(a ^ b for a, b in zip(ct, _prf(key, b"pad", who, nonce)[:8])), "big")


# ── read state ───────────────────────────────────────────────────────────────────────────────

def set_read(person: str, conv: str, upto: float, db: Path | None = None, device: str | None = None) -> float:
    """How far `person` has read conversation `conv` (never moves back). Returns the stored mark and
    tells their other devices."""
    with _lock:
        con = _db(db)
        try:
            row = con.execute("SELECT upto FROM reads WHERE person=? AND conv=?", (person, conv)).fetchone()
            mark = max(float(upto), float(row["upto"]) if row else 0.0)
            con.execute("INSERT OR REPLACE INTO reads (person, conv, upto) VALUES (?,?,?)", (person, conv, mark))
            con.commit()
        finally:
            con.close()
    emit(person, "thread", {"thread_id": conv, "read_upto": mark}, db=db, device=device)
    return mark


def reads(person: str, db: Path | None = None) -> dict[str, float]:
    con = _db(db)
    try:
        return {r["conv"]: r["upto"] for r in con.execute("SELECT conv, upto FROM reads WHERE person=?", (person,))}
    finally:
        con.close()


# ── what each device was on ──────────────────────────────────────────────────────────────────

_TOKENISH = re.compile(r"(?i)(token|key|secret|session|auth|sig|code|password|pass|otp)")


def _secretish(seg: str) -> bool:
    """A path segment that looks like a capability (a document id, a reset token, a JWT): 16+ characters
    mixing letters and digits, or naming a secret, checked after percent-decoding."""
    s = urllib.parse.unquote(seg)
    return (len(s) >= 16 and re.search(r"[A-Za-z]", s) is not None and re.search(r"\d", s) is not None) \
        or (_TOKENISH.search(s) is not None and len(s) > 24) or s.startswith("eyJ")


def clean_url(raw: Any) -> str | None:
    """A page's address as kept: http(s) only, no query or fragment (where tokens and session ids
    live), no user:password@. None for anything else."""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        u = urllib.parse.urlsplit(s)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    path = "/".join("…" if _secretish(seg) else seg for seg in u.path.split("/"))
    name = f"[{u.hostname}]" if ":" in u.hostname else u.hostname   # IPv6 keeps its brackets
    host = name + (f":{u.port}" if u.port else "")
    return urllib.parse.urlunsplit((u.scheme, host, path, "", ""))[:500]


def put_context(person: str, device: str, device_name: str | None, body: dict[str, Any],
                db: Path | None = None) -> dict[str, Any]:
    """What this device is on now (replaces its last). ValueError when there's nothing usable."""
    kind = str(body.get("kind") or "").strip().lower()
    if kind not in CONTEXT_KINDS:
        raise ValueError(f"kind must be one of {', '.join(CONTEXT_KINDS)}")
    import redact
    title = redact.redact_personal(" ".join(str(body.get("title") or "").split())[:200])
    url = clean_url(body.get("url")) if kind == "web" else None
    path = str(body.get("path") or "").strip()[:500] or None if kind == "document" else None
    app = " ".join(str(body.get("app") or "").split())[:80] or None
    try:
        position = float(body["position"]) if body.get("position") is not None else None
    except (TypeError, ValueError):
        position = None
    if kind == "web" and not url:
        raise ValueError("a web page needs an http(s) url")
    if not (title or url or path):
        raise ValueError("say what it is: a title, a url or a path")
    row = {"device": device, "device_name": device_name, "kind": kind, "title": title or None, "url": url,
           "path": path, "app": app, "position": position, "at": time.time()}
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO context (person, device, device_name, kind, title, url, path, app, position, at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (person, device, device_name, kind, row["title"], url, path, app, position, row["at"]))
            con.execute("DELETE FROM context WHERE at < ?", (time.time() - CONTEXT_S,))
            con.commit()
        finally:
            con.close()
    return row


def get_context(person: str, except_device: str | None = None, db: Path | None = None) -> list[dict[str, Any]]:
    """What this person's other devices were last on (the last day), newest first."""
    con = _db(db)
    try:
        rows = con.execute("SELECT * FROM context WHERE person=? AND at >= ? ORDER BY at DESC",
                           (person, time.time() - CONTEXT_S)).fetchall()
        return [{k: r[k] for k in r.keys() if k != "person"} for r in rows if r["device"] != except_device]
    finally:
        con.close()


def take_context(person: str, device: str, db: Path | None = None) -> bool:
    """Another of the person's devices picked up what `device` was on (or dismissed it): it goes."""
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM context WHERE person=? AND device=?", (person, device)).rowcount
            con.commit()
            return n > 0
        finally:
            con.close()


def forget_device(person: str, device: str, db: Path | None = None) -> int:
    """One of their devices left (they still have others): only what that device was on goes."""
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM context WHERE person=? AND device=?", (person, device)).rowcount
            con.commit()
            return n
        finally:
            con.close()


def forget_person(person: str, db: Path | None = None) -> int:
    """Leaving the household: their feed, read marks and context go."""
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM changes WHERE owner=?", (person,)).rowcount
            n += con.execute("DELETE FROM reads WHERE person=?", (person,)).rowcount
            n += con.execute("DELETE FROM context WHERE person=?", (person,)).rowcount
            con.commit()
            return n
        finally:
            con.close()
