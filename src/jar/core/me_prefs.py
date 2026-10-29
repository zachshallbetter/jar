#!/usr/bin/env python3
"""A person's look across their devices (#1159): {look, accent, follow}, kept on titan instead of iCloud key-value
storage (the Mac's iCloud entitlement needs a hand-made provisioning profile, jellybean#323). The Mac writes it when
the look or accent changes; the iPhone and Watch read it on refresh while `follow` is on.

Each person's own (kids too); a shared household screen has none (the route table). Kept in spaces.db's theme_prefs,
so delete-my-data covers it (me_delete's stores)."""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

LOOKS = ("cool", "warm", "graphite", "paper", "evening", "macos")
ACCENTS = ("sienna", "dusk", "grove", "ember", "slate")      # Settings' catalogue (jellybean SettingsSpec AccentBrand)
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")                      # or a custom colour
_lock = threading.Lock()


class Invalid(Exception):
    pass


def _db(path: Path | None = None) -> sqlite3.Connection:
    import spaces
    con = spaces._db(path)
    con.execute("CREATE TABLE IF NOT EXISTS theme_prefs (person TEXT PRIMARY KEY, look TEXT, accent TEXT, "
                "follow INTEGER NOT NULL DEFAULT 1, updated_at REAL)")
    return con


def get(person: str, path: Path | None = None) -> dict[str, Any]:
    """The person's own; nothing set yet: look and accent null, follow true (each device keeps its own until then)."""
    con = _db(path)
    try:
        r = con.execute("SELECT look, accent, follow, updated_at FROM theme_prefs WHERE person=?", (person,)).fetchone()
    finally:
        con.close()
    if r is None:
        return {"look": None, "accent": None, "follow": True, "updated_at": None}
    return {"look": r[0], "accent": r[1], "follow": bool(r[2]), "updated_at": r[3]}


def clean(body: Any) -> dict[str, Any]:
    """Any of look, accent, follow; anything else, or a value outside the catalogue, is Invalid."""
    if not isinstance(body, dict) or not body:
        raise Invalid("send any of look, accent, follow")
    extra = set(body) - {"look", "accent", "follow"}
    if extra:
        raise Invalid(f"unknown field: {sorted(extra)[0]}")
    out: dict[str, Any] = {}
    if "look" in body:
        look = str(body["look"] or "").strip().lower()
        if look not in LOOKS:
            raise Invalid(f"look is one of {', '.join(LOOKS)}")
        out["look"] = look
    if "accent" in body:
        raw = str(body["accent"] or "").strip()
        acc = raw.upper() if _HEX.match(raw) else raw.lower()
        if acc not in ACCENTS and not _HEX.match(acc):
            raise Invalid(f"accent is one of {', '.join(ACCENTS)}, or #RRGGBB")
        out["accent"] = acc
    if "follow" in body:
        if not isinstance(body["follow"], bool):
            raise Invalid("follow is true or false")
        out["follow"] = body["follow"]
    return out


def put(person: str, body: Any, path: Path | None = None) -> dict[str, Any]:
    """Change the fields sent, stamped now; the rest stay. → the person's prefs."""
    ch = clean(body)
    with _lock:
        cur = get(person, path)
        new = {**cur, **ch, "updated_at": time.time()}
        con = _db(path)
        try:
            con.execute("INSERT OR REPLACE INTO theme_prefs (person, look, accent, follow, updated_at) VALUES (?,?,?,?,?)",
                        (person, new["look"], new["accent"], int(bool(new["follow"])), new["updated_at"]))
            con.commit()
        finally:
            con.close()
    return new
