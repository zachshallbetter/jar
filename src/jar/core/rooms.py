#!/usr/bin/env python3
"""The household's rooms as a store (titan-node#1511; Zach, 10-10: "I can't add or edit Rooms"). Before this a room was
only a name: a screen's `devices.room`, and Home Assistant's areas read by name (room_state, room hints).

  rooms     id, name (what the household calls it), ha_area (the Home Assistant area it is, if any), created_at,
            removed_at. Names are unique, ignoring case and spacing.
  HA areas  read, never written. Each area becomes a room the first time the rooms are listed. Renaming one changes
            only titan's name for it: `ha_area` keeps pointing at the area, so occupancy (room_state) and hints still
            find its sensors. Removing one leaves a tombstone, so the area doesn't come back as a room by itself.
  screens   follow the room: a rename moves every device in it to the new name, and a removal takes the room off
            them. Both return which screens moved (a household screen's name; anyone else's device only as a count).
  undo      rename and remove return what undo needs (surfaces.save_undo keeps it): the old name, or the room and the
            devices it was taken from.

Adults and the admin change rooms; kids see them (their surface offers the operations as unavailable); a shared
screen gets none of this (it names its own room with PATCH /v1/devices/self).
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("ROOMS_DB", "/ai/datasets/connectors/rooms.db"))
NAME_RX = re.compile(r"[A-Za-z0-9' &-]+")      # the same words a screen may name its room with (set_device_room)
_lock = threading.Lock()


class RoomError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def key(name: Any) -> str:
    """How names compare: case and spacing ignored (room_state.room_key's rule)."""
    return " ".join(str(name or "").split()).casefold()


def clean(name: Any) -> str:
    n = " ".join(str(name or "").split())
    if not n or len(n) > 30 or not NAME_RX.fullmatch(n):
        raise RoomError(400, "a room's name is 1-30 letters, digits, spaces, ' & or -")
    return n


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, name TEXT NOT NULL, ha_area TEXT, "
                "created_at REAL NOT NULL, removed_at REAL)")
    try:
        os.chmod(p, 0o640)
    except OSError:
        pass
    return con


def _live(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM rooms WHERE removed_at IS NULL ORDER BY name COLLATE NOCASE").fetchall()


def _taken(con: sqlite3.Connection, name: str, but: str | None = None) -> bool:
    return any(key(r["name"]) == key(name) and r["id"] != but for r in _live(con))


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "ha_area": r["ha_area"]}


def sync(ha_areas: list[str] | None, device_rooms: list[str] | None = None, *, path: Path | None = None,
         now: float | None = None) -> list[dict[str, Any]]:
    """The rooms, after taking in what's new: each Home Assistant area not seen before (a removed one stays removed),
    and each room a screen already names that isn't one yet (rooms from before this store). → live rooms by name."""
    now = time.time() if now is None else now
    with _lock:
        con = _db(path)
        try:
            with con:
                seen_areas = {key(r["ha_area"]) for r in con.execute("SELECT ha_area FROM rooms WHERE ha_area IS NOT NULL")}
                for area in ha_areas or []:
                    a = " ".join(str(area or "").split())
                    if not a or key(a) in seen_areas:
                        continue
                    seen_areas.add(key(a))
                    # an area whose name a local room already has: that room is this area (it was named first)
                    same = [r for r in _live(con) if key(r["name"]) == key(a) and not r["ha_area"]]
                    if same:
                        con.execute("UPDATE rooms SET ha_area=? WHERE id=?", (a, same[0]["id"]))
                    else:
                        con.execute("INSERT INTO rooms VALUES (?,?,?,?,NULL)", (_new_id(), a[:30], a, now))
                known = {key(r["name"]) for r in con.execute("SELECT name FROM rooms")}    # removed ones too
                for name in device_rooms or []:
                    n = " ".join(str(name or "").split())
                    if n and key(n) not in known:
                        known.add(key(n))
                        con.execute("INSERT INTO rooms VALUES (?,?,NULL,?,NULL)", (_new_id(), n, now))
            return [_row(r) for r in _live(con)]
        finally:
            con.close()


def _new_id() -> str:
    return "rm-" + secrets.token_hex(5)


def live(path: Path | None = None) -> list[dict[str, Any]]:
    con = _db(path)
    try:
        return [_row(r) for r in _live(con)]
    finally:
        con.close()


def area_of(path: Path | None = None) -> dict[str, str]:
    """key(room name) → its Home Assistant area (rooms renamed here still find their sensors)."""
    return {key(r["name"]): r["ha_area"] for r in live(path) if r["ha_area"]}


def get(room_id: str, path: Path | None = None) -> dict[str, Any] | None:
    con = _db(path)
    try:
        r = con.execute("SELECT * FROM rooms WHERE id=? AND removed_at IS NULL", (str(room_id),)).fetchone()
        return _row(r) if r else None
    finally:
        con.close()


def add(name: Any, *, path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    n = clean(name)
    with _lock:
        con = _db(path)
        try:
            with con:
                if _taken(con, n):
                    raise RoomError(409, "there's already a room by that name")
                rid = _new_id()
                con.execute("INSERT INTO rooms VALUES (?,?,NULL,?,NULL)", (rid, n, time.time() if now is None else now))
            return {"id": rid, "name": n, "ha_area": None}
        finally:
            con.close()


def rename(room_id: str, name: Any, *, path: Path | None = None) -> tuple[dict[str, Any], str]:
    """→ (the room, its old name). The caller moves the screens (household store) and keeps the undo."""
    n = clean(name)
    with _lock:
        con = _db(path)
        try:
            with con:
                r = con.execute("SELECT * FROM rooms WHERE id=? AND removed_at IS NULL", (str(room_id),)).fetchone()
                if not r:
                    raise RoomError(404, "no such room")
                if _taken(con, n, but=r["id"]):
                    raise RoomError(409, "there's already a room by that name")
                con.execute("UPDATE rooms SET name=? WHERE id=?", (n, r["id"]))
            return {"id": r["id"], "name": n, "ha_area": r["ha_area"]}, r["name"]
        finally:
            con.close()


def remove(room_id: str, *, path: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Tombstones the room (an HA area stays out of the list); → the room as it was."""
    with _lock:
        con = _db(path)
        try:
            with con:
                r = con.execute("SELECT * FROM rooms WHERE id=? AND removed_at IS NULL", (str(room_id),)).fetchone()
                if not r:
                    raise RoomError(404, "no such room")
                con.execute("UPDATE rooms SET removed_at=? WHERE id=?", (time.time() if now is None else now, r["id"]))
            return _row(r)
        finally:
            con.close()


def restore(room_id: str, name: str, *, path: Path | None = None) -> dict[str, Any]:
    """Undo of a remove or a rename: the room back, by its id, with that name (refused if the name is taken now)."""
    with _lock:
        con = _db(path)
        try:
            with con:
                r = con.execute("SELECT * FROM rooms WHERE id=?", (str(room_id),)).fetchone()
                if not r:
                    raise RoomError(404, "no such room")
                if _taken(con, name, but=r["id"]):
                    raise RoomError(409, "another room has that name now")
                con.execute("UPDATE rooms SET name=?, removed_at=NULL WHERE id=?", (name, r["id"]))
            return {"id": r["id"], "name": name, "ha_area": r["ha_area"]}
        finally:
            con.close()
