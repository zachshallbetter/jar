#!/usr/bin/env python3
"""The family meeting (jellybean#425): the household's week on one shared screen, built in code.

A Sunday-evening agenda for the Apple TV (or any shared screen): last week, the week ahead day by day, where the kids
are staying and when that changes, and what needs sorting out (a kid's event nobody's free to drive). Each person
confirms their part from their own phone (next step: the cards). Everything here is the household's to see,
kids included (titan-node#418): an event's title is shown only when it's on a household calendar or shared with the
household; anyone else's is "busy". No model writes any of it.

  python3 scripts/family_meeting.py [--week YYYY-MM-DD]          the agenda (the week's Monday; default the coming one)
  python3 scripts/family_meeting.py [--week YYYY-MM-DD] --ask    the open driver questions to the adults' phones
  python3 scripts/family_meeting.py --start                      Sunday 6 PM (titan-family-meeting.timer): the
                                                                 questions, and the household's meeting card
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

LAST_WEEK_MAX = 8
DB = Path(os.environ.get("FAMILY_MEETING_DB", "/ai/datasets/connectors/family_meeting.db"))
_lock = threading.Lock()


def _clock(d: datetime) -> str:
    return d.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def week_of(today: date) -> date:
    """The Monday a Sunday meeting plans: tomorrow when it's Sunday, else the coming Monday (today if Monday)."""
    return today + timedelta(days=(7 - today.weekday()) % 7)


def fetch_nodes(start: date, days: int, tz, get: Callable[[str], dict] | None = None) -> list[dict]:
    """Appointment nodes touching the week (from a week before it, for multi-day states)."""
    if get is None:
        import agent_runtime_lib as lib
        get = lambda path: lib._runtime_request("record", path, "GET")   # noqa: E731
    lo = datetime.combine(start - timedelta(days=7), time(0), tz).astimezone(timezone.utc)
    hi = datetime.combine(start + timedelta(days=days), time(0), tz).astimezone(timezone.utc)
    path = (f"/v1/nodes?node_type=appointment&field=start&gte={lo:%Y-%m-%dT%H:%M:%SZ}"
            f"&lte={hi:%Y-%m-%dT%H:%M:%SZ}&limit=1000")
    return (get(path) or {}).get("nodes") or []


def _shown(ev) -> bool:
    return ev.audience == "household"


def day_block(day: date, nodes: list[dict], people: list[dict], tz, household: set[str],
              custody_at: Callable[[str, datetime], dict | None]) -> dict[str, Any]:
    """One day: its events as the household may see them, the kids' hand-offs that day, and what needs a driver."""
    import household_coordination as hc
    evs = hc.events_for(day, nodes, people, tz, household=household)
    items = []
    for ev in evs:
        who = " and ".join(ev.people)
        what = ev.title if _shown(ev) else "busy"
        items.append({"at": _clock(ev.start), "start": ev.start.isoformat(timespec="minutes"), "what": what,
                      "who": who, "text": f"{_clock(ev.start)} {who + ': ' if who else ''}{what}"})
    sort_out = []
    for c in hc.conflicts(evs, people):
        if c["need"] != "driver":
            continue          # a person's own overlap is theirs, on their phone (household_coordination), not here
        ev = next((e for e in evs if e.start.isoformat(timespec="minutes") == c["event"]["start"]
                   and e.title == c["event"]["title"]), None)
        what = ev.title if ev is not None and _shown(ev) else "something"
        at = _clock(ev.start) if ev is not None else ""
        key = f"{day.isoformat()}:{c['who'].lower().replace(' ', '-')}:{ev.start:%H%M}" if ev is not None else \
            f"{day.isoformat()}:{c['who'].lower().replace(' ', '-')}"
        sort_out.append({"key": key, "who": c["who"], "need": "driver", "at": at, "what": what,
                         "text": f"{c['who']} has {what} at {at}, and nobody's free to drive yet. Who's taking {c['who']}?"})
    handoffs = []
    kids = [p for p in people if p.get("role") == "kid"]
    lo = datetime.combine(day, time(0), tz)
    for p in kids:
        s = custody_at(str(p["id"]), lo)
        if s and s.get("until") is not None and lo <= s["until"] < lo + timedelta(days=1):
            handoffs.append((str(p.get("name") or "").split(" ")[0], s["until"], s.get("then"),
                             p.get("other_parent") or "their other parent"))
    hand = []
    for at in sorted({h[1] for h in handoffs}):
        group = [h for h in handoffs if h[1] == at]
        names = [h[0] for h in group]
        who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        where = "home" if group[0][2] == "here" else f"to {group[0][3]}'s"
        hand.append({"at": _clock(at), "text": f"{_clock(at)} {who} {where}"})
    return {"date": day.isoformat(), "label": f"{day:%a %b} {day.day}", "events": items, "handoffs": hand,
            "sort_out": sort_out}


def last_week(start: date, tz, events: list[dict] | None = None) -> list[str]:
    """What the household logged last week (household-audience timeline entries only), newest first."""
    import agent_runtime_lib as lib
    from reflection import _event_ts
    evs = events if events is not None else lib._timeline_events()
    lo = datetime.combine(start - timedelta(days=7), time(0), tz).timestamp()
    hi = datetime.combine(start, time(0), tz).timestamp()
    out = []
    for e in evs:
        pl = e.get("payload") if isinstance(e.get("payload"), dict) else {}
        if pl.get("audience") != "household" or pl.get("source"):
            continue          # the household's own notes; never a person's, never a connector's
        ts = _event_ts(e)
        if ts is not None and lo <= ts < hi and str(pl.get("text") or "").strip():
            out.append((ts, " ".join(str(pl["text"]).split())[:140]))
    return [t for _, t in sorted(out, reverse=True)[:LAST_WEEK_MAX]]


def agenda(start: date, nodes: list[dict], people: list[dict], tz, household: set[str],
           custody_at: Callable[[str, datetime], dict | None], timeline: list[dict] | None = None,
           custody_line: str | None = None, answers: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """answers: item key → {person id: take | pass} (the phones' answers, answers_for)."""
    names = {str(p["id"]): str(p.get("name") or p["id"]).split(" ")[0] for p in people}
    days = [day_block(start + timedelta(days=i), nodes, people, tz, household, custody_at) for i in range(7)]
    for d in days:
        for s in d["sort_out"]:
            said = (answers or {}).get(s["key"], {})
            taker = next((names.get(pid, pid) for pid, a in said.items() if a == "take"), None)
            s["passed"] = sorted(names.get(pid, pid) for pid, a in said.items() if a == "pass")
            if taker:
                s["taken_by"] = taker
                s["text"] = f"{taker}'s taking {s['who']} ({s['what']} at {s['at']})."
    sort_out = [dict(s, day=d["label"]) for d in days for s in d["sort_out"]]
    return {"week": start.isoformat(), "title": f"Week of {start:%b} {start.day}",
            "kids": custody_line, "last_week": last_week(start, tz, timeline), "days": days, "sort_out": sort_out}


# ── the phones' answers ──────────────────────────────────────────────────────────────────────────

def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        os.close(os.open(str(p), os.O_CREAT | os.O_WRONLY, 0o600))
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS answers (week TEXT NOT NULL, item TEXT NOT NULL, person TEXT NOT NULL, "
                "answer TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (week, item, person))")
    return con


def answers_for(week: str, path: Path | None = None) -> dict[str, dict[str, str]]:
    with _lock:
        con = _db(path)
        try:
            out: dict[str, dict[str, str]] = {}
            for item, person, ans in con.execute("SELECT item, person, answer FROM answers WHERE week=?", (week,)):
                out.setdefault(item, {})[person] = ans
            return out
        finally:
            con.close()


_ANSWER_RX = re.compile(r"^meeting (\d{4}-\d{2}-\d{2}) (\S+) (take|pass)$")


def answer(value: str, person: str | None, role: str | None, path: Path | None = None) -> tuple[bool, bool, str]:
    """A card button ("meeting <week> <item> take|pass"), in code: → (recorded, settled for everyone, what to say).
    Only the household's grown-ups drive; "I will" settles it for all of them, "Not me" only for the one who said it."""
    m = _ANSWER_RX.match(str(value or "").strip())
    if not m:
        return False, False, "That button isn't a family-meeting answer."
    if not person or role not in ("admin", "adult"):
        return False, False, "Only the household's grown-ups can take a drive."
    week, item, ans = m.groups()
    with _lock:
        con = _db(path)
        try:
            taken = con.execute("SELECT person FROM answers WHERE week=? AND item=? AND answer='take' AND person<>?",
                                (week, item, person)).fetchone()
            if ans == "take" and taken:
                return False, True, "Someone's already taking that one."
            con.execute("INSERT OR REPLACE INTO answers (week, item, person, answer, at) VALUES (?,?,?,?,?)",
                        (week, item, person, ans, _time.time()))
            con.commit()
        finally:
            con.close()
    return True, ans == "take", ("Got it: you're driving." if ans == "take" else "Noted: not you.")


def ask_cards(a: dict[str, Any], send: Callable[..., Any] | None = None) -> int:
    """Each open driver question as a card to the adults, with "I will" / "Not me" (proactive card_action → answer).
    One card per question and week (its key), so a re-run updates it. → cards sent."""
    if send is None:
        import proactive
        send = proactive.add_card
    n = 0
    for s in a["sort_out"]:
        if s.get("taken_by"):
            continue
        v = f"meeting {a['week']} {s['key']}"
        send(f"{s['day']}: who's taking {s['who']}?", s["text"], kind="suggestion", urgency=2, source="family-meeting",
             to="adults", key=f"meeting-{a['week']}-" + hashlib.sha1(s['key'].encode()).hexdigest()[:12],   # no name to Apple (10-07)
             expires_in=8 * 86400,
             actions=[{"label": "I will", "kind": "reply", "value": f"{v} take"},
                      {"label": "Not me", "kind": "reply", "value": f"{v} pass"}])
        n += 1
    return n


def start_meeting(a: dict[str, Any], send: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Sunday 6 PM (Zach, 10-02; titan-family-meeting.timer): the open driver questions to the adults' phones, and one
    household card that the meeting is on, saying only what the household may see: how many days have plans and how
    many things need sorting out. → {week, asked, card}."""
    if send is None:
        import proactive
        send = proactive.add_card
    asked = ask_cards(a, send)
    busy = sum(1 for d in a["days"] if d["events"])
    open_ = sum(1 for s in a["sort_out"] if not s.get("taken_by"))
    detail = (f"{busy} day{'s' if busy != 1 else ''} with plans"
              + (f", {open_} thing{'s' if open_ != 1 else ''} to sort out (the grown-ups have the questions on their phones)"
                 if open_ else ", nothing to sort out") + ".")
    card = send(f"Family meeting: {a['title'].replace('Week of', 'the week of')}", detail, kind="info", urgency=1,
                source="family-meeting", to="household", key=f"meeting-open-{a['week']}", expires_in=4 * 3600,
                # `open`: a client that has the meeting screen (the TV, jellybean#425) opens it; others ask the prompt
                actions=[{"label": "Open the week", "kind": "prompt", "open": "family-meeting",
                          "value": "(from a suggestion card) What's on the family meeting for this week?"}])
    return {"week": a["week"], "asked": asked, "card": (card or {}).get("id")}


def custody_on_shared() -> bool:
    """Whether a shared screen shows where the kids are staying. Off until Zach says (titan-node#654 kept the "Kids:"
    line to the household's people, never a shared device; the meeting on the TV is where it matters most)."""
    import os
    return os.environ.get("FAMILY_SCREEN_CUSTODY", "0").strip().lower() in ("1", "true", "yes", "on")


def build(start: date | None = None, role: str | None = "household") -> dict[str, Any]:
    """The agenda for a viewer: a shared screen (role household) leaves the kids' stays out unless custody_on_shared()."""
    import custody
    import household_identity as hh
    import local_time
    import world
    tz = local_time.zone()
    start = start or week_of(datetime.now(tz).date())
    people = hh.get_store().people()
    kids_ok = role in ("admin", "adult", "kid") or custody_on_shared()
    line = custody.line(people, "adult", datetime.combine(start, time(9), tz)) if kids_ok else None
    return agenda(start, fetch_nodes(start, 7, tz), people, tz, world.household_calendars(),
                  custody.state_at if kids_ok else (lambda kid, at: None), custody_line=line,
                  answers=answers_for(start.isoformat()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--week")
    ap.add_argument("--ask", action="store_true", help="send the open driver questions to the adults' phones")
    ap.add_argument("--start", action="store_true", help="Sunday 6 PM: the questions, and the household's meeting card")
    a = ap.parse_args()
    ag = build(date.fromisoformat(a.week) if a.week else None)
    if a.start:
        print(json.dumps(start_meeting(ag)))
        return 0
    if a.ask:
        print(json.dumps({"week": ag["week"], "asked": ask_cards(ag)}))
        return 0
    print(json.dumps(ag, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
