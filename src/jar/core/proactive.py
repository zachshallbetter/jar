#!/usr/bin/env python3
"""Proactive cards and event triggers: the sense → triage → act → surface loop.

Sense:   anything (Jellybean collectors, a git watcher, a timer) POSTs an event to
         /v1/triggers: {source, kind, summary, detail?, dedupe_key?}.
Triage:  the LOCAL Decider-2B (:8031) drops events it is confident are noise. Everything else
         goes on, so a weak triage model can only save work, never lose a real event.
Act:     a background agent turn on the LOCAL brain with read-only tools investigates and reports
         in a few sentences, or answers NO_ACTION. Household data stays on titan (no Jev, no Gemini).
Surface: a result worth the user's attention becomes a proactive card (Jellybean polls
         GET /v1/proactive and shows it in its card stack) and a household timeline line.

The agent can also raise a card itself with the `notify_user` tool.

Cards to people and devices: a card has an audience (`to`): "admins" (the default, and every card
made before audiences existed), "adults", "household", or a list of person ids. GET /v1/proactive
and the long-poll GET /v1/cards/wait return only the cards the caller's person may see, and
dismissing a card dismisses it for that person only. A card sent with a `key` is updated in place
by the next card with the same key and audience (a laundry timer, a delivery on its way): same id,
new content, shown again. `expires_in` retires a card on its own. POST /v1/cards sends one (a
person from Jellybean, Home Assistant or a script with the admin token).
Store: SQLite at $PROACTIVE_DB (default /ai/datasets/connectors/proactive.db).
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import local_time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops_ports import CODE_MODEL_URL  # noqa: E402

DB_PATH = Path(os.environ.get("PROACTIVE_DB", "/ai/datasets/connectors/proactive.db"))
DECIDER_URL = os.environ.get("DECIDER_URL", "http://127.0.0.1:8031").rstrip("/")
CODE_MODEL_URL = os.environ.get("CODE_MODEL_URL", CODE_MODEL_URL).rstrip("/")
NOISE_CONFIDENCE = float(os.environ.get("TRIGGER_NOISE_CONFIDENCE", "0.8"))
MAX_TURNS_PER_HOUR = int(os.environ.get("TRIGGER_MAX_TURNS_PER_HOUR", "30"))
DEDUPE_SECONDS = 6 * 3600
KINDS = {"info", "reminder", "calendar", "alert", "suggestion", "report", "update"}
AUDIENCES = ("admins", "adults", "household")

_lock = threading.Lock()

_TRIAGE_QUESTIONS = {
    "actionable": {"type": "choice",
                   "instructions": "Does this event need the household agent to do or tell the user something?",
                   "criteria": {"yes": "worth looking into or telling the user about (bills, deadlines, errors, "
                                       "requests, messages from people, important changes)",
                                "no": "routine noise (autosaves, build output, caches, duplicates, trivial changes)"}},
    "urgency": {"type": "score", "instructions": "How soon should the user hear about this?",
                "criteria": ["not at all", "whenever convenient", "today", "right now"]},
}

_TRIGGER_PROMPT = """[AUTONOMOUS TRIGGER — no user is waiting]
An event arrived from {source} ({kind}): {summary}
{detail}
Investigate with your read-only tools only if that helps (knowledge base, records, timeline, host \
status, web). Then start your reply with ONE of these lines:
  NO_ACTION   — nothing worth keeping
  PASSIVE     — worth remembering, but the user doesn't need to look now
  NOTIFY      — the user should see this soon (a decision, a deadline, a problem, something they asked for)
After PASSIVE or NOTIFY, write the note: at most 3 short sentences, answer first, no preamble."""


_REVIEW_PROMPT = """You review a code change for the owner of this household's systems (titan, Jellybean \
and the runtimes). Be concrete and brief.

Recorded design decisions that may apply (from the knowledge base):
{decisions}

Change from {source}: {summary}
{detail}

Start your reply with ONE line:
  NO_ACTION   — trivial (formatting, docs typo, generated files)
  PASSIVE     — a normal change worth remembering
  NOTIFY      — a likely bug, regression, security problem, or a change that breaks a recorded decision
Then at most 3 short sentences: what changed and anything risky, naming files."""


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY, source TEXT, kind TEXT, title TEXT, detail TEXT, urgency INTEGER,
        action_prompt TEXT, created_at REAL, acked_at REAL)""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(cards)")]
    for col, decl in (("actions", "TEXT"), ("audience", "TEXT"), ("card_key", "TEXT"), ("updated_at", "REAL"),
                      ("expires_at", "REAL"), ("sender", "TEXT"), ("data", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE cards ADD COLUMN {col} {decl}")
    con.execute("""CREATE TABLE IF NOT EXISTS card_acks (
        card_id TEXT, person_id TEXT, acked_at REAL, PRIMARY KEY (card_id, person_id))""")
    # Universal card controls (#1039), per person: Later (a real snooze) and Stop these (a mute), and what each
    # person did with a source's cards (the turn report's noisy-source counts; mutes are triage's negatives).
    con.execute("""CREATE TABLE IF NOT EXISTS card_snoozes (
        card_id TEXT, person_id TEXT, role TEXT, until REAL, snoozed_at REAL, notified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (card_id, person_id))""")
    # Later on Now's items (#1233): the same per-person snooze, keyed by an items-surface id (todo:…, scheduled:…,
    # task:…). It hides the item from that person's Now only, until `until`.
    con.execute("""CREATE TABLE IF NOT EXISTS item_snoozes (
        item_id TEXT, person_id TEXT, role TEXT, until REAL, snoozed_at REAL, PRIMARY KEY (item_id, person_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_mutes (
        id TEXT PRIMARY KEY, person_id TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
        key_family TEXT NOT NULL DEFAULT '', label TEXT, created_at REAL, UNIQUE (person_id, source, kind, key_family))""")
    # Per-person source modes (quiet; stop is a source-wide mute) and per-device delivery levels (#1039, 10-06).
    con.execute("""CREATE TABLE IF NOT EXISTS card_source_modes (
        person_id TEXT NOT NULL, source TEXT NOT NULL, mode TEXT NOT NULL, set_at REAL, PRIMARY KEY (person_id, source))""")
    con.execute("""CREATE TABLE IF NOT EXISTS device_delivery (
        device_id TEXT PRIMARY KEY, person_id TEXT, level TEXT NOT NULL, set_at REAL)""")
    # Quiet hours (#1039, 10-06): a person's own, and a device's override (no row = the device follows its person).
    con.execute("""CREATE TABLE IF NOT EXISTS person_quiet (
        person_id TEXT PRIMARY KEY, quiet TEXT NOT NULL, set_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS device_quiet (
        device_id TEXT PRIMARY KEY, person_id TEXT, quiet TEXT NOT NULL, set_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_feedback (
        person_id TEXT, source TEXT, kind TEXT, key_family TEXT, signal TEXT, at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS triggers (
        id TEXT PRIMARY KEY, source TEXT, kind TEXT, summary TEXT, dedupe_key TEXT, received_at REAL,
        outcome TEXT, reason TEXT, card_id TEXT, finished_at REAL)""")
    tcols = [r[1] for r in con.execute("PRAGMA table_info(triggers)")]
    if "detail" not in tcols:
        con.execute("ALTER TABLE triggers ADD COLUMN detail TEXT")   # so interrupted work can be re-run
    for col in ("person", "role"):   # who a card reply came from: its outcome goes back to them
        if col not in tcols:
            con.execute(f"ALTER TABLE triggers ADD COLUMN {col} TEXT")
    return con


# ---------------------------------------------------------------------------- cards

ACTION_KINDS = {"check", "reply", "prompt", "dismiss"}
# A screen a client may open instead of acting (jellybean#425): `open` rides on an ordinary button (a prompt), so a
# client that knows the screen opens it and every other client does what the button always did. Not a new kind:
# older apps may mishandle one.
OPEN_HINTS = {"family-meeting"}


_ACTION_ID_RX = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def _clean_actions(actions: Any) -> list[dict[str, str]]:
    """Up to 3 buttons: {id, label, kind: check|reply|prompt|dismiss, value, open?}. Anything else is dropped. A
    button keeps its own id when it names an answer a client sends back (#446: allow_once / always / decline);
    otherwise a0, a1…"""
    out = []
    seen: set[str] = set()
    for i, a in enumerate(actions if isinstance(actions, list) else []):
        if not isinstance(a, dict) or a.get("kind") not in ACTION_KINDS or not str(a.get("label") or "").strip():
            continue
        own = str(a.get("id") or "")
        aid = own if _ACTION_ID_RX.match(own) and own not in seen else f"a{i}"
        seen.add(aid)
        b = {"id": aid, "label": str(a["label"]).strip()[:24], "kind": a["kind"], "value": str(a.get("value") or "")[:300]}
        if a.get("open") in OPEN_HINTS:
            b["open"] = a["open"]
        out.append(b)
    return out[:3]


def audience(to: Any) -> str:
    """Normalise `to` for storage (audience.py): "admins" | "adults" | "household" | a JSON list of ids."""
    import audience as _aud
    return _aud.normalize(to, default="admins")


def visible_to(aud: str | None, person_id: str | None, role: str | None) -> bool:
    """Whether a card with this audience is for this person (None: an internal caller sees all)."""
    import audience as _aud
    return _aud.visible(aud or "admins", person_id, role)


# Engineering cards (jellybean#289, Zach 10-02): code reviews, checks and the Mac's system signals. The apps fold
# them into one collapsible "Engineering" card; they push on their own only when urgent (3), never one by one.
ENGINEERING_SOURCES = ("trigger:git", "trigger:checks", "trigger:mac", "loop:checks")


def card_group(source: str | None) -> str | None:
    s = str(source or "")
    return "engineering" if s in ENGINEERING_SOURCES or s.startswith("checks") else None


_made = threading.local()


def track_cards() -> None:
    """Start noting the cards this thread creates or updates (an API turn: its reply's action blocks)."""
    _made.ids = []


def tracked_cards() -> list[str]:
    """The ids noted since track_cards, in the order they were made; tracking stops."""
    ids = list(getattr(_made, "ids", None) or [])
    _made.ids = None
    return ids


def cards_for_reply(ids: list[str], person: str | None, role: str | None, db: Path | None = None) -> list[dict[str, Any]]:
    """Of the cards a turn made (tracked_cards), the ones its asker sees now, in the order made: their audience
    includes the asker (never another person's card), every fence admits them, not yet acted on. None on a shared
    device: it has no personal cards (jellybean#201 slice 3)."""
    if not ids or not person or role not in ("admin", "adult", "kid"):
        return []
    seen = {c["id"]: c for c in list_cards(db=db, person=person, role=role)}
    return [seen[i] for i in ids if i in seen]


# ---------------------------------------------------------------------------- universal controls (#1039)

# Cards that can be snoozed but never muted: a person must not be able to silence a safety prompt for good.
SAFETY_SOURCES = frozenset({"lock-up", "home-security", "alarm", "security"})


def is_safety_source(source: Any) -> bool:
    """THE safety test for a card's source, everywhere: titan's push, can_mute, can_stop, quiet hours, urgency, and the
    phone (jellybean#1056): exactly one of SAFETY_SOURCES, read trimmed and lowercased. No prefix ("trigger:alarm",
    "device:alarm" are ordinary)."""
    return str(source or "").strip().lower() in SAFETY_SOURCES
# Cards whose buttons commit something: Done on them is "Not now", never an approval (the request stays pending).
APPROVAL_SOURCES = frozenset({"approve-cap", "mail-move", "mail-unsub", "item-consent", "lock-up", "model-switch", "tv-unlock",
                              "sitter", "world-proposal"})
# A card a source gave no expiry expires by its kind (an approval waits a week): an ignored card never re-nags.
DEFAULT_EXPIRES = {"info": 86400.0, "update": 86400.0, "calendar": 2 * 86400.0, "suggestion": 3 * 86400.0,
                   "report": 3 * 86400.0, "reminder": 7 * 86400.0, "alert": 7 * 86400.0}
APPROVAL_EXPIRES = 7 * 86400.0
SNOOZE_MAX = 30 * 86400.0
_FAMILY_TAIL = re.compile(r"[-_:](?:\d{4}-\d{2}(?:-\d{2})?|[0-9a-f]{6,}|\d+)$", re.I)


def key_family(key: str | None) -> str:
    """A card key's family: before its first ":", without trailing dates, ids or numbers
    ("bill-due:lumen-2026-10" → "bill-due", "coordination-2026-10-05" → "coordination")."""
    k = str(key or "").strip().lower().split(":", 1)[0]
    prev = None
    while prev != k:
        prev, k = k, _FAMILY_TAIL.sub("", k)
    return k


def default_expires(kind: str, source: str) -> float:
    return APPROVAL_EXPIRES if source in APPROVAL_SOURCES else DEFAULT_EXPIRES.get(kind, 86400.0)


def can_mute(card: dict[str, Any], role: str | None = None) -> bool:
    return not is_safety_source(card.get("source")) and role != "household"


def _mute_scope(card: dict[str, Any]) -> tuple[str, str, str]:
    fam = key_family(card.get("key"))
    return ("", "", fam) if fam else (str(card.get("source") or ""), str(card.get("kind") or ""), "")


def _muted(card: dict[str, Any], mutes: list[tuple[str, str, str]]) -> bool:
    fam = key_family(card.get("key"))
    for src, kind, mfam in mutes:
        if mfam and mfam == fam:
            return True
        if not mfam and src == card.get("source") and kind in (card.get("kind"), "*"):   # "*": the whole source (stop)
            return True
    return False


def _mutes_of(con: sqlite3.Connection, person: str | None) -> list[tuple[str, str, str]]:
    if not person:
        return []
    return [tuple(r) for r in con.execute("SELECT source, kind, key_family FROM card_mutes WHERE person_id=?", (person,))]


def _feedback(con: sqlite3.Connection, person: str, card: dict[str, Any], signal: str) -> None:
    con.execute("INSERT INTO card_feedback VALUES (?,?,?,?,?,?)", (person, card.get("source"), card.get("kind"),
                                                                   key_family(card.get("key")), signal, time.time()))


def _visible_card(card_id: str, person: str, role: str | None, db: Path | None) -> dict[str, Any] | None:
    return next((c for c in list_cards(include_acked=True, db=db, person=person, role=role, controls=False)
                 if c["id"] == card_id), None)


def snooze(card_id: str, until: float, *, person: str, role: str | None, db: Path | None = None,
           now: float | None = None) -> dict[str, Any] | None:
    """Later: hidden for this person until `until` (≤ 30 days), then it comes back once. None if they can't see it;
    ValueError for a time that isn't in the future or too far out."""
    now = time.time() if now is None else now
    if not person:
        raise ValueError("no person")
    until = float(until)
    if until <= now or until > now + SNOOZE_MAX:
        raise ValueError("until must be in the future and at most 30 days out")
    card = _visible_card(card_id, person, role, db)
    if not card:
        return None
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO card_snoozes VALUES (?,?,?,?,?,0)", (card_id, person, role, until, now))
            _feedback(con, person, card, "snooze")
            con.commit()
        finally:
            con.close()
    return {"id": card_id, "snoozed_until": until}


def snooze_item(item_id: str, until: float, *, person: str, role: str | None, db: Path | None = None,
                now: float | None = None) -> dict[str, Any]:
    """Later on a Now item (#1233): hidden from this person's Now until `until` (≤ 30 days, as a card's). The caller
    checked the person may see it; ValueError for no person or a time that isn't in the future or too far out.
    Snoozes that ended a day ago are dropped on the way."""
    now = time.time() if now is None else now
    if not person or role == "household":
        raise ValueError("no person")
    until = float(until)
    if until != until or until <= now or until > now + SNOOZE_MAX:   # NaN, past, or too far out
        raise ValueError("until must be in the future and at most 30 days out")
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO item_snoozes VALUES (?,?,?,?,?)", (item_id, person, role, until, now))
            con.execute("DELETE FROM item_snoozes WHERE until < ?", (now - 86400,))
            con.commit()
        finally:
            con.close()
    return {"id": item_id, "snoozed_until": until}


def snoozed_items(person: str | None, now: float | None = None, db: Path | None = None) -> set[str]:
    """The item ids this person put off with Later that are still hidden at `now` (#1233); none for no person."""
    if not person:
        return set()
    now = time.time() if now is None else now
    con = _db(db)
    try:
        return {r[0] for r in con.execute("SELECT item_id FROM item_snoozes WHERE person_id=? AND until > ?",
                                          (person, now))}
    finally:
        con.close()


def mute(card_id: str, *, person: str, role: str | None, db: Path | None = None) -> dict[str, Any] | None:
    """Stop these: this person stops seeing (and being pushed) cards in this card's scope; this one is dismissed for
    them. None if they can't see it; PermissionError for a safety card or a shared household screen."""
    card = _visible_card(card_id, person, role, db)
    if not card:
        return None
    if not person or not can_mute(card, role):
        raise PermissionError("cannot_mute")
    src, kind, fam = _mute_scope(card)
    label = (fam or f"{src} · {kind}")[:80]
    mid = "mu-" + hashlib.sha1(f"{person}|{src}|{kind}|{fam}".encode()).hexdigest()[:12]
    now = time.time()
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR IGNORE INTO card_mutes VALUES (?,?,?,?,?,?,?)", (mid, person, src, kind, fam, label, now))
            con.execute("INSERT OR IGNORE INTO card_acks (card_id, person_id, acked_at) VALUES (?,?,?)", (card_id, person, now))
            _feedback(con, person, card, "mute")   # a negative example for the proactive triage
            con.commit()
            row = con.execute("SELECT id, source, kind, key_family, label, created_at FROM card_mutes WHERE person_id=? "
                              "AND source=? AND kind=? AND key_family=?", (person, src, kind, fam)).fetchone()
        finally:
            con.close()
    return dict(zip(("id", "source", "kind", "key_family", "label", "created_at"), row))


def mutes(person: str, db: Path | None = None) -> list[dict[str, Any]]:
    con = _db(db)
    try:
        rows = con.execute("SELECT id, source, kind, key_family, label, created_at FROM card_mutes WHERE person_id=? "
                           "ORDER BY created_at DESC", (person,)).fetchall()
    finally:
        con.close()
    return [dict(zip(("id", "source", "kind", "key_family", "label", "created_at"), r)) for r in rows]


def unmute(person: str, mute_id: str, db: Path | None = None) -> bool:
    with _lock:
        con = _db(db)
        try:
            n = con.execute("DELETE FROM card_mutes WHERE id=? AND person_id=?", (mute_id, person)).rowcount
            con.commit()
        finally:
            con.close()
    return n > 0


# ── per-source modes and per-device delivery (#1039, 10-06) ──

MODES = ("notify", "quiet", "stop")
PERSON_LEVELS = ("all", "important", "off")
HOUSEHOLD_LEVELS = ("household", "off")


def source_mode(person: str | None, source: str, con: sqlite3.Connection | None = None, db: Path | None = None) -> str:
    """notify | quiet | stop for this person and source (stop: the source-wide mute)."""
    if not person:
        return "notify"
    own = con is None
    con = con or _db(db)
    try:
        if con.execute("SELECT 1 FROM card_mutes WHERE person_id=? AND source=? AND kind='*' AND key_family=''",
                       (person, source)).fetchone():
            return "stop"
        r = con.execute("SELECT mode FROM card_source_modes WHERE person_id=? AND source=?", (person, source)).fetchone()
        return r[0] if r and r[0] in MODES else "notify"
    finally:
        if own:
            con.close()


def set_source_mode(person: str, role: str | None, source: str, mode: str, db: Path | None = None) -> dict[str, str]:
    """ValueError for a bad mode or source; PermissionError("cannot_stop") for stopping a safety source or on a shared
    household screen. stop = a source-wide mute (listed with the others, undone by DELETE /v1/proactive/mutes)."""
    source = str(source or "").strip()[:80]
    if mode not in MODES or not source or not person:
        raise ValueError("mode must be notify, quiet or stop, for a named source")
    if mode == "stop" and (is_safety_source(source) or role == "household"):
        raise PermissionError("cannot_stop")
    now = time.time()
    mid = "mu-" + hashlib.sha1(f"{person}|{source}|*|".encode()).hexdigest()[:12]
    with _lock:
        con = _db(db)
        try:
            con.execute("DELETE FROM card_mutes WHERE person_id=? AND source=? AND kind='*' AND key_family=''", (person, source))
            con.execute("DELETE FROM card_source_modes WHERE person_id=? AND source=?", (person, source))
            if mode == "stop":
                con.execute("INSERT OR REPLACE INTO card_mutes VALUES (?,?,?,?,?,?,?)",
                            (mid, person, source, "*", "", f"{source} · all", now))
                con.execute("INSERT INTO card_feedback VALUES (?,?,?,?,?,?)", (person, source, "*", "", "mute", now))
            elif mode == "quiet":
                con.execute("INSERT INTO card_source_modes VALUES (?,?,?,?)", (person, source, "quiet", now))
            con.commit()
        finally:
            con.close()
    return {"source": source, "mode": mode}


def _week_start(now: float) -> float:
    import local_time
    from datetime import datetime, timedelta
    d = datetime.fromtimestamp(now, local_time.zone())
    monday = (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.timestamp()


def sources(person: str, role: str | None, db: Path | None = None, now: float | None = None) -> list[dict[str, Any]]:
    """Every source this person has had a visible card from in 30 days, or set a mode for, with their own counts since
    Monday: shown, done, snoozed, acted, expired (theirs, untouched). Never anyone else's counts."""
    now = time.time() if now is None else now
    week = _week_start(now)
    cards = [c for c in list_cards(now - 30 * 86400, True, db, person=person, role=role, controls=False)]
    # list_cards leaves expired cards out; this week's expired ones are counted from the table below
    con = _db(db)
    try:
        set_src = {r[0] for r in con.execute("SELECT source FROM card_source_modes WHERE person_id=?", (person,))} | \
            {r[0] for r in con.execute("SELECT source FROM card_mutes WHERE person_id=? AND kind='*'", (person,))}
        fb: dict[tuple[str, str], int] = {}
        for src, sig, n in con.execute("SELECT source, signal, COUNT(*) FROM card_feedback WHERE person_id=? AND at>=? "
                                       "GROUP BY source, signal", (person, week)):
            fb[(src, sig)] = n
        expired_rows = con.execute(
            "SELECT id, source, kind, title, detail, audience, card_key, sender FROM cards WHERE expires_at>=? AND "
            "expires_at<? AND acked_at IS NULL", (week, now)).fetchall()
        acked = {r[0] for r in con.execute("SELECT card_id FROM card_acks WHERE person_id=?", (person,))}
        modes = {s_: source_mode(person, s_, con) for s_ in ({c["source"] for c in cards} | set_src)}
    finally:
        con.close()
    expired: dict[str, int] = {}
    for cid, src, kind, title, detail, aud, key, sender in expired_rows:
        if cid in acked or not visible_to(aud or "admins", person, role):
            continue
        expired[src] = expired.get(src, 0) + 1
    shown: dict[str, int] = {}
    for c in cards:
        if (c.get("created_at") or 0) >= week:
            shown[c["source"]] = shown.get(c["source"], 0) + 1
    out = []
    for src, mode in modes.items():
        out.append({"source": src, "label": src.replace("-", " ").replace(":", " · "), "mode": mode,
                    "can_stop": not is_safety_source(src) and role != "household",
                    "this_week": {"shown": shown.get(src, 0), "done": fb.get((src, "done"), 0),
                                  "snoozed": fb.get((src, "snooze"), 0), "acted": fb.get((src, "acted"), 0),
                                  "expired": expired.get(src, 0)}})
    return sorted(out, key=lambda r: (-r["this_week"]["shown"], r["source"]))


def device_level(device_id: str | None, role: str | None, db: Path | None = None) -> str:
    default = "household" if role == "household" else "all"
    if not device_id:
        return default
    con = _db(db)
    try:
        r = con.execute("SELECT level FROM device_delivery WHERE device_id=?", (device_id,)).fetchone()
    finally:
        con.close()
    levels = HOUSEHOLD_LEVELS if role == "household" else PERSON_LEVELS
    return r[0] if r and r[0] in levels else default


def set_device_level(device_id: str, person: str, role: str | None, level: str, db: Path | None = None) -> dict[str, str]:
    levels = HOUSEHOLD_LEVELS if role == "household" else PERSON_LEVELS
    if level not in levels:
        raise ValueError(f"level must be one of {', '.join(levels)}")
    with _lock:
        con = _db(db)
        try:
            con.execute("INSERT OR REPLACE INTO device_delivery VALUES (?,?,?,?)", (device_id, person, level, time.time()))
            con.commit()
        finally:
            con.close()
    return {"id": device_id, "level": level}


def important(card: dict[str, Any]) -> bool:
    return (int(card.get("urgency") or 0) >= 2 or is_safety_source(card.get("source"))
            or card.get("source") in APPROVAL_SOURCES)


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_WEEKEND_STARTS = (4, 5)   # a window belongs to the day it starts on: Friday and Saturday nights are the weekend


def _minutes(v: Any, field: str) -> int:
    m = _HHMM.match(str(v or ""))
    if not m:
        raise ValueError(f"{field} must be HH:MM")
    return int(m.group(1)) * 60 + int(m.group(2))


def check_quiet(q: Any, device: bool = False) -> dict[str, Any] | None:
    """The stored form of a quiet-hours body, or ValueError. None = off (a person) / follows the person (a device);
    {"off": true} = a device that's never quiet."""
    if q is None:
        return None
    if not isinstance(q, dict):
        raise ValueError("quiet must be an object or null")
    if device and q.get("off") is True and set(q) == {"off"}:
        return {"off": True}
    extra = set(q) - {"weekdays", "weekend", "except_important"}
    if extra:
        raise ValueError(f"unknown quiet keys: {', '.join(sorted(extra))}")
    out: dict[str, Any] = {"except_important": True}
    if "except_important" in q:
        if not isinstance(q["except_important"], bool):
            raise ValueError("except_important must be true or false")
        out["except_important"] = q["except_important"]
    for part in ("weekdays", "weekend"):
        w = q.get(part)
        if w is None:
            out[part] = None
            continue
        if not isinstance(w, dict) or set(w) != {"start", "end"}:
            raise ValueError(f"{part} must be {{start, end}} or null")
        if _minutes(w["start"], f"{part}.start") == _minutes(w["end"], f"{part}.end"):
            raise ValueError(f"{part}: start and end are the same minute")
        out[part] = {"start": w["start"], "end": w["end"]}
    return out


def _quiet_row(table: str, key: str, ident: str | None, db: Path | None) -> dict[str, Any] | None:
    if not ident:
        return None
    con = _db(db)
    try:
        r = con.execute(f"SELECT quiet FROM {table} WHERE {key}=?", (ident,)).fetchone()
    finally:
        con.close()
    try:
        return json.loads(r[0]) if r else None
    except ValueError:
        return None


def person_quiet(person: str | None, role: str | None, db: Path | None = None) -> dict[str, Any] | None:
    """A person's own quiet hours, or None (off). A household screen has none of its own beyond its device's."""
    return None if role == "household" else _quiet_row("person_quiet", "person_id", person, db)


def device_quiet(device_id: str | None, db: Path | None = None) -> dict[str, Any] | None:
    return _quiet_row("device_quiet", "device_id", device_id, db)


def set_person_quiet(person: str, role: str | None, q: Any, db: Path | None = None) -> dict[str, Any] | None:
    if role == "household":
        raise ValueError("a household screen has no person quiet hours; set the device's")
    v = check_quiet(q)
    with _lock:
        con = _db(db)
        try:
            con.execute("DELETE FROM person_quiet WHERE person_id=?", (person,))
            if v is not None:
                con.execute("INSERT INTO person_quiet VALUES (?,?,?)", (person, json.dumps(v), time.time()))
            con.commit()
        finally:
            con.close()
    return v


def set_device_quiet(device_id: str, person: str, q: Any, db: Path | None = None) -> dict[str, Any] | None:
    v = check_quiet(q, device=True)
    with _lock:
        con = _db(db)
        try:
            con.execute("DELETE FROM device_quiet WHERE device_id=?", (device_id,))
            if v is not None:
                con.execute("INSERT INTO device_quiet VALUES (?,?,?,?)", (device_id, person, json.dumps(v), time.time()))
            con.commit()
        finally:
            con.close()
    return v


def quiet_for(person: str | None, role: str | None, device_id: str | None, db: Path | None = None) -> dict[str, Any] | None:
    """The quiet hours in force on this device: its own override, else its person's. None = never quiet."""
    dq = device_quiet(device_id, db)
    if dq is not None:
        return None if dq.get("off") else dq
    return person_quiet(person, role, db)


def in_quiet(q: dict[str, Any] | None, now: float | None = None) -> bool:
    """Whether `now` (household clock) is inside q's window: today's, or yesterday's when it runs past midnight."""
    if not q or q.get("off"):
        return False
    from datetime import datetime, timedelta
    d = datetime.fromtimestamp(time.time() if now is None else now, local_time.zone())
    t = d.hour * 60 + d.minute
    for start_day, today in ((d, True), (d - timedelta(days=1), False)):
        w = q.get("weekend" if start_day.weekday() in _WEEKEND_STARTS else "weekdays")
        if not w:
            continue
        a, b = _minutes(w["start"], "start"), _minutes(w["end"], "end")
        if a < b:
            if today and a <= t < b:
                return True
        elif (today and t >= a) or (not today and t < b):
            return True
    return False


def alert_for(card: dict[str, Any], person: str | None, role: str | None, device_id: str | None = None,
              db: Path | None = None, level: str | None = None, mode: str | None = None,
              quiet: dict[str, Any] | None | bool = False, now: float | None = None) -> bool:
    """May this card interrupt this person on this device (banner, peek, sound, Notification Center)? The deck lists it
    either way. False for a source they set quiet (or stopped), a card the device's level leaves out, or, during quiet
    hours, a card below important (every card, if except_important is false). Safety sources always alert in quiet
    hours. `quiet` False = look it up; None = none in force."""
    m = mode if mode is not None else source_mode(person, str(card.get("source") or ""), db=db)
    if m != "notify":
        return False
    lv = level if level is not None else device_level(device_id, role, db)
    if lv == "off":
        return False
    if lv == "important" and not important(card):
        return False
    if lv == "household" and str(card.get("to") or "") != "household":
        return False
    q = quiet_for(person, role, device_id, db) if quiet is False else quiet
    if q and not is_safety_source(card.get("source")) and in_quiet(q, now):
        return bool(q.get("except_important", True)) and important(card)
    return True


def hidden_for(person: str | None, card: dict[str, Any], db: Path | None = None, now: float | None = None) -> bool:
    """Whether this person has muted this card's scope or snoozed it until later (push.targets: no push for them)."""
    if not person:
        return False
    now = time.time() if now is None else now
    con = _db(db)
    try:
        if _muted(card, _mutes_of(con, person)):
            return True
        r = con.execute("SELECT until FROM card_snoozes WHERE card_id=? AND person_id=?", (card.get("id"), person)).fetchone()
    finally:
        con.close()
    return bool(r and r[0] > now)


def wake_snoozed(db: Path | None = None, now: float | None = None, push_fn=None) -> int:
    """Snoozes that are due: each pushed once to its person's own devices (content-free), then marked. → how many."""
    now = time.time() if now is None else now
    with _lock:
        con = _db(db)
        try:
            due = con.execute("SELECT card_id, person_id, role FROM card_snoozes WHERE until <= ? AND notified=0",
                              (now,)).fetchall()
            for cid, pid, _r in due:
                con.execute("UPDATE card_snoozes SET notified=1 WHERE card_id=? AND person_id=?", (cid, pid))
            con.commit()
        finally:
            con.close()
    n = 0
    for cid, pid, role in due:
        card = next((c for c in list_cards(db=db, person=pid, role=role) if c["id"] == cid), None)
        if not card:
            continue   # expired, dismissed or muted since: it stays gone
        try:
            if push_fn is None:
                import push
                push_fn = push.push_card_async
            push_fn({**card, "to": json.dumps([pid]), "urgency": max(2, int(card.get("urgency") or 0))})
            n += 1
        except Exception:  # noqa: BLE001 - a push never breaks the loop
            pass
    return n


TITLE_MAX = 120


def clip_title(title: str, limit: int = TITLE_MAX) -> str:
    """A card title of at most `limit` characters, cut at a word: back to the last space past half the limit, then
    without a trailing , ; : - – — or … (a title cut mid-word read "Move “Project sync” to Thu Oc")."""
    t = str(title or "").strip()
    if len(t) <= limit:
        return t
    cut = t[:limit]
    sp = cut.rfind(" ")
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip().rstrip(",;:-–—…").rstrip()


def add_card(title: str, detail: str = "", kind: str = "info", urgency: int = 1, source: str = "titan",
             action_prompt: str | None = None, db: Path | None = None, actions: Any = None, *,
             to: Any = None, key: str | None = None, expires_in: float | None = None,
             sender: str | None = None, data: Any = None) -> dict[str, Any]:
    kind = kind if kind in KINDS else "info"
    acts = _clean_actions(actions)
    urgency = max(0, min(3, int(urgency)))
    aud = audience(to)
    now = time.time()
    # every card expires (#1039): the source's own time, else its kind's default, so an ignored card never re-nags
    expires_at = now + (float(expires_in) if expires_in else default_expires(kind, source))
    key = (str(key).strip()[:80] or None) if key else None
    # Structured payload for the apps (a conflict's two events, a link to open): a JSON object, ≤ 4 KB.
    blob = json.dumps(data, separators=(",", ":")) if isinstance(data, dict) and data else None
    if blob and len(blob) > 4096:
        blob = None
    card = {"source": source, "kind": kind, "title": clip_title(title), "detail": detail.strip()[:1500],
            "urgency": urgency, "action_prompt": action_prompt, "actions": acts, "to": aud, "key": key,
            "expires_at": expires_at, "from": sender, "updated_at": now,
            "data": json.loads(blob) if blob else None, "group": card_group(source)}
    with _lock:
        con = _db(db)
        try:
            row = None
            if key:
                row = con.execute("SELECT id, created_at FROM cards WHERE card_key=? AND audience=? AND "
                                  "(expires_at IS NULL OR expires_at > ?) ORDER BY created_at DESC LIMIT 1",
                                  (key, aud, now)).fetchone()
            if row:
                # An update: same card, new content, shown again to everyone it's for.
                card.update(id=row[0], created_at=row[1], updated=True)
                con.execute("UPDATE cards SET source=?, kind=?, title=?, detail=?, urgency=?, action_prompt=?, "
                            "actions=?, updated_at=?, expires_at=?, sender=?, data=?, acked_at=NULL WHERE id=?",
                            (source, kind, card["title"], card["detail"], urgency, action_prompt, json.dumps(acts),
                             now, expires_at, sender, blob, row[0]))
                con.execute("DELETE FROM card_acks WHERE card_id=?", (row[0],))
            else:
                cid = "pc-" + hashlib.sha1(f"{now}:{title}:{aud}".encode()).hexdigest()[:12]
                card.update(id=cid, created_at=now)
                con.execute("INSERT INTO cards (id, source, kind, title, detail, urgency, action_prompt, created_at, "
                            "acked_at, actions, audience, card_key, updated_at, expires_at, sender, data) "
                            "VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?,?)",
                            (cid, source, kind, card["title"], card["detail"], urgency, action_prompt, now,
                             json.dumps(acts), aud, key, now, expires_at, sender, blob))
            con.commit()
        finally:
            con.close()
    try:   # the iPhone, with the app closed (push.py: content-free; only devices that should get it)
        import push
        push.push_card_async(card)
    except Exception:  # noqa: BLE001 - push must never break a card
        pass
    if urgency >= 3 and visible_to(aud, None, "admin"):
        _urgent_nudge()   # the ntfy channel is the admin's phone; content-free either way
    made = getattr(_made, "ids", None)
    if made is not None and card.get("id") not in made:
        made.append(card["id"])   # this turn's cards, for its reply's action blocks (jellybean#201 slice 3)
    return card


def _urgent_nudge() -> None:
    """Phone nudge for a "right now" card, so it isn't missed while the Mac is closed. Content-free
    on purpose: the alert channel (ntfy) is a public service and must never carry household data,
    so the card itself stays in Jellybean."""
    try:
        import titan_alert
        titan_alert.alert("Titan has an urgent note", "Open Jellybean to read it.", priority="high", tags="bell")
    except Exception:  # noqa: BLE001 - a dead alert channel must not break card creation
        pass


def list_cards(since: float = 0.0, include_acked: bool = False, db: Path | None = None, *,
               person: str | None = None, role: str | None = None, controls: bool = True,
               device_id: str | None = None, ineligible: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Cards created or updated after `since`, not expired. With a person/role, only the cards for
    them, and "acked" means that person dismissed it (or it was dismissed for everyone). With a person (#1039): a
    card they muted the scope of is left out, one they snoozed is left out until it's due, and a due one comes back
    with `snoozed_from` and counts as changed at its due time. Every card says `can_mute` and `approval`.
    `ineligible` (#1060's trace): when given, counts this viewer's own open cards left out by their own mute or
    snooze, {"muted": n, "snoozed": n}; counted only after visibility and fences, so never a card they can't see."""
    now = time.time()
    con = _db(db)
    try:
        q = ("SELECT id, source, kind, title, detail, urgency, action_prompt, created_at, acked_at, actions, "
             "audience, card_key, COALESCE(updated_at, created_at), expires_at, sender, data FROM cards "
             "WHERE COALESCE(updated_at, created_at) > ? AND (expires_at IS NULL OR expires_at > ?)")
        rows = con.execute(q + " ORDER BY COALESCE(updated_at, created_at)", (since, now)).fetchall()
        mine = ({r[0]: r[1] for r in con.execute("SELECT card_id, acked_at FROM card_acks WHERE person_id=?",
                                                 (person,))} if person else {})
        snoozes = ({r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT card_id, until, snoozed_at FROM card_snoozes WHERE person_id=?", (person,))} if person and controls
            else {})
        muted = _mutes_of(con, person) if controls else []
        # a snooze that came due after `since` brings its card back even if the card itself didn't change
        back = [cid for cid, (until, _at) in snoozes.items() if since < until <= now]
        have = {r[0] for r in rows}
        extra = [cid for cid in back if cid not in have]
        if extra:
            rows += con.execute(q.replace("COALESCE(updated_at, created_at) > ?", "id IN (%s)" % ",".join("?" * len(extra))),
                                (*extra, now)).fetchall()
    finally:
        con.close()
    keys = ("id", "source", "kind", "title", "detail", "urgency", "action_prompt", "created_at", "acked_at", "actions",
            "to", "key", "updated_at", "expires_at", "from", "data")
    lvl = device_level(device_id, role, db) if controls else None
    qh = quiet_for(person, role, device_id, db) if controls else None
    modes_cache: dict[str, str] = {}
    cards = []
    for r in rows:
        c = dict(zip(keys, r))
        c["to"] = c["to"] or "admins"
        c["group"] = card_group(c["source"])
        if not visible_to(c["to"], person, role):
            continue
        if person is not None or role is not None:
            import fences   # #357: a card about a fenced thing doesn't show to someone the fence keeps out
            if not fences.get().admits(person, role, c.get("from"), text=f"{c['title']}\n{c['detail']}", item=c["id"]):
                continue
        if person and c["acked_at"] is None and c["id"] in mine:
            c["acked_at"] = mine[c["id"]]
        if controls:
            if muted and _muted(c, muted):
                if ineligible is not None and c["acked_at"] is None:
                    ineligible["muted"] = ineligible.get("muted", 0) + 1
                continue
            sn = snoozes.get(c["id"])
            if sn and sn[0] > now:
                if ineligible is not None and c["acked_at"] is None:
                    ineligible["snoozed"] = ineligible.get("snoozed", 0) + 1
                continue   # snoozed: back at sn[0]
            c["snoozed_from"] = (local_time.day(sn[1]) if sn else None)
            c["can_mute"] = can_mute(c, role)
            c["approval"] = c["source"] in APPROVAL_SOURCES
            # may it interrupt here (#1039, 10-06): their mode for its source, this device's level, quiet hours
            c["alert"] = alert_for(c, person, role, device_id, db, level=lvl, quiet=qh, now=now,
                                   mode=modes_cache.setdefault(c["source"], source_mode(person, c["source"], db=db)))
        if not include_acked and c["acked_at"] is not None:
            continue
        try:
            c["actions"] = json.loads(c["actions"] or "[]")
        except ValueError:
            c["actions"] = []
        try:
            c["data"] = json.loads(c["data"]) if c["data"] else None
        except ValueError:
            c["data"] = None
        cards.append(c)
    return cards


def expired_between(since: float, until: float, db: Path | None = None) -> list[dict[str, Any]]:
    """Cards whose expiry fell in (since, until]: id, source, kind, urgency, to, key, from, expires_at and group (no
    words), for push.clear_expired to take their notifications off the phones."""
    con = _db(db)
    try:
        rows = con.execute("SELECT id, source, kind, urgency, audience, card_key, sender, expires_at FROM cards "
                           "WHERE expires_at > ? AND expires_at <= ? ORDER BY expires_at", (since, until)).fetchall()
    finally:
        con.close()
    keys = ("id", "source", "kind", "urgency", "to", "key", "from", "expires_at")
    return [{**dict(zip(keys, r)), "to": r[4] or "admins", "group": card_group(r[1])} for r in rows]


def last_change(db: Path | None = None) -> float:
    """When any card last appeared or changed (the long-poll's cheap "anything new?")."""
    con = _db(db)
    try:
        card = float(con.execute("SELECT COALESCE(MAX(COALESCE(updated_at, created_at)), 0) FROM cards").fetchone()[0])
        # a snooze coming due is a change too (#1039): the long-poll wakes and the card comes back
        due = float(con.execute("SELECT COALESCE(MAX(until), 0) FROM card_snoozes WHERE until <= ?",
                                (time.time(),)).fetchone()[0])
        return max(card, due)
    finally:
        con.close()


# What a tap relayed from a Watch may answer (10-07 review, #1013 rule 3): a reply action's verb (the value's first
# word, before ":" or a space) must be here. Anything else (an unlock, a mail move or undo, a standing grant, a model
# switch, sitter mode, a re-share, a cancel) is refused with WATCH_PHONE_ONLY and the card stays, so a new card
# producer's verb fails closed until it's classified (test_watch_wrist.py keeps the two lists complete).
WATCH_OK_VERBS = frozenset({
    "decline", "later", "keep", "paid", "noteok", "missno", "gapskip", "nocap", "skip", "pick", "still", "confirm",
    "reject", "right", "facet", "facetset", "fence", "stop", "meeting",
    "cap",    # a limit: step_caps requires the explicit amount its Watch button showed (WATCH_NEEDS_AMOUNT)
    "lock",   # Lock Up: home_security refuses a relayed lock with WATCH_LOCK and an audit row (#1013); a TV's relock
})
WATCH_REFUSED_VERBS = frozenset({"unlock", "move", "undo", "allow_once", "always", "switch", "start", "share", "cancel",
                                 "unsub"})   # unsub: a sender's one-click unsubscribe, the owner's iPhone or Mac only (#1642)
WATCH_PHONE_ONLY = "Do that on your iPhone."


def _verb(value: Any) -> str:
    m = re.match(r"[A-Za-z_]+", str(value or "").strip())
    return m.group(0).lower() if m else ""


def card_action(card_id: str, action_id: str, db: Path | None = None,
                trigger: Callable[[dict], Any] | None = None, *, person: str | None = None,
                role: str | None = None, device: dict[str, Any] | None = None, value: Any = None,
                via: Any = None) -> dict[str, Any]:
    """The user pressed a card button in Jellybean. check → queue that named check; reply → tell the
    agent through the loop; prompt → the app asks it as the user (nothing to do here); dismiss → ack.
    The card is acked in every case. via "watch" (jellybean#900): the iPhone relayed a Watch tap (its own claim,
    defence in depth); anything else, or none, is today's tap."""
    via = "watch" if str(via or "").strip().lower() == "watch" else None
    card = next((c for c in list_cards(include_acked=True, db=db, person=person, role=role) if c["id"] == card_id), None)
    if not card:
        return {"ok": False, "error": "no such card"}
    act = next((a for a in card["actions"] if a["id"] == action_id), None)
    if not act:
        return {"ok": False, "error": "no such action"}
    result: dict[str, Any] = {"ok": True, "kind": act["kind"]}
    if card.get("acked_at") is not None and act["kind"] != "dismiss":
        # Answered already (this person tapped it, or it was settled for everyone): a repeat or a late
        # retry from the app never queues the check or tells the agent twice. ok, so the app stops retrying.
        result["already"] = True
        result["said"] = "Already answered."
        return result
    if via == "watch" and act["kind"] == "reply" and _verb(act.get("value")) not in WATCH_OK_VERBS:
        # from a wrist, nothing that unlocks, moves, grants or switches (10-07): the card stays for the phone
        result["ok"], result["said"] = False, WATCH_PHONE_ONLY
        return result
    if person:   # what this person did with the source's card (#1039): a dismiss button is Done, any other acts
        with _lock:
            con = _db(db)
            try:
                _feedback(con, person, card, "done" if act["kind"] == "dismiss" else "acted")
                con.commit()
            finally:
                con.close()
    if act.get("id") == "keep" and str(card.get("key") or "").startswith("world-note-") and person:
        # #377 half 2: Keep on a miss proposal; the app then asks its prompt as the person (a turn they see makes the
        # change). Recorded here so the weekly report can count Kept vs Not useful.
        try:
            import miss_proposals
            import world as _W
            miss_proposals.mark(_W.get(), person, int(str(card["key"]).rsplit("-", 1)[1]), "kept")
        except Exception:  # noqa: BLE001 - the tap still works; only the count misses it
            pass
    key = str(card.get("card_key") or card.get("key") or "")
    if act["kind"] == "prompt" and key.startswith("textprop-") and person:
        # jellybean#1189: Add / Update on a plan from texts keeps it, so the owner's Mac may thread the texts it came
        # from (event_proposals.kept). Only the owner's own proposal row is marked.
        try:
            import event_proposals
            import hooks
            hcon = hooks.connect()
            try:
                event_proposals.mark_kept(hcon, person, key[len("textprop-"):])
            finally:
                hcon.close()
        except Exception:  # noqa: BLE001 - the tap still works; only the threading misses it
            pass
    if act["kind"] == "check":
        result["job"] = request_check(act["value"], requested_by=f"card:{card_id}", db=db)
    elif act["kind"] == "reply" and card.get("source") == "world-proposal":
        # a parent confirming or turning down a kid's statement (#358): decided in code, not by the agent,
        # and one parent's answer settles it for both
        import world
        try:
            import household_identity as hh
            folks = hh.get_store().people()
        except Exception:  # noqa: BLE001
            folks = []
        done, result["said"] = world.answer_ask(world.get(), act["value"], person, role, folks)
        result["ok"] = done
        if done:
            ack_card(card_id, db=db)
            return result
    elif act["kind"] == "reply" and card.get("source") == "kb-digest":
        # the owner keeping a filtered mail, or saying dropping it was right (#861), decided in code
        import kb_digest
        done, result["said"] = kb_digest.answer(act["value"], person)
        result["ok"] = done
        ack_card(card_id, db=db)
        return result
    elif act["kind"] == "reply" and card.get("source") == "item-consent":
        # the owner's yes or no for one read of their mail or scan by someone else (#446 step 3), decided in code
        import item_open
        done, result["said"] = item_open.answer(act["value"], person)
        result["ok"] = done
        ack_card(card_id, db=db)
        return result
    elif act["kind"] == "reply" and card.get("source") == "model-switch":
        # an admin switching titan's local model after the nightly eval, or keeping it (#408): decided in
        # code; Switch only queues the request, which model_switch.py apply performs and rolls back on failure
        import model_switch
        done, result["said"] = model_switch.answer(act["value"], person, role)
        result["ok"] = done
        if done:
            ack_card(card_id, db=db)
        return result
    elif act["kind"] == "reply" and card.get("source") == "tv-unlock":
        # an adult unlocking a household TV for their own things, or locking it again (jellybean#427), decided in code
        import tv_unlock
        done, result["said"] = tv_unlock.answer(act["value"], person, role)
        result["ok"] = done
        ack_card(card_id, db=db, person=person)
        return result
    elif act["kind"] == "reply" and card.get("source") == "mail-move":
        # archiving mail on the server (#955 step 5): only a tap on the owner's own iPhone or Mac moves anything,
        # decided in code. A refused tap leaves the card; an answered one retires it.
        import mail_moves
        done, result["said"] = mail_moves.answer(act["value"], person, role, device)
        result["ok"] = done
        if done:
            ack_card(card_id, db=db, person=person)
        return result
    elif act["kind"] == "reply" and card.get("source") == "mail-unsub":
        # a sender's one-click unsubscribe (#1642): only a tap on the owner's own iPhone or Mac sends it, decided in code
        import mail_unsubscribe
        done, result["said"] = mail_unsubscribe.answer(act["value"], person, role, device)
        result["ok"] = done
        if done:
            ack_card(card_id, db=db, person=person)
        return result
    elif act["kind"] == "reply" and card.get("source") == "file-share-changed":
        # a shared file changed on its owner's Mac (#1170): share this version, stop sharing, or later (the card stays)
        import file_share
        done, result["said"] = file_share.answer(act["value"], person, role)
        result["ok"] = done or str(act["value"]).startswith("later:")
        if done:
            ack_card(card_id, db=db, person=person)
        return result
    elif act["kind"] == "reply" and card.get("source") == "bill-due":
        # a bill coming due (#567): Paid records the owner's word (never money), Not yet asks again on the due day
        import bill_due
        done, result["said"] = bill_due.answer(act["value"], person, role)
        result["ok"] = done
        if done:
            ack_card(card_id, db=db, person=person)
        return result
    elif act["kind"] == "reply" and card.get("source") == "approve-cap":
        # a task step's limit on a slider (jellybean#707): the owner's own amount, never one they didn't see
        import step_caps
        # the slider's number travels in `value`, accepted only for this same cap ("cap:<id>:<n>"); otherwise the
        # button's own value, which carries the amount its label showed
        own = str(act["value"])
        sent = str(value or "")
        explicit = own.startswith("cap:") and sent.startswith(":".join(own.split(":")[:2]) + ":")
        if via == "watch" and own.startswith("cap:") and not explicit:
            # a Watch tap must carry the amount its button showed: never the suggestion approved unseen
            result["ok"], result["said"] = False, step_caps.WATCH_NEEDS_AMOUNT
            return result
        chosen = sent if explicit else own
        done, result["said"] = step_caps.answer(chosen, person, role, device, via=via)
        result["ok"] = done
        if done:
            ack_card(card_id, db=db, person=person)
        return result
    elif act["kind"] == "reply" and card.get("source") == "lock-up":
        # Lock Up (titan-node#413): only a tap on the person's own iPhone locks, decided in code. A refused tap (a Mac,
        # off, ran out) leaves the card as it is; Lock's success retires it for everyone, Not now for this person.
        import home_security
        done, result["said"] = home_security.answer(act["value"], person, role, device, via=via)
        result["ok"] = done
        if done and act["value"].startswith("decline:"):
            ack_card(card_id, db=db, person=person)
        return result
    elif act["kind"] == "reply" and card.get("source") == "sitter":
        # an adult turning on sitter mode for a TV that asked (jellybean#428), decided in code
        import sitter
        done, result["said"] = sitter.answer(act["value"], person, role)
        result["ok"] = done
        ack_card(card_id, db=db)
        return result
    elif act["kind"] == "reply" and card.get("source") == "voice-note":
        # a recipient keeping a voice note's words (titan-node#859), decided in code; private to them, the card stays
        import voice_notes
        done, result["said"] = voice_notes.keep(card, person, role)
        result["ok"] = done
        return result
    elif act["kind"] == "reply" and card.get("source") == "family-meeting":
        # who's driving (jellybean#425), decided in code: "I will" settles it for every adult, "Not me" for this one
        import family_meeting
        done, settled, result["said"] = family_meeting.answer(act["value"], person, role)
        result["ok"] = done or settled
        ack_card(card_id, db=db, person=None if settled else person)
        return result
    elif act["kind"] == "reply":
        result["trigger"] = (trigger or submit_trigger)({
            "source": "card-reply", "kind": "reply",
            "summary": f"The user answered \"{act['label']}\" to your card \"{card['title']}\"",
            "detail": card["detail"], "dedupe_key": f"reply:{card_id}:{action_id}",
            "person": person, "role": role})
    ack_card(card_id, db=db, person=person)
    return result


def ack_card(card_id: str, db: Path | None = None, *, person: str | None = None, signal: str | None = None) -> bool:
    """Dismiss a card: for that person only when one is given (the others it's for still see it),
    for everyone otherwise. signal "done": the person's own Done, counted for the turn report (#1039)."""
    with _lock:
        con = _db(db)
        try:
            if person:
                row = con.execute("SELECT source, kind, card_key FROM cards WHERE id=?", (card_id,)).fetchone()
                if not row:
                    return False
                cur = con.execute("INSERT OR IGNORE INTO card_acks (card_id, person_id, acked_at) VALUES (?,?,?)",
                                  (card_id, person, time.time()))
                if signal and cur.rowcount:
                    _feedback(con, person, {"source": row[0], "kind": row[1], "key": row[2]}, signal)
            else:
                cur = con.execute("UPDATE cards SET acked_at=? WHERE id=? AND acked_at IS NULL", (time.time(), card_id))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


# ---------------------------------------------------------------------------- outcomes (#1039)

NOISY_SHARE = 0.60   # expired untouched + muted over shown
NOISY_MIN_SHOWN = 10


def outcomes(since: float, until: float, admin: str | None, db: Path | None = None) -> dict[str, dict[str, Any]]:
    """Per card source, what happened to its cards in [since, until): shown (cards sent), done, snoozed, muted,
    acted (a source's button), expired untouched (expired with no one's Done, button or snooze). Counts only, never a
    title. `mine`: the admin's own counts; everyone else's only in the totals (no per-person counts by name)."""
    con = _db(db)
    try:
        out: dict[str, dict[str, Any]] = {}

        def row(src: str) -> dict[str, Any]:
            return out.setdefault(src or "unknown", {"shown": 0, "done": 0, "snoozed": 0, "muted": 0, "acted": 0,
                                                     "expired_untouched": 0,
                                                     "mine": {"done": 0, "snoozed": 0, "muted": 0, "acted": 0}})
        for src, n in con.execute("SELECT source, COUNT(*) FROM cards WHERE created_at >= ? AND created_at < ? "
                                  "GROUP BY source", (since, until)):
            row(src)["shown"] += n
        names = {"done": "done", "snooze": "snoozed", "mute": "muted", "acted": "acted"}
        for src, person, sig, n in con.execute(
                "SELECT source, person_id, signal, COUNT(*) FROM card_feedback WHERE at >= ? AND at < ? "
                "GROUP BY source, person_id, signal", (since, until)):
            k = names.get(sig)
            if not k:
                continue
            r = row(src)
            r[k] += n
            if admin and person == admin:
                r["mine"][k] += n
        for src, n in con.execute(
                "SELECT source, COUNT(*) FROM cards c WHERE expires_at >= ? AND expires_at < ? AND acked_at IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM card_acks a WHERE a.card_id = c.id) "
                "AND NOT EXISTS (SELECT 1 FROM card_snoozes z WHERE z.card_id = c.id) GROUP BY source", (since, until)):
            row(src)["expired_untouched"] += n
        for r in out.values():
            r["noisy"] = bool(r["shown"] >= NOISY_MIN_SHOWN
                              and (r["expired_untouched"] + r["muted"]) / r["shown"] > NOISY_SHARE)
        return out
    finally:
        con.close()


# ---------------------------------------------------------------------------- triggers

def _decider_triage(event: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({"state": {"now": local_time.now_line(),
                                 "event": f"{event['source']} / {event['kind']}: {event['summary']}",
                                 # Triage needs the gist only; the whole diff still goes to the review.
                                 # Decider on the shared GPU1 runs out of memory at ~4000 chars.
                                 "detail": str(event.get("detail") or "")[:1500]},
                       "questions": _TRIAGE_QUESTIONS}).encode()
    req = urllib.request.Request(f"{DECIDER_URL}/v1/systemone", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["answers"]


def _role_of(person: str) -> str:
    """A person's own role when a trigger didn't carry one: from the household store, never assumed. Someone the store
    doesn't know (or a store that can't be read) gets a kid's, the narrowest a person has: a missing role used to read
    as "adult", so a kid's card reply could see what only adults may (review on #1735)."""
    try:
        import household_identity as hh
        r = next((str(p.get("role") or "") for p in hh.get_store().people() if str(p.get("id")) == str(person)), "")
    except Exception:  # noqa: BLE001
        r = ""
    return r if r in ("admin", "adult", "kid") else "kid"


def _agent_turn(prompt: str, person: str | None = None, role: str | None = None) -> str:
    """A read-only, local-brain agent turn (household data stays on titan). With a person (a card
    reply), it runs as them: their role's tools and only what they may see."""
    import agent_runtime_lib as lib
    if person:
        lib._TURN_IO.person = {"id": person, "name": None}
        lib._TURN_IO.role = role or _role_of(person)
    lib._TURN_IO.brain = "local"
    lib._TURN_IO.detail = "brief"
    lib._TURN_IO.budget_s = 300   # nobody is waiting; a cold local prompt alone can take ~45 s
    lib._TURN_IO.jev_source = "proactive"   # its Jev decisions are the loop's, not a person's
    try:
        # Its tools never send anything out of the house (#748): no web search, fetching, image description or Jev.
        return lib._run_agent_turn([{"role": "system", "content": lib.AGENT_IDENTITY + "\n\n" + lib.build_tool_system_prompt(lib.BACKGROUND_TOOLS)},
                                    {"role": "user", "content": prompt}],
                                   allowed_tools=set(lib.BACKGROUND_TOOLS), max_rounds=4)
    finally:
        lib._TURN_IO.brain = None
        lib._TURN_IO.detail = None
        lib._TURN_IO.budget_s = None
        lib._TURN_IO.jev_source = None
        if person:
            lib._TURN_IO.person = None
            lib._TURN_IO.role = None


def _kb_passages(query: str, top_k: int = 3) -> str:
    try:
        import agent_runtime_lib as lib
        import kb_privacy
        r = lib._runtime_request("retrieval", "/v1/retrieval/query", "POST",
                                 {"query": query, "top_k": top_k, **kb_privacy.QUERY_OPTS}, timeout=10)
        hits = r.get("results") or []
        lines = [f"- {h.get('title') or h.get('source_id')}: {str(h.get('chunk_text') or h.get('text') or '')[:300]}"
                 for h in hits[:top_k]]
        return "\n".join(lines) or "(none found)"
    except Exception:  # noqa: BLE001
        return "(knowledge base unavailable)"


def _code_review(ev: dict[str, Any]) -> str:
    """Review a git change on the local code model (Qwen3.6 on GPUs 0 and 2): no data leaves titan."""
    prompt = _REVIEW_PROMPT.format(decisions=_kb_passages(ev["summary"]), **ev)
    body = json.dumps({"model": "local", "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 450, "temperature": 0.2}).encode()
    req = urllib.request.Request(f"{CODE_MODEL_URL}/v1/chat/completions", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")


DEV_SOURCES = {"git", "checks"}


def _timeline(line: str, stream: str = "household-timeline", *, person: str | None = None,
              audience: str = "admins") -> None:
    """A loop note on the timeline. Trigger notes and dev activity are titan's own, for the admin
    ("admins"); a card reply's outcome is the person who replied's (audience.py)."""
    import agent_runtime_lib as lib
    import uuid
    payload: dict[str, Any] = {"text": line[:500], "audience": audience}
    if person:
        payload["person"] = person
    lib._runtime_request("event", "/v1/events", "POST", {
        "record_id": stream, "event_type": "household.note", "trace_id": uuid.uuid4().hex,
        "stream_key": stream, "session_id": stream, "payload": payload})


# A commit Claude helped write (Jellybean's git trigger detail is author, subject, body, stat, patch):
# the dev note says so, so "what did I get done today?" credits it (09-30: Claude's commits read as Zach's).
_WITH_CLAUDE_RX = re.compile(r"^Co-Authored-By:\s*Claude\b", re.I | re.M)
_MERGE_RX = re.compile(r"^\S+ [0-9a-f]{7,40}: Merge (remote-tracking )?(branch|pull request|origin/|main|remote)", re.I)


def submit_trigger(event: dict[str, Any], *, triage: Callable[[dict], dict] | None = None,
                   agent: Callable[[str], str] | None = None, timeline: Callable[[str], None] | None = None,
                   reviewer: Callable[[dict], str] | None = None,
                   db: Path | None = None, background: bool = True) -> dict[str, Any]:
    """Record an event and process it (in a background thread unless background=False).
    Returns {id, status} where status is accepted | duplicate | rate_limited."""
    source = str(event.get("source") or "").strip()[:40]
    summary = str(event.get("summary") or "").strip()
    if not source or not summary:
        raise ValueError("source and summary are required")
    kind = str(event.get("kind") or "event").strip()[:40]
    dedupe = str(event.get("dedupe_key") or f"{source}:{summary}")[:300]
    if source == "git" and _MERGE_RX.search(summary):
        # A merge commit's diff is other commits that were reviewed on their own (branch merges,
        # "Merge origin/main into …"): reviewing it again only raises duplicate cards.
        return {"id": None, "status": "skipped"}
    fingerprint = _patch_fingerprint(str(event.get("detail") or "")) if source == "git" else None
    if fingerprint:   # a branch commit and its squash-merge carry the same patch: review it once
        dedupe = f"gitpatch:{fingerprint}"
    now = time.time()
    tid = "tr-" + hashlib.sha1(f"{now}:{dedupe}".encode()).hexdigest()[:12]
    ev = {"source": source, "kind": kind, "summary": summary[:500], "detail": str(event.get("detail") or "")[:4000]}
    person = str(event.get("person") or "").strip().lower()[:60] or None   # a card reply's sender
    if person:
        ev.update(person=person, role=str(event.get("role") or "")[:20] or None)
    with _lock:
        con = _db(db)
        try:
            dup = con.execute("SELECT id FROM triggers WHERE dedupe_key=? AND received_at>?",
                              (dedupe, now - DEDUPE_SECONDS)).fetchone()
            if dup:
                return {"id": dup[0], "status": "duplicate"}
            turns = con.execute("SELECT COUNT(*) FROM triggers WHERE outcome IN ('reported','logged','no_action','running') "
                                "AND received_at>?", (now - 3600,)).fetchone()[0]
            limited = turns >= MAX_TURNS_PER_HOUR
            con.execute("INSERT INTO triggers (id, source, kind, summary, dedupe_key, received_at, outcome, "
                        "reason, card_id, finished_at, detail, person, role) VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)",
                        (tid, source, kind, ev["summary"], dedupe, now,
                         "rate_limited" if limited else "queued", None, ev["detail"], ev.get("person"), ev.get("role")))
            con.commit()
        finally:
            con.close()
    if limited:
        return {"id": tid, "status": "rate_limited"}

    def work() -> None:
        _process(tid, ev, triage=triage, agent=agent, timeline=timeline, reviewer=reviewer, db=db)

    if background:
        threading.Thread(target=work, name=f"trigger-{tid}", daemon=True).start()
    else:
        work()
    return {"id": tid, "status": "accepted"}


def _process(tid: str, ev: dict[str, Any], *, triage: Callable[[dict], dict] | None = None,
             agent: Callable[[str], str] | None = None, timeline: Callable[[str], None] | None = None,
             reviewer: Callable[[dict], str] | None = None, db: Path | None = None,
             reason_prefix: str = "") -> None:
    """Triage → act → surface one recorded trigger; always leaves it with a final outcome."""
    source = ev["source"]
    outcome, reason, card_id = "error", reason_prefix, None
    try:
        try:
            ans = (triage or _decider_triage)(ev)
            act = ans.get("actionable") or {}
            p_no = float((act.get("probabilities") or {}).get("no", 0.0))
            urgency = int(round(float((ans.get("urgency") or {}).get("score", 1))))
        except Exception as exc:  # noqa: BLE001 - no triage means investigate, never drop
            p_no, urgency = 0.0, 1
            reason = "; ".join(x for x in (reason_prefix, f"triage unavailable: {str(exc)[:120]}") if x)
        if p_no >= NOISE_CONFIDENCE:
            outcome, reason = "dropped", f"noise (p={p_no:.2f})"
            return
        _set(tid, "running", reason, None, db)
        if ev["source"] == "git" and ev["detail"] and not agent:
            reply = (reviewer or _code_review)(ev).strip()    # code changes: the local code model
        elif agent:
            reply = agent(_TRIGGER_PROMPT.format(**ev)).strip()
        else:
            reply = _agent_turn(_TRIGGER_PROMPT.format(**ev), ev.get("person"), ev.get("role")).strip()
        tier, note = _tier(reply)
        if tier == "none":
            outcome = "no_action"
            return
        # A card reply's outcome is for the person who replied (a household device: the household's);
        # everything else the loop notes is titan's own, for the admin.
        who = ev.get("person")
        to = None if not who else "household" if who == "household" else [who]
        try:   # passive and notify both go on a timeline the agent sees; dev activity has its own
            with_claude = source == "git" and bool(_WITH_CLAUDE_RX.search(ev.get("detail") or ""))
            line = f"{ev['summary'][:200]}{' (with Claude)' if with_claude else ''} — {note[:250]}"
            if timeline:
                timeline(line)
            elif who:
                _timeline(line, "household-timeline", person=None if who == "household" else who,
                          audience="household" if who == "household" else "private")
            else:
                _timeline(line, "dev-activity" if source in DEV_SOURCES else "household-timeline")
        except Exception:  # noqa: BLE001
            pass
        if tier == "passive":
            outcome = "logged"
            return
        acts = None
        if source == "checks" and " failed" in ev["summary"]:
            acts = [{"label": "Re-run", "kind": "check", "value": ev["summary"].split(" failed")[0].strip()},
                    {"label": "Dismiss", "kind": "dismiss"}]
        card = add_card(ev["summary"][:120], note, kind="report", urgency=max(urgency, 1),
                        source=f"trigger:{source}", action_prompt=f"Tell me more about: {ev['summary'][:200]}",
                        db=db, actions=acts, to=to)
        card_id, outcome = card["id"], "reported"
    except Exception as exc:  # noqa: BLE001
        outcome, reason = "error", str(exc)[:300]
    finally:
        _set(tid, outcome, reason, card_id, db, finished=True)


RECOVER_MAX_AGE = 6 * 3600


def recover_interrupted(db: Path | None = None, *, max_age: float = RECOVER_MAX_AGE,
                        background: bool = True, **process_kw: Any) -> dict[str, int]:
    """At agent-API startup nothing can still be running, so a trigger left 'running' or 'queued'
    was cut off by the restart (every deploy restarts the API). Re-run each once, oldest first, one
    at a time; one older than max_age, already retried, or without its detail stored becomes
    'interrupted'. Returns {"retried": n, "interrupted": m}."""
    now = time.time()
    with _lock:
        con = _db(db)
        try:
            rows = con.execute("SELECT id, source, kind, summary, detail, received_at, reason, person, role "
                               "FROM triggers WHERE outcome IN ('running','queued') ORDER BY received_at").fetchall()
        finally:
            con.close()
    retry, gone = [], 0
    for tid, source, kind, summary, detail, received, reason, person, role in rows:
        if now - received > max_age or "retried" in (reason or "") or detail is None:
            _set(tid, "interrupted", "cut off by an agent restart", None, db, finished=True)
            gone += 1
        else:
            _set(tid, "queued", "retried after an agent restart", None, db)
            ev = {"source": source, "kind": kind, "summary": summary, "detail": detail or ""}
            if person:
                ev.update(person=person, role=role)
            retry.append((tid, ev))

    def run_all() -> None:
        for tid, ev in retry:
            _process(tid, ev, db=db, reason_prefix="retried after an agent restart", **process_kw)

    if retry:
        if background:
            threading.Thread(target=run_all, name="trigger-recovery", daemon=True).start()
        else:
            run_all()
    return {"retried": len(retry), "interrupted": gone}


def _tier(reply: str) -> tuple[str, str]:
    """(none|passive|notify, note) from the trigger turn's reply. Anything unlabelled is treated as
    passive: it lands on the timeline but never interrupts the user."""
    text = (reply or "").strip()
    first, _, rest = text.partition("\n")
    head = first.strip().upper().rstrip(":").strip("*` ")
    if not text or head.startswith("NO_ACTION") or text.upper() == "NO_ACTION":
        return "none", ""
    if head.startswith("NOTIFY"):
        return "notify", (rest.strip() or first[len("NOTIFY"):].strip(" :—-"))
    if head.startswith("PASSIVE"):
        return "passive", (rest.strip() or first[len("PASSIVE"):].strip(" :—-"))
    return "passive", text


def _patch_fingerprint(detail: str) -> str | None:
    """Hash of a diff's changed lines only (no commit hashes, headers, index or hunk lines), so the
    same change seen twice (on its branch, then squash-merged) dedupes. None if there's no diff."""
    if "diff --git" not in detail:
        return None
    changed = [ln for ln in detail.splitlines()
               if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    return hashlib.sha1("\n".join(changed).encode()).hexdigest()[:20] if changed else None


def _set(tid: str, outcome: str, reason: str, card_id: str | None, db: Path | None, finished: bool = False) -> None:
    with _lock:
        con = _db(db)
        try:
            con.execute("UPDATE triggers SET outcome=?, reason=?, card_id=?, finished_at=? WHERE id=?",
                        (outcome, reason, card_id, time.time() if finished else None, tid))
            con.commit()
        finally:
            con.close()


def recent_triggers(last: int = 20, db: Path | None = None) -> list[dict[str, Any]]:
    con = _db(db)
    try:
        rows = con.execute("SELECT id, source, kind, summary, received_at, outcome, reason, card_id, finished_at "
                           "FROM triggers ORDER BY received_at DESC LIMIT ?", (last,)).fetchall()
    finally:
        con.close()
    keys = ("id", "source", "kind", "summary", "received_at", "outcome", "reason", "card_id", "finished_at")
    return [dict(zip(keys, r)) for r in rows]


# ---------------------------------------------------------------------------- named checks
# Headless, allowlisted Mac checks. Jellybean defines each check (name → command) in its own
# settings and publishes only the names; titan can ask for a check BY NAME and never supplies a
# command. Jellybean claims jobs, runs them, posts results, and posts a "checks" trigger so the
# loop can surface a failure.

CHECK_DEDUPE_SECONDS = 600


def _jobs_db(db: Path | None = None) -> sqlite3.Connection:
    con = _db(db)
    con.execute("""CREATE TABLE IF NOT EXISTS check_catalog (
        name TEXT PRIMARY KEY, description TEXT, updated_at REAL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS device_jobs (
        id TEXT PRIMARY KEY, check_name TEXT, requested_by TEXT, requested_at REAL, claimed_at REAL,
        finished_at REAL, ok INTEGER, exit_code INTEGER, output TEXT, duration_ms INTEGER)""")
    con.execute("CREATE TABLE IF NOT EXISTS device_jobs_seen (k TEXT PRIMARY KEY, at REAL)")
    if "reported_at" not in {r[1] for r in con.execute("PRAGMA table_info(device_jobs)")}:
        con.execute("ALTER TABLE device_jobs ADD COLUMN reported_at REAL")
    return con


# Jellybean asks for jobs every 30 s while it runs; longer than this without asking means it isn't
# picking checks up (the app is closed or the Mac is asleep), so a queued check waits for it.
CLAIM_STALE_SECONDS = 120


def set_catalog(checks: list[dict[str, Any]], db: Path | None = None) -> int:
    rows = [(str(c.get("name") or "").strip()[:60], str(c.get("description") or "")[:300])
            for c in checks if isinstance(c, dict) and str(c.get("name") or "").strip()]
    with _lock:
        con = _jobs_db(db)
        try:
            con.execute("DELETE FROM check_catalog")
            con.executemany("INSERT INTO check_catalog VALUES (?,?,?)", [(n, d, time.time()) for n, d in rows])
            con.commit()
        finally:
            con.close()
    return len(rows)


def catalog(db: Path | None = None) -> list[dict[str, str]]:
    con = _jobs_db(db)
    try:
        return [{"name": n, "description": d} for n, d in
                con.execute("SELECT name, description FROM check_catalog ORDER BY name").fetchall()]
    finally:
        con.close()


def request_check(name: str, requested_by: str = "agent", db: Path | None = None) -> dict[str, Any]:
    """Queue a named check. Reuses a pending/claimed/recent job for the same check (no loops)."""
    name = str(name or "").strip()
    with _lock:
        con = _jobs_db(db)
        try:
            known = [r[0] for r in con.execute("SELECT name FROM check_catalog").fetchall()]
            if name not in known:
                return {"error": f"unknown check {name!r}", "available": sorted(known)}
            recent = con.execute("SELECT id, finished_at FROM device_jobs WHERE check_name=? AND requested_at>? "
                                 "ORDER BY requested_at DESC LIMIT 1", (name, time.time() - CHECK_DEDUPE_SECONDS)).fetchone()
            if recent:
                return {"id": recent[0], "status": "reused", "finished": recent[1] is not None}
            jid = "job-" + hashlib.sha1(f"{time.time()}:{name}".encode()).hexdigest()[:12]
            con.execute("INSERT INTO device_jobs (id, check_name, requested_by, requested_at) VALUES (?,?,?,?)",
                        (jid, name, requested_by, time.time()))
            con.commit()
            return {"id": jid, "status": "queued"}
        finally:
            con.close()


def claim_jobs(limit: int = 3, db: Path | None = None) -> list[dict[str, Any]]:
    with _lock:
        con = _jobs_db(db)
        try:
            rows = con.execute("SELECT id, check_name, requested_at FROM device_jobs WHERE claimed_at IS NULL "
                               "ORDER BY requested_at LIMIT ?", (limit,)).fetchall()
            now = time.time()
            con.execute("INSERT INTO device_jobs_seen VALUES ('claim', ?) ON CONFLICT(k) DO UPDATE SET at=excluded.at", (now,))
            con.executemany("UPDATE device_jobs SET claimed_at=? WHERE id=?", [(now, r[0]) for r in rows])
            con.commit()
        finally:
            con.close()
    return [{"id": r[0], "check": r[1], "requested_at": r[2]} for r in rows]


def post_result(job_id: str, ok: bool, exit_code: int | None, output: str, duration_ms: int | None,
                db: Path | None = None) -> bool:
    with _lock:
        con = _jobs_db(db)
        try:
            cur = con.execute("UPDATE device_jobs SET finished_at=?, ok=?, exit_code=?, output=?, duration_ms=? "
                              "WHERE id=? AND finished_at IS NULL",
                              (time.time(), int(bool(ok)), exit_code, str(output or "")[-8000:], duration_ms, job_id))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


REPORT_MAX_AGE_SECONDS = 24 * 3600


def unreported_results(max_age: float = REPORT_MAX_AGE_SECONDS, db: Path | None = None) -> list[dict[str, Any]]:
    """Checks the agent started (run_check) that have finished since, not yet told to the user:
    the next chat turn reports them, since a turn can't come back later on its own."""
    con = _jobs_db(db)
    try:
        rows = con.execute("SELECT id, check_name, finished_at, ok, exit_code FROM device_jobs WHERE requested_by='agent' "
                           "AND finished_at IS NOT NULL AND reported_at IS NULL AND finished_at>? ORDER BY finished_at",
                           (time.time() - max_age,)).fetchall()
    finally:
        con.close()
    return [{"id": r[0], "check": r[1], "finished_at": r[2], "ok": bool(r[3]), "exit_code": r[4]} for r in rows]


def mark_reported(job_ids: list[str], db: Path | None = None) -> None:
    if not job_ids:
        return
    with _lock:
        con = _jobs_db(db)
        try:
            con.executemany("UPDATE device_jobs SET reported_at=? WHERE id=?", [(time.time(), j) for j in job_ids])
            con.commit()
        finally:
            con.close()


def last_claim(db: Path | None = None) -> float | None:
    """When Jellybean last asked for jobs (epoch seconds), or None if it never has."""
    con = _jobs_db(db)
    try:
        r = con.execute("SELECT at FROM device_jobs_seen WHERE k='claim'").fetchone()
        return r[0] if r else None
    finally:
        con.close()


def job(job_id: str, db: Path | None = None) -> dict[str, Any] | None:
    con = _jobs_db(db)
    try:
        r = con.execute("SELECT id, check_name, requested_at, claimed_at, finished_at, ok, exit_code, output, duration_ms "
                        "FROM device_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    keys = ("id", "check", "requested_at", "claimed_at", "finished_at", "ok", "exit_code", "output", "duration_ms")
    return dict(zip(keys, r))


def recent_jobs(last: int = 20, db: Path | None = None) -> list[dict[str, Any]]:
    con = _jobs_db(db)
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM device_jobs ORDER BY requested_at DESC LIMIT ?", (last,))]
    finally:
        con.close()
    return [j for j in (job(i, db) for i in ids) if j]
