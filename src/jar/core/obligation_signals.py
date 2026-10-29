#!/usr/bin/env python3
"""Text-derived payment evidence for obligation threads (#951 step 4, the titan half; the Mac reads its own texts).

The Mac asks titan which of the person's obligations a text could be about (GET /v1/obligations/watch): their own
visible threads as opaque account refs with the vendor, amount, due day and state titan already holds. It reads its
texts on the device, and for one that confirms a payment or reports a failed one, posts one derived, body-free fact
(POST /v1/hooks, from the paired device):

  {"kind": "obligation_signal", "signal": "paid" | "payment_failed", "account_ref": "acct-<16 hex>",
   "amount": 48.2 | null, "on": "2026-10-08", "confidence": 0.0-1.0, "sig_id": "<16 hex>"}

  account_ref  from the watch list: "acct-" + sha1(account)[:16], the account being titan's own normalised vendor name
               (obligations.account_of); only one of the poster's own visible obligations is accepted (404 otherwise).
  amount       the amount the text names, or null; dollars, 0 to 1,000,000.
  on           the day the text says it happened (ISO day), not in the future, not over a year old.
  sig_id       opaque (the Mac's sha1 of the message guid, first 16): dedup only, never shown.

Nothing else is accepted: no text, no sender, no handle, no vendor name, no other field (400). A shared household
screen has no personal items, and a kid has no bills (403). Signals are kept in links.db's obligation_signals table,
owned by the poster, private: the same sig_id again replaces it.

Settlement: a signal at confidence 0.6 or more becomes a dated piece of the poster's own view of that obligation
("paid Oct 8 (a text)"), weighed like a receipt or a failure notice: evidence, never the person's word (#508), and
never on anyone else's view, whatever the bill's audience. Logs and stats carry counts only.
"""
from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import time
from datetime import date, timedelta
from typing import Any, Callable

import local_time

KIND = "obligation_signal"
FIELDS = frozenset({"kind", "signal", "account_ref", "amount", "on", "confidence", "sig_id"})
SIGNALS = {"paid": "paid", "payment_failed": "failed"}   # → the piece kind obligations settles with
REF_RX = re.compile(r"acct-[0-9a-f]{16}")
SIG_RX = re.compile(r"[0-9a-f]{16}")
DAY_RX = re.compile(r"\d{4}-\d{2}-\d{2}")
MIN_CONFIDENCE = 0.6
MAX_AGE_DAYS = 366
MAX_AMOUNT = 1_000_000
ROLES = ("admin", "adult")

SCHEMA = """
CREATE TABLE IF NOT EXISTS obligation_signals (owner TEXT NOT NULL, sig_id TEXT NOT NULL, signal TEXT NOT NULL,
    account TEXT NOT NULL, amount REAL, on_day TEXT NOT NULL, confidence REAL NOT NULL, made_at REAL NOT NULL,
    PRIMARY KEY (owner, sig_id));
"""


class SignalError(ValueError):
    def __init__(self, detail: str, status: int = 400) -> None:
        super().__init__(detail)
        self.status = status


def ref(account: str) -> str:
    """An account's opaque ref: never the name itself on the wire back from the device."""
    return "acct-" + hashlib.sha1(str(account).encode()).hexdigest()[:16]


def watch(threads: list[Any], open_states: tuple[str, ...]) -> list[dict[str, Any]]:
    """The viewer's obligations a text could be about: their visible open threads, newest cycle per account, as
    {account_ref, vendor, amount, due, state}. Only what titan already shows this viewer; never an owner or a source."""
    best: dict[str, Any] = {}
    for t in threads:
        if t.state not in open_states:
            continue
        held = best.get(t.account)
        if held is None or t.cycle > held.cycle:
            best[t.account] = t
    return [{"account_ref": ref(a), "vendor": t.vendor, "amount": t.amount,
             "due": t.due.isoformat() if t.due else None, "state": t.state}
            for a, t in sorted(best.items(), key=lambda kv: (kv[1].due or kv[1].cycle, kv[0]))]


def validate(body: Any, today: date) -> dict[str, Any]:
    """The signal, checked field by field; SignalError (its message names the field, never echoes a value)."""
    if not isinstance(body, dict):
        raise SignalError("an obligation signal is a JSON object")
    extra = set(body) - FIELDS
    if extra:
        raise SignalError("unknown fields: obligation signals carry only " + ", ".join(sorted(FIELDS)))
    missing = FIELDS - set(body)
    if missing:
        raise SignalError("missing fields: " + ", ".join(sorted(missing)))
    if body["kind"] != KIND:
        raise SignalError("kind must be obligation_signal")
    if body["signal"] not in SIGNALS:
        raise SignalError("signal must be paid or payment_failed")
    r = body["account_ref"]
    if not isinstance(r, str) or not REF_RX.fullmatch(r):
        raise SignalError("account_ref must be acct- and 16 lowercase hex characters")
    sid = body["sig_id"]
    if not isinstance(sid, str) or not SIG_RX.fullmatch(sid):
        raise SignalError("sig_id must be 16 lowercase hex characters")
    conf = body["confidence"]
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not math.isfinite(conf) or not 0 <= conf <= 1:
        raise SignalError("confidence must be a number from 0 to 1")
    amt = body["amount"]
    if amt is not None and (isinstance(amt, bool) or not isinstance(amt, (int, float)) or not math.isfinite(amt)
                            or not 0 <= amt <= MAX_AMOUNT):
        raise SignalError(f"amount must be a number from 0 to {MAX_AMOUNT:,}, or null")
    on = body["on"]
    try:
        day = date.fromisoformat(on) if isinstance(on, str) and DAY_RX.fullmatch(on) else None
    except ValueError:
        day = None
    if day is None:
        raise SignalError("on must be an ISO day (YYYY-MM-DD)")
    if not today - timedelta(days=MAX_AGE_DAYS) <= day <= today + timedelta(days=1):
        raise SignalError(f"on must be within the last {MAX_AGE_DAYS} days, and not in the future")
    return {"signal": body["signal"], "account_ref": r, "amount": None if amt is None else round(float(amt), 2),
            "on": day.isoformat(), "confidence": float(conf), "sig_id": sid}


def ensure(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def put(person: str, role: str | None, body: Any, con: sqlite3.Connection,
        accounts: Callable[[], dict[str, str]], today: date | None = None) -> dict[str, Any]:
    """Keep one signal, owned by the poster, private. accounts(): the poster's own visible obligations as
    {account_ref: account} (computed only after the shape is valid). The same sig_id again replaces it."""
    if role not in ROLES:
        raise SignalError("bills are an adult's own", 403)
    s = validate(body, today or local_time.today())
    acct = (accounts() or {}).get(s["account_ref"])
    if not acct:
        raise SignalError("account_ref isn't one of your obligations", 404)
    ensure(con)
    con.execute("INSERT OR REPLACE INTO obligation_signals (owner, sig_id, signal, account, amount, on_day, confidence, "
                "made_at) VALUES (?,?,?,?,?,?,?,?)",
                (person, s["sig_id"], s["signal"], acct, s["amount"], s["on"], s["confidence"], time.time()))
    con.commit()
    return {"kind": KIND, "sig_id": s["sig_id"], "accepted": True}


def of(person: str | None, con: sqlite3.Connection) -> list[dict[str, Any]]:
    """The viewer's own signals that count (confidence ≥ MIN_CONFIDENCE), as obligations reads them:
    [{account, date, kind (paid | failed), amount}]. Never anyone else's."""
    if not person:
        return []
    ensure(con)
    rows = con.execute("SELECT signal, account, amount, on_day FROM obligation_signals WHERE owner=? AND confidence>=? "
                       "ORDER BY on_day, made_at", (person, MIN_CONFIDENCE)).fetchall()
    return [{"account": a, "date": d, "kind": SIGNALS[s], "amount": amt} for s, a, amt, d in rows if s in SIGNALS]
