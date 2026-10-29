#!/usr/bin/env python3
"""Sitter and guest mode on a household TV (jellybean#428): for an evening, the TV shows the sitter what they need
(the kids' day, the contacts the adults chose, how the house works, Wi-Fi) and nothing else, then goes back to
normal by itself.

  sheet      what the adults keep for a sitter: contacts [{name, role, phone}], basics [{title, text}],
             kids [{name, bedtime?, notes?}] (notes are where a parent may add an allergy: health facts appear only
             when a parent writes them here, never pulled from the fenced store), wifi {network, password}.
             Its own 0600 file, never the KB or memory. No tool reads it and it never enters a prompt or a turn's
             context; the Wi-Fi password is registered with the secret vault (known_secrets) so it's masked if it
             ever appears in text sent out. Adults read and replace it (GET/PUT /v1/home/sitter/sheet).
  start      an adult (POST /v1/home/sitter {device_id?, kind?, until?}) starts it now; a household TV asks
             (POST from the TV's token), and each adult at home confirms on their phone (card source "sitter",
             actions start / decline), like the TV unlock (tv_unlock.at_home). Default end: midnight.
  on         GET /v1/home/sitter (the TV's own token): {state: off|pending|on, kind?, ends_at?, kids_today?,
             contacts?, basics?, kids?, wifi?}: the sheet only while on. kids_today is Home's household-visible
             today (home_screen.gather for a household device) filtered to the kids' first names, never an adult's
             events or where the parents are. Guest mode: basics and wifi only.
  blocked    while on, that TV's other routes answer 403 sitter_mode (enforced in the API's middleware), so leaving
             the screen shows nothing of the household.
  ends       ends_at; an adult's DELETE /v1/home/sitter (or Lock on their card). Never the TV alone.
  audit      every session is a row: which TV, kind, who started it (or approved the TV's ask), when, how it ended.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

DB = Path(os.environ.get("SITTER_DB", "/ai/datasets/connectors/sitter.db"))
REQUEST_TTL = 90
MAX_HOURS = 16
KINDS = ("sitter", "guest")
ADULTS = ("admin", "adult")
LIMITS = {"contacts": 8, "basics": 12, "kids": 8}
# What a TV may still reach while sitter mode is on: the sitter screen itself and pairing housekeeping.
ALLOWED_WHILE_ON = (("GET", "/v1/home/sitter"), ("POST", "/v1/home/sitter"), ("GET", "/v1/me"))

_lock = threading.Lock()
_on_cache: dict[str, float] = {}      # device id → ends_at, for the middleware's per-request check
_cache_loaded = False


def _db_path() -> Path:
    return Path(os.environ.get("SITTER_DB") or DB)


def _db() -> sqlite3.Connection:
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
    os.chmod(p, 0o600)
    con = sqlite3.connect(p)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS sheet (id INTEGER PRIMARY KEY CHECK (id = 1), body TEXT, updated_at REAL,"
                " updated_by TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, device_id TEXT, device_name TEXT, kind TEXT,"
                " state TEXT, requested_at REAL, request_expires REAL, started_at REAL, ends_at REAL, started_by TEXT,"
                " ended_at REAL, end_reason TEXT, ended_by TEXT, card_id TEXT, sent_to TEXT)")
    return con


# ── the sheet ──

def _clean(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def normalize(body: Any) -> dict[str, Any]:
    """The sheet as stored: known fields only, trimmed and capped."""
    b = body if isinstance(body, dict) else {}
    contacts = [{"name": _clean(c.get("name"), 60), "role": _clean(c.get("role"), 60), "phone": _clean(c.get("phone"), 30)}
                for c in (b.get("contacts") or []) if isinstance(c, dict) and _clean(c.get("name"), 60)]
    basics = [{"title": _clean(x.get("title"), 60), "text": str(x.get("text") or "").strip()[:600]}
              for x in (b.get("basics") or []) if isinstance(x, dict) and _clean(x.get("title"), 60)]
    kids = []
    for k in b.get("kids") or []:
        if isinstance(k, dict) and _clean(k.get("name"), 40):
            row = {"name": _clean(k.get("name"), 40)}
            if _clean(k.get("bedtime"), 20):
                row["bedtime"] = _clean(k.get("bedtime"), 20)
            if str(k.get("notes") or "").strip():
                row["notes"] = str(k.get("notes")).strip()[:400]
            kids.append(row)
    out: dict[str, Any] = {"contacts": contacts[:LIMITS["contacts"]], "basics": basics[:LIMITS["basics"]],
                           "kids": kids[:LIMITS["kids"]]}
    w = b.get("wifi")
    if isinstance(w, dict) and _clean(w.get("network"), 64):
        out["wifi"] = {"network": _clean(w.get("network"), 64), "password": str(w.get("password") or "")[:128]}
    return out


def sheet() -> dict[str, Any]:
    with _lock:
        con = _db()
        r = con.execute("SELECT body, updated_at, updated_by FROM sheet WHERE id=1").fetchone()
        con.close()
    if not r:
        return normalize({})
    out = normalize(json.loads(r["body"] or "{}"))
    out["updated_at"], out["updated_by"] = r["updated_at"], r["updated_by"]
    return out


def save_sheet(body: Any, person: str | None, *, now: float | None = None) -> dict[str, Any]:
    """Replace the sheet. `wifi` left out keeps the stored one (an editor needn't send the password back); an explicit
    `wifi: null` clears it."""
    now = time.time() if now is None else now
    s = normalize(body)
    if isinstance(body, dict) and "wifi" not in body:
        kept = sheet().get("wifi")
        if kept:
            s["wifi"] = kept
    with _lock:
        con = _db()
        with con:
            con.execute("INSERT INTO sheet (id, body, updated_at, updated_by) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE"
                        " SET body=excluded.body, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                        (json.dumps(s), now, person))
        con.close()
    return sheet()


def known_secrets() -> list[str]:
    """Values the secret vault masks wherever they appear: the Wi-Fi password."""
    try:
        pw = (sheet().get("wifi") or {}).get("password") or ""
    except Exception:  # noqa: BLE001
        return []
    return [pw] if len(pw) >= 3 else []


# ── sessions ──

def _midnight(now: float) -> float:
    """The household's next midnight (local_time's zone), never the host's: titan runs in UTC, where "midnight" is
    5–6 PM in the house."""
    import local_time
    z = local_time.zone()
    d = datetime.fromtimestamp(now, z) if z is not None else datetime.fromtimestamp(now).astimezone()
    return datetime.combine(d.date() + timedelta(days=1), datetime.min.time(), tzinfo=d.tzinfo).timestamp()


def _ends(until: Any, now: float) -> float:
    """`until`: epoch seconds or ISO; default midnight; at most MAX_HOURS ahead, at least 15 minutes."""
    t = None
    if isinstance(until, (int, float)):
        t = float(until)
    elif isinstance(until, str) and until.strip():
        try:
            dt = datetime.fromisoformat(until.strip())
            if dt.tzinfo is None:   # a clock time with no zone is the household's, not the host's (UTC)
                import local_time
                z = local_time.zone()
                dt = dt.replace(tzinfo=z) if z is not None else dt.astimezone()
            t = dt.timestamp()
        except ValueError:
            t = None
    t = t if t is not None else _midnight(now)
    return max(now + 15 * 60, min(now + MAX_HOURS * 3600, t))


def _reload_cache(now: float) -> None:
    global _cache_loaded
    con = _db()
    rows = con.execute("SELECT device_id, ends_at FROM sessions WHERE state='on' AND ended_at IS NULL").fetchall()
    con.close()
    _on_cache.clear()
    for r in rows:
        if r["ends_at"] > now:
            _on_cache[r["device_id"]] = max(_on_cache.get(r["device_id"], 0), r["ends_at"])
    _cache_loaded = True


def is_on(device_id: str | None, *, now: float | None = None) -> bool:
    """For the middleware: is sitter mode on for this TV? Cached; refreshed on every change."""
    if not device_id:
        return False
    now = time.time() if now is None else now
    with _lock:
        if not _cache_loaded:
            _reload_cache(now)
        return _on_cache.get(device_id, 0) > now


def blocked(device_id: str | None, method: str, path: str, *, now: float | None = None) -> bool:
    """A household TV in sitter mode reaches only the sitter screen."""
    return is_on(device_id, now=now) and (method.upper(), path) not in ALLOWED_WHILE_ON


def _close_expired(now: float) -> None:
    with _lock:
        con = _db()
        with con:
            con.execute("UPDATE sessions SET ended_at=ends_at, end_reason='expired', state='ended' WHERE state='on' AND"
                        " ended_at IS NULL AND ends_at<=?", (now,))
            con.execute("UPDATE sessions SET state='expired' WHERE state='pending' AND request_expires<=?", (now,))
        _reload_cache(now)
        con.close()


def _start_row(device: dict[str, Any], kind: str, ends_at: float, person: str, now: float, sid: str | None = None) -> str:
    """Sitter mode on now (any earlier session on that TV ends)."""
    sid = sid or secrets.token_hex(8)
    with _lock:
        con = _db()
        with con:
            con.execute("UPDATE sessions SET state='ended', ended_at=?, end_reason='replaced' WHERE device_id=? AND"
                        " state='on' AND ended_at IS NULL", (now, device["id"]))
            if con.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                con.execute("UPDATE sessions SET state='on', started_at=?, ends_at=?, started_by=? WHERE id=?",
                            (now, ends_at, person, sid))
            else:
                con.execute("INSERT INTO sessions (id, device_id, device_name, kind, state, requested_at, started_at,"
                            " ends_at, started_by) VALUES (?,?,?,?,?,?,?,?,?)",
                            (sid, device["id"], device.get("name") or "TV", kind, "on", now, now, ends_at, person))
        _reload_cache(now)
        con.close()
    return sid


def start(device: dict[str, Any], person: str, role: str | None, *, kind: str = "sitter", until: Any = None,
          now: float | None = None) -> tuple[int, dict[str, Any]]:
    """An adult turns it on for a TV."""
    now = time.time() if now is None else now
    if role not in ADULTS:
        return 403, {"error": "Only an adult can start sitter mode.", "code": "not_adult"}
    kind = kind if kind in KINDS else "sitter"
    ends_at = _ends(until, now)
    sid = _start_row(device, kind, ends_at, person, now)
    return 200, {"id": sid, "state": "on", "kind": kind, "ends_at": ends_at}


def request(device: dict[str, Any], people: list[dict[str, Any]], *, kind: str = "sitter", until: Any = None,
            now: float | None = None, add_card: Any = None, where: Any = None) -> tuple[int, dict[str, Any]]:
    """The TV asks; the adults at home confirm on their phones."""
    now = time.time() if now is None else now
    import tv_unlock
    if str(device.get("platform") or "") not in tv_unlock.TV_PLATFORMS:
        return 403, {"error": "Only a household TV asks for sitter mode.", "code": "not_tv"}
    kind = kind if kind in KINDS else "sitter"
    asked = tv_unlock.at_home(people, where)
    if not asked:
        return 409, {"error": "There's no adult in the household to ask.", "code": "no_adult"}
    ends_at = _ends(until, now)
    if add_card is None:
        import proactive
        add_card = proactive.add_card
    sid = secrets.token_hex(8)
    name = str(device.get("name") or "TV")
    label = "sitter mode" if kind == "sitter" else "guest mode"
    import local_time
    until_words = local_time.clock(ends_at)   # the household's clock, never titan's UTC
    card = add_card(f"Turn on {label} on the {name}?",
                    f"Until {until_words}. It shows what you've set for a {('sitter' if kind == 'sitter' else 'guest')}, "
                    "and nothing else of the household.", kind="info", urgency=2, source="sitter", to=asked,
                    key=f"sitter:{sid}", expires_in=REQUEST_TTL,
                    actions=[{"id": "start", "label": "Turn on", "kind": "reply", "value": f"start:{sid}"},
                             {"id": "decline", "label": "Not now", "kind": "reply", "value": f"decline:{sid}"}],
                    data={"kind": "sitter_request", "session_id": sid, "device_name": name, "mode": kind,
                          "ends_at": ends_at})
    with _lock:
        con = _db()
        with con:
            con.execute("UPDATE sessions SET state='replaced' WHERE device_id=? AND state='pending'", (device["id"],))
            con.execute("INSERT INTO sessions (id, device_id, device_name, kind, state, requested_at, request_expires,"
                        " ends_at, card_id, sent_to) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, device["id"], name, kind, "pending", now, now + REQUEST_TTL, ends_at,
                         str(card.get("id") or ""), json.dumps(asked)))
        con.close()
    return 202, {"id": sid, "state": "pending", "kind": kind, "expires_at": now + REQUEST_TTL, "asked": len(asked)}


def answer(value: str, person: str | None, role: str | None, *, now: float | None = None) -> tuple[bool, str]:
    """A tap on the phone's card: start:<id> / decline:<id> / end:<id>. (done, what to say)."""
    now = time.time() if now is None else now
    verb, _, sid = str(value or "").partition(":")
    who = str(person or "")
    if role not in ADULTS or not who:
        return False, "Only an adult can do that."
    if verb == "end":
        return (True, "Sitter mode is off.") if end_session(sid, who, role, now=now) else (False, "It's already off.")
    with _lock:
        con = _db()
        r = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        con.close()
    if not r or who not in json.loads(r["sent_to"] or "[]"):
        return False, "That request isn't there any more."
    if verb == "decline":
        return True, "OK, the TV stays as it is."
    if verb != "start":
        return False, "titan didn't understand that."
    if r["state"] != "pending" or now > (r["request_expires"] or 0):
        return False, "That request ran out. Ask again on the TV."
    _start_row({"id": r["device_id"], "name": r["device_name"]}, r["kind"], max(r["ends_at"], now + 15 * 60), who, now,
               sid=sid)
    return True, f"{'Sitter' if r['kind'] == 'sitter' else 'Guest'} mode is on for the {r['device_name']}."


def end_session(sid: str | None, person: str | None, role: str | None, *, device_id: str | None = None,
                reason: str = "ended", now: float | None = None) -> int:
    """An adult ends it: one session by id, or every one on a TV (device_id), or all of them (neither)."""
    now = time.time() if now is None else now
    if role not in ADULTS:
        return 0
    q, args = "UPDATE sessions SET state='ended', ended_at=?, end_reason=?, ended_by=? WHERE state='on' AND ended_at IS NULL", \
        [now, reason, person]
    if sid:
        q, args = q + " AND id=?", args + [sid]
    if device_id:
        q, args = q + " AND device_id=?", args + [device_id]
    with _lock:
        con = _db()
        with con:
            n = con.execute(q, args).rowcount
        _reload_cache(now)
        con.close()
    return n


def state(device_id: str, *, now: float | None = None, kids_today: Callable[[list[str]], list[dict]] | None = None) -> dict[str, Any]:
    """The TV's view: {state, kind?, ends_at?, ...the sheet while on}."""
    now = time.time() if now is None else now
    _close_expired(now)
    with _lock:
        con = _db()
        on = con.execute("SELECT * FROM sessions WHERE device_id=? AND state='on' AND ended_at IS NULL ORDER BY started_at"
                         " DESC LIMIT 1", (device_id,)).fetchone()
        pend = con.execute("SELECT * FROM sessions WHERE device_id=? AND state='pending' AND request_expires>? ORDER BY"
                           " requested_at DESC LIMIT 1", (device_id, now)).fetchone()
        con.close()
    if not on:
        return {"state": "pending", "kind": pend["kind"], "expires_at": pend["request_expires"]} if pend else {"state": "off"}
    s = sheet()
    out: dict[str, Any] = {"state": "on", "kind": on["kind"], "ends_at": on["ends_at"], "basics": s["basics"]}
    if s.get("wifi"):
        out["wifi"] = s["wifi"]
    if on["kind"] == "sitter":
        out["contacts"] = s["contacts"]
        out["kids"] = s["kids"]
        try:
            out["kids_today"] = (kids_today or _kids_today)([k["name"] for k in s["kids"]])
        except Exception:  # noqa: BLE001
            out["kids_today"] = []
    return out


def _kids_today(names: list[str]) -> list[dict[str, Any]]:
    """Home's household-visible today, the items that name a kid on the sheet (or a kid in the household)."""
    import home_screen
    import household_identity as hh
    kids = [str(p.get("name") or "").split(" ")[0] for p in hh.get_store().people() if p.get("role") == "kid"]
    firsts = {n.split(" ")[0].lower(): n.split(" ")[0] for n in list(names) + kids if n}
    return match_kids(home_screen.gather("household").get("today") or [], list(firsts.values()))


def match_kids(today: list[dict[str, Any]], firsts: list[str]) -> list[dict[str, Any]]:
    """Today's household items that name a kid ("Piano lesson: Ava", "Leo's dentist"), per kid."""
    out: dict[str, list[dict[str, Any]]] = {}
    for it in today:
        title = str(it.get("title") or "")
        for first in firsts:
            if first and re.search(r"\b" + re.escape(first) + r"(?:'s|’s)?\b", title, re.I):
                out.setdefault(first, []).append({"at": it.get("at"), "what": title})
    return [{"name": n, "items": items} for n, items in out.items()]


def audit(days: float = 30, *, now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    _close_expired(now)
    with _lock:
        con = _db()
        rows = con.execute("SELECT id, device_id, device_name, kind, state, requested_at, started_at, ends_at, started_by,"
                           " ended_at, end_reason, ended_by FROM sessions WHERE requested_at>=? ORDER BY requested_at DESC",
                           (now - days * 86400,)).fetchall()
        con.close()
    return [dict(r) for r in rows]
