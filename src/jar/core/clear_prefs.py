#!/usr/bin/env python3
"""What "clear this" means for each person (#1646, slice 1): archive or move to the trash, one setting with an
override per kind of item. The default is archive. Jellybean's own clearing follows it, so "keep everything" and
"clear to the trash" are one switch.

  archive   keep it, out of the way (mail's Archive)
  trash     recoverable for the source's own retention (mail's Trash)

Deleting permanently isn't offered here: it can't be undone, so it waits for its own decision and review.

Kinds today: mail (mail_moves follows it). The others (files, photos, notes, reminders, messages) are accepted so a
person's choice is kept, and they take effect as each source learns to clear (#1646).

Each person's own (kids too, though only adults clear mail); a shared household screen has none (the route table).
Kept in spaces.db's clear_prefs, so delete-my-data covers it (me_delete's stores)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

MODES = ("archive", "trash")
DEFAULT = "archive"
KINDS = ("mail", "files", "photos", "notes", "reminders", "messages")
_lock = threading.Lock()


class Invalid(Exception):
    pass


def _db(path: Path | None = None) -> sqlite3.Connection:
    import spaces
    con = spaces._db(path)
    con.execute("CREATE TABLE IF NOT EXISTS clear_prefs (person TEXT PRIMARY KEY, mode TEXT NOT NULL, kinds TEXT NOT NULL "
                "DEFAULT '{}', updated_at REAL)")
    return con


def get(person: str, path: Path | None = None) -> dict[str, Any]:
    """{mode, kinds: {kind: mode}, updated_at}; nothing set yet: archive, no overrides."""
    con = _db(path)
    try:
        r = con.execute("SELECT mode, kinds, updated_at FROM clear_prefs WHERE person=?", (person,)).fetchone()
    finally:
        con.close()
    if r is None:
        return {"mode": DEFAULT, "kinds": {}, "updated_at": None}
    try:
        kinds = {k: v for k, v in json.loads(r[1] or "{}").items() if k in KINDS and v in MODES}
    except (ValueError, AttributeError):
        # a damaged override can't be honoured, and the default alone might send a kind its owner keeps to the trash:
        # the whole setting reads as the safe one
        return {"mode": DEFAULT, "kinds": {}, "updated_at": r[2]}
    return {"mode": r[0] if r[0] in MODES else DEFAULT, "kinds": kinds, "updated_at": r[2]}


def mode(person: str | None, kind: str, path: Path | None = None) -> str:
    """What clearing one kind of this person's items does. No person, or anything unreadable: archive."""
    if not person:
        return DEFAULT
    try:
        p = get(person, path)
    except Exception:  # noqa: BLE001  (the safe choice when the setting can't be read: keep it)
        return DEFAULT
    return p["kinds"].get(kind) or p["mode"]


def clean(body: Any) -> dict[str, Any]:
    """Either or both of {mode, kinds: {kind: mode | null}}; null drops an override. Anything else is Invalid."""
    if not isinstance(body, dict) or not body:
        raise Invalid("send mode and/or kinds")
    extra = set(body) - {"mode", "kinds"}
    if extra:
        raise Invalid(f"unknown field: {sorted(extra)[0]}")
    out: dict[str, Any] = {}
    if "mode" in body:
        m = str(body["mode"] or "").strip().lower()
        if m == "delete":
            raise Invalid("deleting permanently isn't offered yet: it can't be undone (#1646)")
        if m not in MODES:
            raise Invalid(f"mode is one of {', '.join(MODES)}")
        out["mode"] = m
    if "kinds" in body:
        if not isinstance(body["kinds"], dict):
            raise Invalid("kinds is an object of kind: mode")
        kinds: dict[str, str | None] = {}
        for k, v in body["kinds"].items():
            if k not in KINDS:
                raise Invalid(f"kind is one of {', '.join(KINDS)}")
            if v is None:
                kinds[k] = None
                continue
            m = str(v).strip().lower()
            if m == "delete":
                raise Invalid("deleting permanently isn't offered yet: it can't be undone (#1646)")
            if m not in MODES:
                raise Invalid(f"{k}'s mode is one of {', '.join(MODES)}, or null to follow the default")
            kinds[k] = m
        out["kinds"] = kinds
    return out


def put(person: str, body: Any, path: Path | None = None) -> dict[str, Any]:
    """Change what's sent, stamped now; the rest stays. → the person's setting."""
    ch = clean(body)
    with _lock:
        cur = get(person, path)
        kinds = dict(cur["kinds"])
        for k, v in (ch.get("kinds") or {}).items():
            if v is None:
                kinds.pop(k, None)
            else:
                kinds[k] = v
        new = {"mode": ch.get("mode", cur["mode"]), "kinds": kinds, "updated_at": time.time()}
        con = _db(path)
        try:
            with con:
                con.execute("INSERT INTO clear_prefs (person, mode, kinds, updated_at) VALUES (?,?,?,?) ON CONFLICT(person) "
                            "DO UPDATE SET mode=excluded.mode, kinds=excluded.kinds, updated_at=excluded.updated_at",
                            (person, new["mode"], json.dumps(new["kinds"], sort_keys=True), new["updated_at"]))
        finally:
            con.close()
    return new
