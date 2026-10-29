#!/usr/bin/env python3
"""Our year (jellybean#430, slice 1; contract approved 10-03): the household's past year for the TV, from what titan
already holds, in code. Always the household's view, whoever asks: a person's private items never appear.

  moments   past events on calendars the adults made the household's (title and place name only; never attendees,
            notes or links), kept once they've happened (remember(), from the world model's calendar sync: the sync
            looks ahead, so past events roll off titan); trips (such an event of 2 days or more); milestones (a household member's birthday the
            household may see, and household events whose titles name one: first day of school, graduation…)
  photos    per-month counts and days, summed from per-day counts each person's own phone reports for the albums
            they've shared with the household (opt-in; counts only: never an image, a name, a place or a face)
  never     where anyone was (places, arrivals, presence: the shared-device rule), custody, anything private

  GET  /v1/our-year?year=2026          any role; the household's view
  POST /v1/our-year/photos {days}      a person's own phone: {"YYYY-MM-DD": count}, replacing theirs for those days
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

DB = Path(os.environ.get("OUR_YEAR_DB", "/ai/datasets/connectors/our_year.db"))
MAX_PER_MONTH = 40
_lock = threading.Lock()
_MILESTONE_RX = re.compile(r"(?i)\b(first day of (?:school|kindergarten|\w+ grade)|last day of school|graduation|"
                           r"recital|tournament|championship|game day|big game|anniversary|birthday|concert|"
                           r"performance|play opening|ceremony|wedding|moving day)\b")
_LINK_RX = re.compile(r"(?i)\bhttps?://\S+|\bwww\.\S+")


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or os.environ.get("OUR_YEAR_DB") or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS photo_days (person TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, "
                "updated REAL NOT NULL, PRIMARY KEY (person, day))")
    # household moments once they've happened: the calendar sync looks ahead, so past events roll off titan
    con.execute("CREATE TABLE IF NOT EXISTS moments (date TEXT NOT NULL, key TEXT NOT NULL, moment TEXT NOT NULL, "
                "kept REAL NOT NULL, cal TEXT NOT NULL DEFAULT '', PRIMARY KEY (date, key))")
    return con


def remember(nodes: list[dict], household: set[str], tz, now: datetime | None = None,
             path: Path | None = None) -> dict[str, int]:
    """Keep the household's past moments (only what household_moments shows: title, date, kind, place name), from the
    world model's calendar sync (every 15 min), so the year outlives the sync window. → {kept}."""
    now = now or datetime.now(tz)
    rows = []
    for y in {now.year, now.year - 1}:
        for m in household_moments(nodes, household, tz, y, now.date(), keep_cal=True):
            cal = m.pop("_cal")
            rows.append((m["date"], m["title"].lower(), json.dumps(m), time.time(), cal))
    with _lock:
        con = _db(path)
        try:
            con.executemany("INSERT OR REPLACE INTO moments VALUES (?,?,?,?,?)", rows)
            # a calendar the adults no longer call the household's: what was kept from it goes
            gone = [c for (c,) in con.execute("SELECT DISTINCT cal FROM moments") if c not in household]
            dropped = sum(con.execute("DELETE FROM moments WHERE cal=?", (c,)).rowcount for c in gone)
            con.commit()
        finally:
            con.close()
    return {"kept": len(rows), "dropped": dropped}


def remembered(year: int, path: Path | None = None) -> list[dict[str, Any]]:
    con = _db(path)
    try:
        return [json.loads(r[0]) for r in con.execute("SELECT moment FROM moments WHERE date LIKE ? ORDER BY date",
                                                      (f"{year:04d}-%",))]
    finally:
        con.close()


def set_photo_days(person: str, days: Any, today: date | None = None, path: Path | None = None) -> int:
    """One person's per-day counts of household-shared photos (replaces theirs for those days). → days stored.
    Days must be real dates within the last 400 days (none in the future); counts 0-10000."""
    if not isinstance(days, dict) or len(days) > 400:
        raise ValueError("days is an object of up to 400 {\"YYYY-MM-DD\": count}")
    import local_time
    today = today or local_time.today()
    rows = []
    for k, v in days.items():
        try:
            d = date.fromisoformat(str(k))
        except ValueError:
            raise ValueError(f"not a date: {k!r}") from None
        if d > today or (today - d).days > 400:
            raise ValueError(f"out of range: {k}")
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10000:
            raise ValueError(f"count for {k} must be 0-10000")
        rows.append((str(person).lower(), d.isoformat(), v, time.time()))
    with _lock:
        con = _db(path)
        try:
            con.executemany("INSERT OR REPLACE INTO photo_days VALUES (?,?,?,?)", rows)
            con.commit()
        finally:
            con.close()
    return len(rows)


def photo_totals(year: int, path: Path | None = None) -> dict[str, int]:
    """day → the household's total (every person's counts summed): no person is named."""
    con = _db(path)
    try:
        return {d: n for d, n in con.execute("SELECT day, SUM(count) FROM photo_days WHERE day LIKE ? GROUP BY day",
                                             (f"{year:04d}-%",)) if n}
    finally:
        con.close()


def _clean(text: Any, limit: int) -> str:
    t = " ".join(_LINK_RX.sub("", str(text or "")).split())
    return t if len(t) <= limit else t[:limit - 1].rstrip() + "…"


def _parse(v: Any, tz) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(tz) if d.tzinfo else d.replace(tzinfo=tz)


def household_moments(nodes: list[dict], household: set[str], tz, year: int, today: date,
                      keep_cal: bool = False) -> list[dict[str, Any]]:
    """Past events on household calendars in `year`: event, trip (2+ days) or milestone (a milestone title)."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for n in nodes:
        d = n.get("data") or {}
        cal = str(d.get("calendar") or "").strip().lower()
        if not cal or cal not in household:
            continue                       # only what the adults made the household's
        s, e = _parse(d.get("start"), tz), _parse(d.get("end"), tz)
        if not s or s.year != year or s.date() > today:
            continue
        title = _clean(d.get("title"), 80)
        if not title:
            continue
        last = (e - timedelta(seconds=1)).date() if e and e > s else s.date()
        days = (last - s.date()).days + 1
        m: dict[str, Any] = {"date": s.date().isoformat(), "title": title,
                             "kind": "trip" if days >= 2 else ("milestone" if _MILESTONE_RX.search(title) else "event")}
        if days >= 2:
            m["end"] = min(last, today).isoformat()
        where = _clean(d.get("location"), 60).split(",")[0].strip()
        if where:
            m["where"] = where
        if keep_cal:
            m["_cal"] = cal          # remember()'s own: never in an answer
        out.setdefault((m["date"], title.lower()), m)
    return sorted(out.values(), key=lambda x: (x["date"], x["title"]))


_MONTHS = {m.lower(): i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                              "August", "September", "October", "November", "December"], 1)}


def _birthday(value: Any) -> tuple[int, int, int | None] | None:
    """(month, day, year or None) from a birthday value: 2014-10-10, October 10, Oct 10, 2014."""
    v = str(value or "").strip()
    try:
        d = date.fromisoformat(v[:10])
        return d.month, d.day, d.year
    except ValueError:
        pass
    m = re.match(r"(?i)^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?$", v)
    if m:
        mon = next((i for name, i in _MONTHS.items() if name.startswith(m.group(1).lower()[:3])), None)
        if mon:
            return mon, int(m.group(2)), int(m.group(3)) if m.group(3) else None
    return None


def birthday_moments(birthdays: list[tuple[str, Any]], year: int, today: date) -> list[dict[str, Any]]:
    """(first name, birthday value) the household may see → past milestones this year ("Ava turns 12")."""
    out = []
    for name, value in birthdays:
        b = _birthday(value)
        if not b:
            continue
        mon, day, born = b
        try:
            d = date(year, mon, day)
        except ValueError:
            continue
        if d > today:
            continue
        first = _clean(name, 30).split(" ")[0]
        title = f"{first} turns {year - born}" if born and 0 < year - born < 120 else f"{first}'s birthday"
        out.append({"date": d.isoformat(), "title": title, "kind": "milestone"})
    return out


def build(year: int, today: date, moments: list[dict[str, Any]], photos: dict[str, int],
          now_iso: str = "") -> dict[str, Any]:
    """The answer: months (newest first) with moments and photo counts, and a year ago today."""
    by_month: dict[str, dict[str, Any]] = {}
    for m in sorted(moments, key=lambda x: (x["date"], x["title"])):
        mo = m["date"][:7]
        if not mo.startswith(f"{year:04d}"):
            continue
        slot = by_month.setdefault(mo, {"month": mo, "moments": [], "photos": {"count": 0, "days": 0}})
        if len(slot["moments"]) < MAX_PER_MONTH:
            slot["moments"].append(m)
    for d, n in photos.items():
        if not d.startswith(f"{year:04d}") or d > today.isoformat():
            continue
        slot = by_month.setdefault(d[:7], {"month": d[:7], "moments": [], "photos": {"count": 0, "days": 0}})
        slot["photos"]["count"] += n
        slot["photos"]["days"] += 1
    return {"year": year, "months": [by_month[k] for k in sorted(by_month, reverse=True)],
            "a_year_ago_today": [], "generated_at": now_iso}


def gather(year: int | None = None, now: datetime | None = None, *,
           nodes: list[dict] | None = None, household: set[str] | None = None,
           birthdays: list[tuple[str, Any]] | None = None, photos_path: Path | None = None) -> dict[str, Any]:
    """The household's view of `year` (default this year), from the live sources unless they're given (tests)."""
    import local_time
    tz = local_time.zone()
    now = now or datetime.now(tz)
    today = now.date()
    year = year or today.year
    if nodes is None:
        import agent_runtime_lib as lib
        nodes = lib.record_nodes("appointment")[0]
    if household is None:
        import world
        household = {str(c).lower() for c in world.household_calendars()}
    if birthdays is None:
        birthdays = household_birthdays()
    def year_of(y: int) -> list[dict[str, Any]]:
        live = household_moments(nodes, household, tz, y, today)
        have = {(m["date"], m["title"].lower()) for m in live}
        kept = [m for m in remembered(y, photos_path) if (m["date"], m["title"].lower()) not in have]
        return live + kept + birthday_moments(birthdays, y, today)
    out = build(year, today, year_of(year), photo_totals(year, photos_path), now.isoformat(timespec="seconds"))
    ago = today.replace(year=today.year - 1) if not (today.month == 2 and today.day == 29) else today - timedelta(days=366)
    out["a_year_ago_today"] = [m for m in year_of(ago.year) if m["date"] == ago.isoformat()][:6]
    return out


def household_birthdays() -> list[tuple[str, Any]]:
    """(name, birthday) of the household's people as the household sees them in the world model (its rules)."""
    try:
        import world as W
        w = W.get()
        out = []
        hh = w.world("household") if hasattr(w, "split") else w
        for eid, row in hh.entities():
            if row["type"] != "person":
                continue
            cid = f"household:{eid}" if hasattr(w, "split") else eid
            if not w.visible_entity(cid, "household", "household"):
                continue
            for f in w.facts(cid, "household", "household"):   # what a shared screen may see (kids_screen's view)
                if f.attr == "birthday" and f.values:
                    out.append((row["name"], f.values[0].value))
        return out
    except Exception:  # noqa: BLE001 - no world model: no birthdays
        return []
