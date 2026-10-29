#!/usr/bin/env python3
"""Text reschedules and cancels as owner-only suggestion cards (the titan half; the Mac reads its own texts).

The Mac's text-hook sync posts one derived, body-free fact per text that seems to move or cancel one of the person's
own calendar events (POST /v1/hooks, from their paired device):

  {"kind": "event_signal", "signal": "reschedule" | "cancel", "event_ref": "cx-calendar-<16 hex>",
   "proposed_start": "<ISO-8601 with an offset>" | null, "confidence": 0.0-1.0, "sig_id": "<16 hex>"}

  event_ref       titan's id for the event: "cx-calendar-" + sha1(source_id)[:16], source_id being the calendar
                  connector's own (the Mac's CalendarMapping.sourceID, "<identifier>@<occurrence start>"). That is
                  connectors_runner.item_id("calendar", source_id) for the default owner; anyone else's copy is keyed
                  "<person>:<source_id>" there, and either form is accepted for the poster's own events.
  sig_id          opaque (the Mac's sha1 of the message guid, first 16): dedup only, never shown.

Nothing else is accepted: no text, no sender, no handle, no other field (400). A shared household screen has no
personal items (403). Signals are kept in links.db's event_signals table, owned by the poster, private.

The suggestions pass (mail_suggestions.main_run, every 30 minutes with the bills) turns new signals into cards:
  - only the poster's own event, by its provenance owner (an event with no recorded owner matches no one);
  - nothing below confidence 0.6, for a reschedule with no proposed start, for an event not found, one already at
    the proposed start, or one that has started;
  - a mail invite's card proposing the same change to the same event (mailsug-inv-…) stands: no second card;
  - "Move “<title>” to Thu Oct 15, 12:00 PM?" (Update / Ignore) or "Remove “<title>”?" (Remove / Ignore), the title
    from the owner's own calendar record only, to the owner only, key textsig-<event id> (a later signal replaces
    it), expiring at the event's start. A tap is the owner's own turn through the calendar tools; nothing is written
    to a calendar without it.
Logs and stats carry counts only.
"""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

KIND = "event_signal"
FIELDS = frozenset({"kind", "signal", "event_ref", "proposed_start", "confidence", "sig_id"})
SIGNALS = ("reschedule", "cancel")
REF_RX = re.compile(r"cx-calendar-[0-9a-f]{16}")
SIG_RX = re.compile(r"[0-9a-f]{16}")
# an ISO-8601 time with its offset, nothing more (no free text rides along in a "date")
START_RX = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})")
AHEAD_DAYS = 60
MIN_CONFIDENCE = 0.6
KEY_PREFIX = "textsig-"
SOURCE = "text-signal"
ROLES = ("admin", "adult", "kid")

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_signals (owner TEXT NOT NULL, sig_id TEXT NOT NULL, signal TEXT NOT NULL,
    event_ref TEXT NOT NULL, proposed_start TEXT, confidence REAL NOT NULL, made_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'new', outcome TEXT, PRIMARY KEY (owner, sig_id));
CREATE INDEX IF NOT EXISTS idx_event_signals_state ON event_signals(state);
"""


class SignalError(ValueError):
    pass


def _now() -> datetime:
    import local_time
    return local_time.now()


def validate(body: Any, now: datetime | None = None) -> dict[str, Any]:
    """The signal, checked field by field; SignalError (its message names the field, never echoes a value)."""
    if not isinstance(body, dict):
        raise SignalError("an event signal is a JSON object")
    extra = set(body) - FIELDS
    if extra:
        raise SignalError("unknown fields: event signals carry only " + ", ".join(sorted(FIELDS)))
    missing = FIELDS - set(body)
    if missing:
        raise SignalError("missing fields: " + ", ".join(sorted(missing)))
    if body["kind"] != KIND:
        raise SignalError("kind must be event_signal")
    sig = body["signal"]
    if sig not in SIGNALS:
        raise SignalError("signal must be reschedule or cancel")
    ref = body["event_ref"]
    if not isinstance(ref, str) or not REF_RX.fullmatch(ref):
        raise SignalError("event_ref must be cx-calendar- and 16 lowercase hex characters")
    sid = body["sig_id"]
    if not isinstance(sid, str) or not SIG_RX.fullmatch(sid):
        raise SignalError("sig_id must be 16 lowercase hex characters")
    conf = body["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
        raise SignalError("confidence must be a number from 0 to 1")
    start = body["proposed_start"]
    iso = None
    if start is not None:
        import local_time
        at = local_time.parse_iso(start) if isinstance(start, str) and START_RX.fullmatch(start) else None
        if at is None or at.tzinfo is None:
            raise SignalError("proposed_start must be an ISO-8601 time with an offset, or null")
        now = now or _now()
        if not now <= at <= now + timedelta(days=AHEAD_DAYS):
            raise SignalError(f"proposed_start must be within the next {AHEAD_DAYS} days")
        iso = at.isoformat()
    return {"signal": sig, "event_ref": ref, "proposed_start": iso, "confidence": float(conf), "sig_id": sid}


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def put(person: str, body: Any, con: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """Keep one signal, owned by the poster. The same sig_id again replaces it (and is judged again)."""
    s = validate(body, now)
    ensure(con)
    con.execute("INSERT OR REPLACE INTO event_signals (owner, sig_id, signal, event_ref, proposed_start, confidence, "
                "made_at, state, outcome) VALUES (?,?,?,?,?,?,?,'new',NULL)",
                (person, s["sig_id"], s["signal"], s["event_ref"], s["proposed_start"], s["confidence"], time.time()))
    con.commit()
    return {"kind": KIND, "sig_id": s["sig_id"], "accepted": True}


# ── the pass ──────────────────────────────────────────────────────────────────────────────────────

def _ref(source_id: str) -> str:
    return "cx-calendar-" + hashlib.sha1(source_id.encode()).hexdigest()[:16]


def event_id(d: dict[str, Any], node_id: str | None = None) -> str:
    """An event's titan id: its provenance item_id, else the connectors rule from its owner and source_id, else the
    record node's id. "" when none can be told. The node id comes last (JEV's review of #1283): the mail invite path
    (mail_suggestions._invite) has only the event's data, so both paths reach the same id whenever it has provenance;
    a connector's record node is named by that same item id anyway."""
    import connectors_runner as cr
    prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
    if prov.get("item_id"):
        return str(prov["item_id"])
    sid, owner = str(prov.get("source_id") or ""), str(prov.get("owner") or "")
    if sid and owner:
        return cr.item_id("calendar", sid if owner == cr.OWNER_DEFAULT else f"{owner}:{sid}")
    return str(node_id or "")


def find(ref: str, owner: str, events: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | None:
    """The poster's own event this ref names → (its data, its titan id). Matched by its id, or by the hash of its
    raw source_id (the Mac's formula for any person); only events whose provenance owner is the poster."""
    import mail_suggestions as M
    for n in events:
        d = n.get("data") or {}
        if not owner or M._event_owner(d) != owner:
            continue
        eid = event_id(d, n.get("node_id"))
        prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
        sid = str(prov.get("source_id") or "")
        if ref in (eid, str(n.get("node_id") or "")) or (sid and _ref(sid) == ref):
            return d, eid or ref
    return None


def _local(iso: str | None) -> datetime | None:
    import local_time
    at = local_time.parse_iso(iso)
    if at is None:
        return None
    if at.tzinfo is not None:
        z = local_time.zone()
        at = (at.astimezone(z) if z is not None else at.astimezone()).replace(tzinfo=None)
    return at


def card(signal: str, ev_title: str, at: datetime, new: datetime | None, end: datetime | None
         ) -> tuple[str, str, list[dict[str, str]]]:
    """(title, detail, buttons). The title is the owner's own calendar record's; the buttons are mail_suggestions'
    calendar update and delete shapes, so a tap is the same turn a mail invite's card makes."""
    import mail_suggestions as M
    if signal == "cancel":
        _t, _d, buttons = M.change_card("remove", {"title": ev_title, "date": at.date().isoformat()}, ev_title, at)
        return (f"Remove “{ev_title}”?",
                f"A text says it's off. On your calendar: {M._when(at.date().isoformat(), f'{at:%H:%M}')}.", buttons)
    assert new is not None
    s: dict[str, Any] = {"title": ev_title, "date": new.date().isoformat(), "start": f"{new:%H:%M}"}
    if end is not None and end > at:
        new_end = new + (end - at)        # the event keeps its length
        if new_end.date() == new.date():
            s["end"] = f"{new_end:%H:%M}"
    title, _d, buttons = M.change_card("update", s, ev_title, at)
    return title, f"A text says it moves. On your calendar now: {M._when(at.date().isoformat(), f'{at:%H:%M}')}.", buttons


def _mail_card_same(cards: sqlite3.Connection, owner: str, eid: str, target: str, now: datetime) -> bool:
    """This owner's mail invite card proposing this same change to this same event, still live (only theirs: another
    person's card about the same event never stands in for this one)."""
    return cards.execute("SELECT 1 FROM cards WHERE key LIKE 'mailsug-%' AND owner=? AND event=? AND target=? AND state "
                         "IN ('held','sent') AND (expires IS NULL OR expires > ?)",
                         (owner, eid, target, f"{now:%Y-%m-%dT%H:%M}")).fetchone() is not None


def judge(row: dict[str, Any], events: list[dict[str, Any]], cards: sqlite3.Connection, now: datetime,
          apply: bool) -> str:
    """One signal → its outcome (a word). now: household time, naive."""
    import mail_suggestions as M
    owner = row["owner"]
    if row["confidence"] < MIN_CONFIDENCE:
        return "low-confidence"
    if row["signal"] == "reschedule" and not row["proposed_start"]:
        return "no-start"
    found = find(row["event_ref"], owner, events)
    if found is None:
        return "not-found"
    d, eid = found
    at = _local(d.get("start"))
    if at is None or at <= now:
        return "started" if at is not None else "no-time"
    ev_title = M._safe_event_title(d.get("title"))
    if not ev_title:
        return "unsafe-title"
    key = (KEY_PREFIX + eid)[:80]
    new = _local(row["proposed_start"]) if row["signal"] == "reschedule" else None
    if row["signal"] == "reschedule":
        if new is None or new <= now:
            return "past"
        if f"{new:%Y-%m-%dT%H:%M}" == f"{at:%Y-%m-%dT%H:%M}":
            if apply:   # it got there on its own: a card still waiting goes
                cards.execute("UPDATE cards SET state='withdrawn' WHERE key=? AND state='held'", (key,))
            return "same"
        target = f"{new:%Y-%m-%dT%H:%M}"
    else:
        target = "cancel"
    if _mail_card_same(cards, owner, eid, target, now):
        return "dup-mail"
    if apply:
        M._store(cards, key, "", owner, card(row["signal"], ev_title, at, new, _local(d.get("end"))),
                 f"{at:%Y-%m-%dT%H:%M}", event=eid, target=target)
    return "update" if row["signal"] == "reschedule" else "remove"


def process(con: sqlite3.Connection, events: list[dict[str, Any]], cards: sqlite3.Connection, now: datetime,
            roles: dict[str, str], apply: bool) -> dict[str, Any]:
    """The new signals → cards held in mail_suggestions' queue (sent by its send_held). → counts by outcome."""
    ensure(con)
    out: dict[str, int] = {}
    rows = con.execute("SELECT owner, sig_id, signal, event_ref, proposed_start, confidence FROM event_signals "
                       "WHERE state='new' ORDER BY made_at").fetchall()
    for owner, sig_id, signal, ref, start, conf in rows:
        if roles.get(owner) not in ROLES:
            outcome = "no-person"
        else:
            outcome = judge({"owner": owner, "signal": signal, "event_ref": ref, "proposed_start": start,
                             "confidence": float(conf)}, events, cards, now, apply)
        out[outcome] = out.get(outcome, 0) + 1
        if apply:
            con.execute("UPDATE event_signals SET state='done', outcome=? WHERE owner=? AND sig_id=?",
                        (outcome, owner, sig_id))
    if apply:
        con.commit()
        cards.commit()
    return {"signals": len(rows), "outcomes": out}
