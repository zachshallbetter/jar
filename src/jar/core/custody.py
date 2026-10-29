#!/usr/bin/env python3
"""Where the kids are staying: here, or with a parent outside the household (custody), as a state with an end.

09-30: titan read the Hamilton calendar's "Kids with Mom" backwards about ten times (it means the kids are here).
Zach then hid that calendar, and the signal was gone. He said in chat that Leo and Ava alternate weeks with
their dad, the hand-off Thursday at 9:00 (10-01 20:58; 10-02 18:05: at their dad's until Thu Oct 8, 9:00).

What's true at a moment, strongest first (each with who said it and where):
  1. a span a parent stated in chat ("with their dad until Thursday 9 AM") — kids_staying
  2. a span from a calendar event a rule names ("Kids with Mom" → here), read even when the calendar is hidden
     from the person's views: hidden isn't unsynced (jellybean#247); only a rule the admin set reads it
  3. the alternating pattern a parent stated (an anchor, who has the first stretch, its length)
Nothing is inferred: with none of these, there's no line.

"With their dad" is the kids' other parent (people store: `other_parent`, a label like "their dad"), never Zach or
another household adult. One line per turn ("Kids: …") for the household's adults and the kids themselves; never on
a shared device.

  python3 scripts/custody.py show
  python3 scripts/custody.py pattern --kids leo,ava --away-from 2026-10-01T09:00 --days 7 \
      --said-by zach --said "chat 10-02 18:05"           (a fleet write: only with the owner session's go)
  python3 scripts/custody.py rule "kids with mom" here --said-by zach
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = Path(os.environ.get("CUSTODY_DB", "/ai/datasets/connectors/custody.db"))
HERE, AWAY = "here", "away"
LONG = timedelta(hours=12)        # a calendar event this long is a state, not an appointment (household_coordination)
_lock = threading.Lock()
RANK = {"chat": 3, "calendar": 2, "pattern": 1}


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE IF NOT EXISTS spans (ref TEXT PRIMARY KEY, kids TEXT NOT NULL, state TEXT NOT NULL, "
                "start TEXT NOT NULL, end TEXT, source TEXT NOT NULL, said_by TEXT, said TEXT, added_at REAL NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS patterns (kids TEXT PRIMARY KEY, away_from TEXT NOT NULL, days INTEGER NOT NULL, "
                "said_by TEXT, said TEXT, added_at REAL NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS rules (title TEXT PRIMARY KEY, state TEXT NOT NULL, said_by TEXT, "
                "added_at REAL NOT NULL)")
    return con


def _iso(d: datetime) -> str:
    return d.isoformat(timespec="minutes")


def _dt(s: Any, zone=None) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None and zone is not None:
        d = d.replace(tzinfo=zone)
    return d


def _key(kids: Iterable[str]) -> str:
    return ",".join(sorted({str(k).lower() for k in kids if k}))


# ── writing ──────────────────────────────────────────────────────────────────────────────────────

def _restate(con: sqlite3.Connection, kids: set[str], state: str, start: datetime, end: datetime | None,
             ref: str) -> list[dict[str, Any]]:
    """A parent's newer chat span with the same state, against an earlier chat span it overlaps (#807). Custody never
    guesses, so only two shapes restate the stretch:
      - the new one starts at or inside the old one ("back Tuesday, not Thursday", said mid-stay): nothing else could
        be meant, since they were already down as that, so the old one ends where the new one starts (or goes, when
        both start together);
      - the new one covers the old one's tail (ends at or after it, or has no end): the old one ends where the new
        one starts, or goes when the new one covers it all.
    The new one starting before the old one and ending before its end ("away Fri night to Sat 10am" against "away
    Sat–Sun") may be an extra night, not a correction: the old one is left as it was and returned, so the reply can
    ask whether it's still on. A span with the other state is a break inside a stretch and trims nothing; kids the
    new span doesn't name keep the old span as it was. → the earlier spans left alone that the new one overlaps."""
    asked: list[dict[str, Any]] = []
    for r in con.execute("SELECT * FROM spans WHERE source='chat' AND state=? AND ref!=?", (state, ref)).fetchall():
        theirs = set(r["kids"].split(","))
        both = theirs & kids
        rs, re_ = _dt(r["start"], start.tzinfo), _dt(r["end"], start.tzinfo)
        if not both or rs is None or (end is not None and rs >= end) or (re_ is not None and re_ <= start):
            continue
        covers_tail = end is None or (re_ is not None and end >= re_)
        if start < rs and not covers_tail:
            asked.append(dict(r))
            continue
        rest = theirs - kids
        if rest:
            con.execute("INSERT OR REPLACE INTO spans (ref, kids, state, start, end, source, said_by, said, added_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (f"{r['ref']}~{_key(rest)}", _key(rest), r["state"], r["start"],
                                                       r["end"], "chat", r["said_by"], r["said"], r["added_at"]))
        if rs < start:
            con.execute("UPDATE spans SET kids=?, end=? WHERE ref=?", (_key(both), _iso(start), r["ref"]))
        else:
            con.execute("DELETE FROM spans WHERE ref=?", (r["ref"],))
    return asked


def set_span(kids: Iterable[str], state: str, start: datetime, end: datetime | None, source: str, said_by: str | None,
             said: str = "", ref: str | None = None, path: Path | None = None,
             left: list[dict[str, Any]] | None = None) -> str:
    """A stretch with a state (here | away), from a source (chat | calendar). → its ref. `left`: filled with the
    earlier chat spans it overlaps but doesn't restate (_restate), for the reply to ask about."""
    if state not in (HERE, AWAY) or source not in ("chat", "calendar"):
        raise ValueError("state is here or away; source is chat or calendar")
    k = _key(kids)
    ref = ref or f"{source}:{k}:{_iso(start)}"
    with _lock:
        con = _db(path)
        try:
            if source == "chat":
                asked = _restate(con, set(k.split(",")), state, start, end, ref)
                if left is not None:
                    left.extend(asked)
            con.execute("INSERT OR REPLACE INTO spans (ref, kids, state, start, end, source, said_by, said, added_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (ref, k, state, _iso(start), _iso(end) if end else None, source,
                                                        said_by, said, time.time()))
            con.commit()
        finally:
            con.close()
    return ref


def set_pattern(kids: Iterable[str], away_from: datetime, days: int, said_by: str | None, said: str = "",
                path: Path | None = None) -> None:
    """Alternating stretches of `days`: away from `away_from` for `days`, then here for `days`, and so on."""
    if not 1 <= int(days) <= 31:
        raise ValueError("a stretch is 1 to 31 days")
    with _lock:
        con = _db(path)
        try:
            con.execute("INSERT OR REPLACE INTO patterns (kids, away_from, days, said_by, said, added_at) VALUES (?,?,?,?,?,?)",
                        (_key(kids), _iso(away_from), int(days), said_by, said, time.time()))
            con.commit()
        finally:
            con.close()


def set_rule(title: str, state: str, said_by: str | None, path: Path | None = None) -> None:
    """A calendar event title that says where the kids are ("kids with mom" → here). Matched whole, any case."""
    if state not in (HERE, AWAY):
        raise ValueError("state is here or away")
    with _lock:
        con = _db(path)
        try:
            con.execute("INSERT OR REPLACE INTO rules (title, state, said_by, added_at) VALUES (?,?,?,?)",
                        (" ".join(str(title).lower().split()), state, said_by, time.time()))
            con.commit()
        finally:
            con.close()


def from_calendar(nodes: Iterable[dict[str, Any]], kids: list[str], zone=None, path: Path | None = None) -> int:
    """Calendar events a rule names become spans, whatever calendar they're on (a hidden calendar too: hidden from
    views isn't unsynced, jellybean#247). Only events of 12 hours or more: a state, not an appointment. → spans."""
    con = _db(path)
    try:
        rules = {r["title"]: r["state"] for r in con.execute("SELECT title, state FROM rules")}
    finally:
        con.close()
    if not rules or not kids:
        return 0
    n = 0
    for node in nodes:
        d = node.get("data") or {}
        state = rules.get(" ".join(str(d.get("title") or "").lower().split()))
        start, end = _dt(d.get("start"), zone), _dt(d.get("end"), zone)
        if not state or start is None or end is None or end - start < LONG:
            continue
        set_span(kids, state, start, end, "calendar", None, f"calendar “{d.get('title')}”",
                 ref=f"calendar:{node.get('node_id') or _iso(start)}", path=path)
        n += 1
    return n


# ── reading ──────────────────────────────────────────────────────────────────────────────────────

def state_at(kid: str, at: datetime, path: Path | None = None) -> dict[str, Any] | None:
    """{state, until, then, source, said_by, said} for one kid at a moment, or None when nothing says."""
    kid = str(kid).lower()
    con = _db(path)
    try:
        spans = [dict(r) for r in con.execute("SELECT * FROM spans") if kid in r["kids"].split(",")]
        pats = [dict(r) for r in con.execute("SELECT * FROM patterns") if kid in r["kids"].split(",")]
    finally:
        con.close()
    zone = at.tzinfo
    best = None
    for s in spans:
        a, b = _dt(s["start"], zone), _dt(s["end"], zone)
        if a and a <= at and (b is None or at < b):
            if best is None or (RANK[s["source"]], s["added_at"]) > (RANK[best["source"]], best["added_at"]):
                best = s
    if best is not None:
        until = _dt(best["end"], zone)
        nxt = (state_at(kid, until, path) or {}).get("state") if until else None
        return {"state": best["state"], "until": until, "then": nxt if nxt != best["state"] else None,
                "source": best["source"], "said_by": best["said_by"], "said": best["said"]}
    for p in sorted(pats, key=lambda p: p["added_at"], reverse=True):
        anchor = _dt(p["away_from"], zone)
        if anchor is None:
            continue
        span = timedelta(days=p["days"])
        k = (at - anchor) // span
        state = AWAY if k % 2 == 0 else HERE
        until = anchor + (k + 1) * span
        return {"state": state, "until": until, "then": HERE if state == AWAY else AWAY, "source": "pattern",
                "said_by": p["said_by"], "said": p["said"]}
    return None


def _when(d: datetime) -> str:
    h = d.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    return f"{d:%a %b} {d.day}, {h}"


def line(people: list[dict[str, Any]], viewer_role: str | None, at: datetime, path: Path | None = None) -> str | None:
    """'Kids: Leo and Ava are with their dad until Thu Oct 8, 9 AM, then here (Zach, chat 10-02 18:05).' for
    the household's adults and kids; None on a shared device or when nothing says."""
    if viewer_role not in ("admin", "adult", "kid"):
        return None
    names = {str(p.get("id")): str(p.get("name") or p.get("id")).split(" ")[0] for p in people}
    groups: dict[tuple, list[str]] = {}
    for p in people:
        if p.get("role") != "kid":
            continue
        s = state_at(str(p["id"]), at, path)
        if s is None:
            continue
        away = f"with {p.get('other_parent') or 'their other parent'}"
        k = (s["state"], s["until"], s["then"], s["said_by"], s["said"], s["source"], away)
        groups.setdefault(k, []).append(names[str(p["id"])])
    if not groups:
        return None
    parts = []
    for (state, until, then, by, said, source, away), kids in groups.items():
        who = kids[0] if len(kids) == 1 else ", ".join(kids[:-1]) + " and " + kids[-1]
        verb = "is" if len(kids) == 1 else "are"
        where = {HERE: "here", AWAY: away}
        txt = f"{who} {verb} {where[state]}"
        if until:
            txt += f" until {_when(until)}" + (f", then {where[then]}" if then else "")
        src = ", ".join(x for x in (names.get(str(by or "").lower(), by or ""), said) if x)
        parts.append(txt + (f" ({src})" if src else "") + (" — alternating" if source == "pattern" else ""))
    return "Kids (where they're staying; their other parent isn't anyone in this household): " + "; ".join(parts) + "."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    p = sub.add_parser("pattern")
    p.add_argument("--kids", required=True)
    p.add_argument("--away-from", required=True)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--said-by", required=True)
    p.add_argument("--said", default="")
    r = sub.add_parser("rule")
    r.add_argument("title")
    r.add_argument("state", choices=(HERE, AWAY))
    r.add_argument("--said-by", required=True)
    a = ap.parse_args()
    import local_time
    zone = local_time.zone()
    if a.cmd == "pattern":
        set_pattern(a.kids.split(","), _dt(a.away_from, zone), a.days, a.said_by, a.said)
    elif a.cmd == "rule":
        set_rule(a.title, a.state, a.said_by)
    import household_identity as hh
    print(line(hh.get_store().people(), "admin", datetime.now(zone)) or "Nothing says where the kids are staying.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
