#!/usr/bin/env python3
"""Unlock the TV with your phone (jellybean#427): an adult's phone briefly opens a grown-ups' layer on a household TV
(their own calendar, cards and bills), then it locks again by itself.

  ask        the TV (its own household token) asks: POST /v1/tv-unlocks/request. A request lives REQUEST_TTL s, one
             per TV (a new ask replaces it).
  confirm    each adult at home (their latest place event: arrived home; nobody known at home: every adult) gets a
             card with a push, source "tv-unlock", data {kind: tv_unlock, request_id, device_name, room, minutes},
             actions unlock / decline. The first Unlock wins; the card retires for everyone.
  unlock     {id, device_id, person_id, started_at, expires_at}: at most MAX_MINUTES. The TV never gets the adult's
             token: titan answers the TV's layer request while the unlock is live, with only that adult's own things.
  layer      GET /v1/tv-unlocks/self/layer: {person, expires_at, calendar, cards, bills}, read-only, computed for that
             adult (their fences apply); 403 the moment it isn't live.
  ends       expires_at; the TV's Lock (DELETE self: lock_tv, kid_profile, ambient, sleep); the adult's Lock now on
             the phone (lock_phone); the adult leaving home (left_home); a new unlock on that TV (replaced).
  audit      every unlock is a row: who, which TV, start, end and why. GET /v1/tv-unlocks (admin).
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

DB = Path(os.environ.get("TV_UNLOCK_DB", "/ai/datasets/connectors/tv_unlock.db"))
REQUEST_TTL = 90
DEFAULT_MINUTES = 5
MAX_MINUTES = 10
ENDED_SHOWN = 60          # `self` still says why it locked for this long
ADULTS = ("admin", "adult")
TV_PLATFORMS = ("tvos",)
END_REASONS = ("expired", "lock_tv", "kid_profile", "ambient", "sleep", "lock_phone", "left_home", "replaced")
TV_REASONS = ("lock_tv", "kid_profile", "ambient", "sleep")

_lock = threading.Lock()


def _db_path() -> Path:
    return Path(os.environ.get("TV_UNLOCK_DB") or DB)


def _db() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, device_id TEXT, device_name TEXT, room TEXT,"
                " minutes INTEGER, created_at REAL, expires_at REAL, state TEXT, card_id TEXT, sent_to TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS unlocks (id TEXT PRIMARY KEY, device_id TEXT, device_name TEXT,"
                " person_id TEXT, person_name TEXT, started_at REAL, expires_at REAL, ended_at REAL, end_reason TEXT,"
                " card_id TEXT)")
    return con


def _first(name: Any) -> str:
    return str(name or "").split(" ")[0] or "Someone"


def _call(pid: str, name: Any) -> str:
    """What the TV greets them as (#1080): their call_me, else the first name."""
    try:
        import household_identity as hh
        return hh.get_store().call_me(pid) or _first(name)
    except Exception:  # noqa: BLE001 - the greeting never blocks an unlock
        return _first(name)


# ── asking ──

def at_home(people: list[dict[str, Any]], where: Callable[[str], dict | None] | None = None) -> list[str]:
    """The adults to ask: those whose latest place event is arriving home; every adult when titan knows of nobody."""
    if where is None:
        import places
        where = places.where_now
    adults = [str(p["id"]) for p in people if p.get("role") in ADULTS]
    home = []
    for pid in adults:
        try:
            w = where(pid) or {}
        except Exception:  # noqa: BLE001
            w = {}
        if w.get("event") == "arrived" and str(w.get("place") or "").lower() == "home":
            home.append(pid)
    return home or adults


def request(device: dict[str, Any], people: list[dict[str, Any]], *, minutes: int = DEFAULT_MINUTES,
            now: float | None = None, add_card: Any = None, where: Any = None) -> tuple[int, dict[str, Any]]:
    """The TV's ask. (status, body): 202 {request_id, expires_at, asked}."""
    now = time.time() if now is None else now
    if str(device.get("platform") or "") not in TV_PLATFORMS:
        return 403, {"error": "only a household TV unlocks this way"}
    minutes = max(1, min(MAX_MINUTES, int(minutes or DEFAULT_MINUTES)))
    asked = at_home(people, where)
    if not asked:
        return 409, {"error": "there's no adult in the household to ask"}
    if add_card is None:
        import proactive
        add_card = proactive.add_card
    rid = secrets.token_hex(8)
    name = str(device.get("name") or "TV")
    room = str(device.get("room") or "")
    _retire_pending(str(device["id"]))
    card = add_card(f"Unlock the {name}?", f"Show your calendar, cards and bills on it for {minutes} minutes. It locks "
                    "by itself.", kind="info", urgency=2, source="tv-unlock", to=asked, key=f"tv-unlock:{rid}",
                    expires_in=REQUEST_TTL,
                    actions=[{"id": "unlock", "label": "Unlock", "kind": "reply", "value": f"unlock:{rid}"},
                             {"id": "decline", "label": "Not now", "kind": "reply", "value": f"decline:{rid}"}],
                    data={"kind": "tv_unlock", "request_id": rid, "device_name": name, "room": room or None,
                          "minutes": minutes})
    with _lock:
        con = _db()
        with con:
            con.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rid, str(device["id"]), name, room, minutes, now, now + REQUEST_TTL, "pending",
                         str(card.get("id") or ""), json.dumps(asked)))
        con.close()
    return 202, {"request_id": rid, "expires_at": now + REQUEST_TTL, "asked": len(asked)}


def _retire_pending(device_id: str) -> None:
    with _lock:
        con = _db()
        rows = con.execute("SELECT id, card_id FROM requests WHERE device_id=? AND state='pending'", (device_id,)).fetchall()
        with con:
            con.execute("UPDATE requests SET state='replaced' WHERE device_id=? AND state='pending'", (device_id,))
        con.close()
    for r in rows:
        _ack(r["card_id"])


def _ack(card_id: str | None, person: str | None = None) -> None:
    if not card_id:
        return
    try:
        import proactive
        proactive.ack_card(card_id, person=person)
    except Exception:  # noqa: BLE001
        pass


# ── answering (proactive.card_action, source "tv-unlock") ──

def answer(value: str, person: str | None, role: str | None, people: list[dict[str, Any]] | None = None, *,
           now: float | None = None, add_card: Any = None) -> tuple[bool, str]:
    """A tap on the phone's card: unlock:<request> / decline:<request> / lock:<unlock>. (done, what to say)."""
    now = time.time() if now is None else now
    verb, _, ref = str(value or "").partition(":")
    who = str(person or "")
    if verb == "lock":
        return (True, "Locked.") if end_unlock(ref, who, "lock_phone", now=now) else (False, "That's already locked.")
    if role not in ADULTS or not who:
        return False, "Only an adult can unlock the TV."
    with _lock:
        con = _db()
        r = con.execute("SELECT * FROM requests WHERE id=?", (ref,)).fetchone()
        con.close()
    if not r or who not in json.loads(r["sent_to"] or "[]"):
        return False, "That request isn't there any more."
    if verb == "decline":
        _ack(r["card_id"], person=who)
        return True, "OK, it stays locked."
    if verb != "unlock":
        return False, "titan didn't understand that."
    if r["state"] != "pending" or now > r["expires_at"]:
        return False, "That request ran out. Ask again on the TV."
    if people is None:
        import household_identity as hh
        people = hh.get_store().people()
    name = next((p.get("name") for p in people if str(p.get("id")) == who), who)
    uid = secrets.token_hex(8)
    expires = now + int(r["minutes"]) * 60
    # Any unlock already on that TV ends: one adult's things at a time.
    for live in _live_on(r["device_id"], now):
        end_unlock(live["id"], None, "replaced", now=now)
    with _lock:
        con = _db()
        with con:
            con.execute("UPDATE requests SET state='granted' WHERE id=? AND state='pending'", (ref,))
            con.execute("INSERT INTO unlocks (id, device_id, device_name, person_id, person_name, started_at, expires_at)"
                        " VALUES (?,?,?,?,?,?,?)", (uid, r["device_id"], r["device_name"], who, name, now, expires))
        con.close()
    _ack(r["card_id"])   # the other adults' copy retires too
    if add_card is None:
        import proactive
        add_card = proactive.add_card
    import local_time
    until = local_time.clock(expires, "%-I:%M")   # the household's clock, never titan's UTC
    try:
        card = add_card(f"{r['device_name']} is showing your things", f"Until {until}. It locks by itself.", kind="info",
                        urgency=1, source="tv-unlock", to=[who], key=f"tv-unlock-on:{uid}", expires_in=expires - now,
                        actions=[{"id": "lock", "label": "Lock now", "kind": "reply", "value": f"lock:{uid}"}],
                        data={"kind": "tv_unlock_active", "unlock_id": uid, "device_name": r["device_name"],
                              "expires_at": expires})
        with _lock:
            con = _db()
            with con:
                con.execute("UPDATE unlocks SET card_id=? WHERE id=?", (str(card.get("id") or ""), uid))
            con.close()
    except Exception:  # noqa: BLE001
        pass
    return True, f"Unlocked the {r['device_name']} for {int(r['minutes'])} minutes."


# ── ending ──

def _live_on(device_id: str, now: float) -> list[sqlite3.Row]:
    with _lock:
        con = _db()
        rows = con.execute("SELECT * FROM unlocks WHERE device_id=? AND ended_at IS NULL", (device_id,)).fetchall()
        con.close()
    out = []
    for r in rows:
        if now >= r["expires_at"]:
            _close(r, "expired", r["expires_at"])
        else:
            out.append(r)
    return out


def _close(r: sqlite3.Row, reason: str, at: float) -> None:
    with _lock:
        con = _db()
        with con:
            con.execute("UPDATE unlocks SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL", (at, reason, r["id"]))
        con.close()
    _ack(r["card_id"])


def end_unlock(uid: str, person: str | None, reason: str, *, now: float | None = None) -> bool:
    """End one unlock. `person`: only that adult's own (the phone's Lock now); None: titan's own ending."""
    now = time.time() if now is None else now
    with _lock:
        con = _db()
        r = con.execute("SELECT * FROM unlocks WHERE id=? AND ended_at IS NULL", (uid,)).fetchone()
        con.close()
    if not r or (person is not None and r["person_id"] != person):
        return False
    _close(r, reason if now < r["expires_at"] else "expired", min(now, r["expires_at"]))
    return True


def end_on_device(device_id: str, reason: str = "lock_tv", *, now: float | None = None) -> int:
    """The TV's Lock (or a kid's profile, Ambient, sleep): everything on that TV ends, and a pending ask is withdrawn."""
    now = time.time() if now is None else now
    reason = reason if reason in TV_REASONS else "lock_tv"
    _retire_pending(device_id)
    live = _live_on(device_id, now)
    for r in live:
        _close(r, reason, now)
    return len(live)


def end_for_person(person: str, reason: str = "left_home", *, now: float | None = None) -> int:
    """The adult walked away (their phone left home): every TV showing their things locks."""
    now = time.time() if now is None else now
    with _lock:
        con = _db()
        rows = con.execute("SELECT * FROM unlocks WHERE person_id=? AND ended_at IS NULL", (person,)).fetchall()
        con.close()
    for r in rows:
        _close(r, reason if now < r["expires_at"] else "expired", min(now, r["expires_at"]))
    return len(rows)


# ── what the TV sees ──

def state(device_id: str, *, now: float | None = None) -> dict[str, Any]:
    """{state: none|pending|active, person?, expires_at?, ended?: {reason, at}} for the TV's own polling."""
    now = time.time() if now is None else now
    live = _live_on(device_id, now)
    if live:
        r = live[0]
        return {"state": "active", "person": {"id": r["person_id"], "first_name": _first(r["person_name"]),
                                                       "call_me": _call(r["person_id"], r["person_name"])},
                "expires_at": r["expires_at"]}
    with _lock:
        con = _db()
        p = con.execute("SELECT * FROM requests WHERE device_id=? AND state='pending' AND expires_at>? "
                        "ORDER BY created_at DESC LIMIT 1", (device_id, now)).fetchone()
        last = con.execute("SELECT end_reason, ended_at FROM unlocks WHERE device_id=? AND ended_at IS NOT NULL "
                           "ORDER BY ended_at DESC LIMIT 1", (device_id,)).fetchone()
        con.close()
    out: dict[str, Any] = {"state": "pending", "expires_at": p["expires_at"]} if p else {"state": "none"}
    if last and now - last["ended_at"] <= ENDED_SHOWN:
        out["ended"] = {"reason": last["end_reason"], "at": last["ended_at"]}
    return out


def layer(device_id: str, *, now: float | None = None, people: list[dict[str, Any]] | None = None,
          calendar: Callable[[str, str], list[dict]] | None = None, cards: Callable[[str, str], list[dict]] | None = None,
          bills: Callable[[str], list[dict]] | None = None) -> dict[str, Any] | None:
    """That adult's own things for the TV while the unlock is live; None the moment it isn't."""
    now = time.time() if now is None else now
    live = _live_on(device_id, now)
    if not live:
        return None
    r = live[0]
    pid = r["person_id"]
    if people is None:
        import household_identity as hh
        people = hh.get_store().people()
    role = next((str(p.get("role")) for p in people if str(p.get("id")) == pid), "adult")
    out = {"person": {"id": pid, "first_name": _first(r["person_name"]), "call_me": _call(pid, r["person_name"])}, "expires_at": r["expires_at"],
           "calendar": [], "cards": [], "bills": []}
    for key, fn, args in (("calendar", calendar or _calendar, (pid, role)), ("cards", cards or _cards, (pid, role)),
                          ("bills", bills or _bills, (pid,))):
        try:
            out[key] = fn(*args)
        except Exception:  # noqa: BLE001
            out[key] = []
    # Checked again after the reads: an unlock that ended meanwhile hands over nothing.
    return out if _live_on(device_id, time.time() if now is None else now) else None


def _calendar(pid: str, role: str) -> list[dict[str, Any]]:
    """Today's and tomorrow's events that are this adult's own (their fences apply: they're the viewer)."""
    import local_time
    import schedule_view
    from datetime import timedelta
    today = local_time.today()
    view = schedule_view.for_viewer(today, today + timedelta(days=1), pid, role, {pid})
    rows = next((p.get("events") or [] for p in view.get("people") or [] if p.get("id") == pid), [])
    keep = ("id", "title", "start", "end", "all_day", "location", "calendar")
    return [{k: e.get(k) for k in keep if e.get(k) is not None} for e in rows]


def _cards(pid: str, role: str) -> list[dict[str, Any]]:
    """Their open cards in v1/proactive's shape, without buttons (read-only), titan's engineering group and this
    feature's own cards left out."""
    import proactive
    keep = ("id", "source", "kind", "title", "detail", "urgency", "created_at", "expires_at", "from", "data")
    out = []
    for c in proactive.list_cards(person=pid, role=role):
        if proactive.card_group(c.get("source")) == "engineering" or c.get("source") == "tv-unlock":
            continue
        if c.get("to") in ("household",):
            continue   # the TV shows the household's cards already
        out.append({k: c.get(k) for k in keep if c.get(k) is not None})
    return out[:20]


def _bills(pid: str) -> list[dict[str, Any]]:
    """Bills due that are this adult's own (the record's owner), soonest first."""
    import agent_runtime_lib as lib
    from datetime import date, timedelta
    nodes, _ = lib.record_nodes("bill")
    out = []
    import local_time
    floor = local_time.today() - timedelta(days=7)
    for n in nodes:
        d = n.get("data") or {}
        p = lib._node_prov(n)
        owner = p.get("owner") or None
        if owner != pid:
            continue
        status = str(d.get("payment_status") or ("due" if d.get("due_date") else "")).lower()
        due = str(d.get("due_date") or "")[:10]
        if status != "due" or not due:
            continue
        try:
            if date.fromisoformat(due) < floor:
                continue
        except ValueError:
            continue
        b = {"title": str(d.get("vendor") or "A bill"), "due": due}
        if d.get("amount") not in (None, ""):
            b["amount"] = d.get("amount")
        if d.get("currency"):
            b["currency"] = d.get("currency")
        out.append(b)
    return sorted(out, key=lambda b: b["due"])[:10]


# ── the audit ──

def audit(days: float = 30, *, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    _close_expired(now)
    with _lock:
        con = _db()
        rows = con.execute("SELECT id, device_id, device_name, person_id, started_at, expires_at, ended_at, end_reason "
                           "FROM unlocks WHERE started_at>=? ORDER BY started_at DESC", (now - days * 86400,)).fetchall()
        con.close()
    return [dict(r) for r in rows]


def _close_expired(now: float) -> None:
    with _lock:
        con = _db()
        rows = con.execute("SELECT * FROM unlocks WHERE ended_at IS NULL AND expires_at<=?", (now,)).fetchall()
        con.close()
    for r in rows:
        _close(r, "expired", r["expires_at"])
