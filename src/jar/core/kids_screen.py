#!/usr/bin/env python3
"""The kids' screen (jellybean#422): each kid's household chores for today, checked off on the TV, with a streak.

A chore is a household activity (the world model's household bubble) with a schedule and an `assignee` fact naming
the kid ("Ava takes out the recycling", said by an adult). Its id is "ch-<activity id>". Due today when its schedule
falls on today; a `when` condition (kids away / here, #687) and the kid being at their other parent's (custody) are
read only where custody may be (people's own devices, or FAMILY_SCREEN_CUSTODY=1). Without custody to read, a chore
is shown as usual: nothing on this screen depends on knowing where a kid is.

The one write a household screen gets: POST /v1/home/kids/chores/{id}/done {done}. Idempotent, today's occurrence of
an assigned household chore only, by a household device, the kid themselves, or the admin; logged with device,
person and time. Nothing else is writable through it.

  sleeps   where the kid sleeps tonight, only with custody allowed
  streak   consecutive days with every chore that kid had done (a day with none doesn't break it)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

DB = Path(os.environ.get("KIDS_CHORES_DB", "/ai/datasets/connectors/kids_chores.db"))
STREAK_DAYS = 60
_lock = threading.Lock()


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS done (chore TEXT NOT NULL, kid TEXT NOT NULL, day TEXT NOT NULL, "
                "done INTEGER NOT NULL, device TEXT, person TEXT, at REAL NOT NULL, PRIMARY KEY (chore, kid, day))")
    con.execute("CREATE TABLE IF NOT EXISTS log (chore TEXT, kid TEXT, day TEXT, done INTEGER, device TEXT, person TEXT, "
                "at REAL)")
    return con


def _clock(d: datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def kid_chores(w, day: date, people: list[dict], state_at: Callable[[str, datetime], dict | None] | None,
               zone=None) -> dict[str, list[dict[str, Any]]]:
    """kid id → [{id, text, at, start, timed, ritual?}] due that day, in time order (untimed last)."""
    import world as W
    hh = w.world("household") if hasattr(w, "split") else w
    kids = {str(p.get("name") or "").split(" ")[0].lower(): p for p in people if p.get("role") == "kid"}
    out: dict[str, list[dict]] = {str(p["id"]): [] for p in kids.values()}
    for eid, row in hh.entities():
        if row["type"] != "activity":
            continue
        cid = f"household:{eid}" if hasattr(w, "split") else eid
        facts = w.facts(cid, "household", "household")
        who = [kids[v.value.strip().split(" ")[0].lower()] for f in facts if f.attr == "assignee" for v in f.values
               if v.value.strip().split(" ")[0].lower() in kids]
        if not who:
            continue
        ritual = next((v.value for f in facts if f.attr == "ritual" for v in f.values
                       if v.value in ("morning", "bedtime")), None)
        for f in facts:
            if f.attr != "schedule":
                continue
            for v in f.values:
                parsed = W.parse_schedule(v.value)
                if not parsed or day.weekday() not in parsed[0]:
                    continue
                import re
                hhmm = parsed[1] or ("21:00" if re.search(r"\bnight", v.value, re.I) else None)
                at = datetime.combine(day, datetime.strptime(hhmm or "00:00", "%H:%M").time(), zone)
                cond = (f.qual or {}).get("when")
                for k in who:
                    s = state_at(str(k["id"]), at) if state_at else None
                    if s and s.get("state") == "away":
                        continue          # at their other parent's then: not their chore that day
                    if cond and s and s.get("state") != ("away" if cond == "kids away" else "here"):
                        continue
                    out[str(k["id"])].append({"id": f"ch-{eid}", "text": row["name"], "at": _clock(at) if hhmm else None,
                                              "start": at.isoformat(timespec="minutes") if hhmm else None,
                                              "timed": hhmm is not None,
                                              **({"ritual": ritual} if ritual else {})})
    for k in out:
        out[k].sort(key=lambda c: (not c["timed"], c["start"] or "", c["text"]))
    return out


def done_on(day: date, path: Path | None = None) -> set[tuple[str, str]]:
    with _lock:
        con = _db(path)
        try:
            return {(c, k) for c, k in con.execute("SELECT chore, kid FROM done WHERE day=? AND done=1", (day.isoformat(),))}
        finally:
            con.close()


def streak(kid: str, today: date, due: Callable[[date], list[dict]], path: Path | None = None) -> int:
    """Consecutive days, back from today (today counts only once all its chores are done), with every chore done. A
    day with no chores doesn't break it."""
    n = 0
    for back in range(STREAK_DAYS):
        d = today - timedelta(days=back)
        chores = due(d)
        if not chores:
            continue
        done = done_on(d, path)
        if all((c["id"], kid) in done for c in chores):
            n += 1
        elif back == 0:
            continue          # today isn't over: it doesn't break the streak yet
        else:
            break
    return n


def sleeps(kid: dict, now: datetime, state_at: Callable[[str, datetime], dict | None]) -> str | None:
    """"Here tonight", "At their dad's tonight", or a change by tomorrow ("At their dad's from 9 AM tomorrow")."""
    s = state_at(str(kid["id"]), now.replace(hour=21, minute=0))
    if not s or not s.get("state"):
        return None
    other = kid.get("other_parent") or "their other parent"
    tonight = "Here tonight" if s["state"] == "here" else f"At {other}'s tonight"
    until = s.get("until")
    if until is not None and until <= now.replace(hour=0, minute=0) + timedelta(days=2):
        when = "tomorrow" if until.date() == now.date() + timedelta(days=1) else "today" if until.date() == now.date() else ""
        if when:
            nxt = "Home" if s.get("then") == "here" else f"At {other}'s"
            return f"{tonight}; {nxt.lower() if nxt == 'Home' else nxt} from {_clock(until)} {when}"
    return tonight


def build(now: datetime, people: list[dict], chores_for: Callable[[date], dict[str, list[dict]]],
          state_at: Callable[[str, datetime], dict | None] | None = None, path: Path | None = None) -> dict[str, Any]:
    today = chores_for(now.date())
    done = done_on(now.date(), path)
    kids = []
    for p in [p for p in people if p.get("role") == "kid"]:
        pid = str(p["id"])
        items = [dict(c, done=(c["id"], pid) in done) for c in today.get(pid, [])]
        nxt = next((c for c in items if not c["done"]), None)
        k = {"id": pid, "name": str(p.get("name") or "").split(" ")[0],
             "chores": [{k_: c[k_] for k_ in ("id", "text", "at", "done", "ritual") if k_ in c} for c in items],
             "next": (nxt["text"] + (f" · {nxt['at']}" if nxt["at"] else "")) if nxt else None,
             "streak": streak(pid, now.date(), lambda d, pid=pid: chores_for(d).get(pid, []), path)}
        if state_at is not None:
            sl = sleeps(p, now, state_at)
            if sl:
                k["sleeps"] = sl
        kids.append(k)
    return {"kids": kids}


def mark(chore: str, done: bool, now: datetime, people: list[dict], chores_for: Callable[[date], dict[str, list[dict]]],
         person: str | None, role: str | None, device: str | None, path: Path | None = None) -> tuple[int, dict[str, Any]]:
    """The checkmark (POST /v1/home/kids/chores/{id}/done). → (HTTP status, body). Today's occurrence only; a kid only
    for their own chore; a household device or the admin for any kid's. Idempotent; every call is logged."""
    if role not in ("household", "kid", "admin"):
        return 403, {"error": "only a household screen or the kid can check off a chore"}
    today = chores_for(now.date())
    whose = [kid for kid, items in today.items() if any(c["id"] == chore for c in items)]
    if not whose:
        return 404, {"error": "no such chore due today"}
    if role == "kid":
        whose = [k for k in whose if k == person]
        if not whose:
            return 403, {"error": "that's someone else's chore"}
    day = now.date().isoformat()
    with _lock:
        con = _db(path)
        try:
            for kid in whose:
                con.execute("INSERT OR REPLACE INTO done (chore, kid, day, done, device, person, at) VALUES (?,?,?,?,?,?,?)",
                            (chore, kid, day, int(bool(done)), device, person, _time.time()))
                con.execute("INSERT INTO log (chore, kid, day, done, device, person, at) VALUES (?,?,?,?,?,?,?)",
                            (chore, kid, day, int(bool(done)), device, person, _time.time()))
            con.commit()
        finally:
            con.close()
    return 200, {"chore": chore, "done": bool(done), "kids": whose}


def _live() -> tuple[datetime, list[dict], Callable, Callable | None]:
    import household_identity as hh
    import local_time
    import world as W
    tz = local_time.zone()
    now = datetime.now(tz)
    people = hh.get_store().people()
    return now, people, tz, W.get()


def _custody_for(role: str | None) -> Callable | None:
    """Custody for this viewer, as Home reads it: people's own devices, or a shared screen with FAMILY_SCREEN_CUSTODY."""
    try:
        import custody
        import family_meeting
        if role in ("admin", "adult", "kid") or family_meeting.custody_on_shared():
            return custody.state_at
    except Exception:  # noqa: BLE001
        pass
    return None


def gather(role: str | None) -> dict[str, Any]:
    now, people, tz, w = _live()
    state_at = _custody_for(role)
    return build(now, people, lambda d: kid_chores(w, d, people, state_at, zone=tz), state_at)


def mark_live(chore: str, done: bool, person: str | None, role: str | None, device: str | None) -> tuple[int, dict]:
    """The checkmark, judged on the same chores the screen shows (the same custody view), so a tap always matches."""
    now, people, tz, w = _live()
    state_at = _custody_for(role)
    return mark(chore, done, now, people, lambda d: kid_chores(w, d, people, state_at, zone=tz), person, role, device)


if __name__ == "__main__":
    print(json.dumps(gather("household"), indent=1))
