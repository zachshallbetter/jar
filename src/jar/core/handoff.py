#!/usr/bin/env python3
"""Continue on another room's screen (jellybean#433 slice 1): a person puts one of their replies on the household
screens in a room they name ("Continue on the kitchen screen"). Nobody is tracked: the person names the room, titan
finds the screens by the room each household device set for itself (PATCH /v1/devices/self {room}).

  rooms      GET /v1/handoff/rooms: the rooms with a household screen ({room, screens}), for the apps' "Continue on…"
  send       POST /v1/handoff {to_room, kind: "reply", text, title?, thread_id?}: a person's own words, put there by them
             (their choice is the consent). 201 {id, room, screens, expires_at}.
  receive    GET /v1/handoff/self (a household screen): what was sent to it, newest first, while it lasts.
  end        TTL_SECONDS on its own; the screen's Done or the sender's take-back (DELETE /v1/handoff/{id}). The text is
             gone from titan when it ends: nothing is kept past it.
Music following a person and anything inferred about where someone is are later slices (#433), not this one.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

DB = Path(os.environ.get("HANDOFF_DB", "/ai/datasets/connectors/handoff.db"))
TTL_SECONDS = 10 * 60
MAX_TEXT = 8000
MAX_TITLE = 120
KINDS = ("reply",)
PEOPLE_ROLES = ("admin", "adult", "kid")

_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    p = Path(os.environ.get("HANDOFF_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS handoffs (id TEXT PRIMARY KEY, sender TEXT, sender_name TEXT, room TEXT,"
                " devices TEXT, kind TEXT, title TEXT, text TEXT, thread_id TEXT, created_at REAL, expires_at REAL,"
                " done_by TEXT)")
    return con


def _room_key(room: Any) -> str:
    return " ".join(str(room or "").split()).casefold()


def screens(people: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Room (folded) → the household screens in it: a household device that named its room and isn't revoked."""
    out: dict[str, list[dict[str, Any]]] = {}
    for p in people:
        if p.get("role") != "household":
            continue
        for d in p.get("devices") or []:
            if d.get("revoked") or not d.get("room"):
                continue
            out.setdefault(_room_key(d["room"]), []).append({"id": d["id"], "name": d.get("name") or "Screen",
                                                              "room": " ".join(str(d["room"]).split())})
    return out


def rooms(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rooms someone can continue on, by name, with how many screens each has."""
    return sorted(({"room": s[0]["room"], "screens": len(s)} for s in screens(people).values()), key=lambda r: r["room"])


def sweep(now: float | None = None) -> int:
    """Ended hand-offs go, text and all."""
    now = time.time() if now is None else now
    with _lock:
        con = _db()
        with con:
            n = con.execute("DELETE FROM handoffs WHERE expires_at<=? OR done_by IS NOT NULL", (now,)).rowcount
        con.close()
    return n


def fence_why(fences_row_list: list[Any]) -> str:
    """What a fence holding a hand-off says, without the text: the first fence's kind, in the household's words."""
    f = fences_row_list[0]
    if f["kind"] == "class" and f["subject"] == "money":
        return "This mentions money, which is for the adults only."
    if f["kind"] == "class" and f["subject"] == "health":
        return "This mentions health, which stays with the person it's about."
    return "This mentions something kept between some people."


def fence_check(text: str, devices: list[dict[str, Any]]) -> str | None:
    """titan-node#917: a room's screens are a shared, household audience. The text goes there only if every fence it
    touches admits that audience; otherwise the reason (no content). Follows the fences module's own switch."""
    import fences
    held = []
    for d in devices or [{"id": None}]:
        held = fences.get().blocking(f"screen:{d.get('id')}", "household", text)
        if held:
            break
    return fence_why(held) if held else None


def send(sender: str, role: str | None, body: Any, people: list[dict[str, Any]], *,
         now: float | None = None, hold: Callable[[str], dict[str, Any] | None] | None = None,
         fenced: Callable[[str, list[dict[str, Any]]], str | None] | None = fence_check) -> tuple[int, dict[str, Any]]:
    """POST /v1/handoff: (status, body). `hold` (slice 2, room_state.hold): a room that's asleep answers 409 with
    the rule that held it, until the sender sends again with `anyway: true`."""
    now = time.time() if now is None else now
    if role not in PEOPLE_ROLES or not sender:
        return 403, {"error": "a person continues on another screen; a shared device doesn't send"}
    if not isinstance(body, dict):
        return 400, {"error": "expected a JSON object"}
    kind = str(body.get("kind") or "reply")
    if kind not in KINDS:
        return 400, {"error": "kind is reply"}
    text = str(body.get("text") or "").strip()
    if not text:
        return 400, {"error": "text is what goes on the screen"}
    by_room = screens(people)
    target = by_room.get(_room_key(body.get("to_room")))
    if not target:
        return 404, {"error": "there's no screen in that room", "rooms": [r["room"] for r in rooms(people)]}
    # Fences first (titan-node#917): a room's screens are a household audience, and there's no "anyway" past a fence.
    # The way through is the owner sharing it explicitly, as everywhere else.
    why = fenced(text, target) if fenced is not None else None
    if why:
        return 403, {"held": "fence", "room": target[0]["room"], "why": why}
    if hold is not None and body.get("anyway") is not True:
        held = hold(target[0]["room"])
        if held:
            return 409, held
    name = next((str(p.get("name") or "") for p in people if str(p.get("id")) == sender), "") or "Someone"
    hid = secrets.token_hex(8)
    title = " ".join(str(body.get("title") or "").split())[:MAX_TITLE]
    sweep(now)
    with _lock:
        con = _db()
        with con:
            con.execute("INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                        (hid, sender, name, target[0]["room"], json.dumps([d["id"] for d in target]), kind, title,
                         text[:MAX_TEXT], str(body.get("thread_id") or "")[:80] or None, now, now + TTL_SECONDS))
        con.close()
    return 201, {"id": hid, "room": target[0]["room"], "screens": [d["name"] for d in target], "expires_at": now + TTL_SECONDS}


def for_device(device_id: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """GET /v1/handoff/self: what was sent to this screen and is still on, newest first."""
    now = time.time() if now is None else now
    sweep(now)
    with _lock:
        con = _db()
        rows = con.execute("SELECT * FROM handoffs WHERE expires_at>? AND done_by IS NULL ORDER BY created_at DESC",
                           (now,)).fetchall()
        con.close()
    out = []
    for r in rows:
        if device_id not in json.loads(r["devices"] or "[]"):
            continue
        out.append({"id": r["id"], "from": str(r["sender_name"]).split(" ")[0], "kind": r["kind"],
                    "title": r["title"] or None, "text": r["text"], "at": r["created_at"], "expires_at": r["expires_at"]})
    return out


def sent_by(sender: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """The sender's own live hand-offs (to take one back)."""
    now = time.time() if now is None else now
    with _lock:
        con = _db()
        rows = con.execute("SELECT id, room, title, created_at, expires_at FROM handoffs WHERE sender=? AND expires_at>? "
                           "AND done_by IS NULL ORDER BY created_at DESC", (sender, now)).fetchall()
        con.close()
    return [dict(r) for r in rows]


def end(hid: str, *, sender: str | None = None, device_id: str | None = None, now: float | None = None) -> bool:
    """Done on the screen it went to, or taken back by its sender. Anyone else: no."""
    now = time.time() if now is None else now
    with _lock:
        con = _db()
        r = con.execute("SELECT * FROM handoffs WHERE id=? AND done_by IS NULL AND expires_at>?", (hid, now)).fetchone()
        ok = bool(r) and ((sender is not None and r["sender"] == sender)
                          or (device_id is not None and device_id in json.loads(r["devices"] or "[]")))
        if ok:
            with con:
                con.execute("UPDATE handoffs SET done_by=? WHERE id=?", ("sender" if sender else "screen", hid))
        con.close()
    if ok:
        sweep(now)
    return ok
