#!/usr/bin/env python3
"""Notices and appointments in scans link to calendar events (titan-node#1503, epic #1498 slice 4).

A scan that carries a date, a time or a place for something to attend or do (an appointment reminder, a school or club
event, jury duty, a "return the form by" deadline, a renewal) is one of two things for its owner:

  link   the owner's calendar already has it (the same day, plus a word of the sender's name or the place): the scan gets an
         `about` edge to that event (graph_edges: measured, accepted), and nothing is asked.
  card   it is not there: the owner gets an "Add to calendar?" card, built like the card a plan agreed over texts makes
         (event_proposals.card's buttons; #1363). The card is a PROPOSAL. Nothing is written to a calendar until the owner
         taps Add, which is their own turn through the calendar tools. Once the event exists, the next pass finds it
         with the same rule and links the scan to it (the same pass again is a no-op: this module holds no state).

Reused, not rebuilt: the envelope (scan_envelope: type, sender, dates), dedupe (scan_dedupe: one proposal per cluster, so a
rescan never asks twice), event_proposals (confidence floor, card words, title span), mail_suggestions (_store, the card queue;
_safe_event_title; the title-word rule of on_calendar), graph_edges (Edge, prov), audience (who sees an edge), local_time.

Whose it is, absolutely:
  - the owner is the scanner (the envelope's owner), never a printed name (#1428). The card goes to the owner alone.
  - a KID's scan goes to the kid's parents (people.parents, the rule behind schedule_digest: parents manage kids' schedules),
    never to the kid, never to anyone else; a kid with no parent set gets no card at all (fail closed).
  - a shared household screen or an unknown person gets nothing.
  - an edge's audience is the owner (and a kid's parents); the event it links to must be the owner's own (or, for a kid, the
    kid's or a parent's), so a scan never links to, or reveals, someone else's event.
  - text that tries to instruct (the envelope's instruction_like) proposes nothing; the title is built from the sender and
    the type, cleaned (mail_suggestions._safe_event_title), never copied from the body.

Every date is the household's (local_time: AGENT_TZ, never titan's UTC). `now` is passed in; a card for the past is not made.

  python3 scripts/scans_score.py --pipeline scan_notices:pipeline
  python3 scripts/scans_score.py --pipeline scan_notices:pipeline --write scripts/evalsets/scans/notices.json
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_dedupe as D  # noqa: E402
import scan_envelope as E  # noqa: E402

VERSION = 1
KEY_PREFIX = "scannotice-"
NOTICE_TYPES = ("appointment", "deadline")
ADULT_ROLES = ("admin", "adult")
# a plain letter is a notice only when it says so: jury duty, a hearing, a renewal, "scheduled for <day>"
_LETTER_CUE = re.compile(r"(?i)\b(jury (?:duty|service)|summons|hearing|must be renewed by|renewal (?:is )?due|scheduled for|report (?:on|by))\b")
_DATE_AFTER = re.compile(r"(?i)\b(?:on|by|for|before|until)\s+" + E._DATE_RX)
_TIME = re.compile(r"(?i)\bat (\d{1,2}):(\d{2})\s*(am|pm)?\b")
_PLACE = re.compile(r"(?im)^\s*(?:location|where|place)\s*:\s*([^\n]{3,80})$")

# the benchmark's household (scans_bench): three adults and two kids whose parents are Alex and Robin
BENCH_PEOPLE: dict[str, dict[str, Any]] = {
    "alex": {"role": "admin", "name": "Alex Hale"}, "robin": {"role": "adult", "name": "Robin Hale"},
    "jordan": {"role": "adult", "name": "Jordan Hale"},
    "ava": {"role": "kid", "name": "Ava Hale", "parents": ["alex", "robin"]},
    "leo": {"role": "kid", "name": "Leo Hale", "parents": ["alex", "robin"]},
}


def people_from(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The household's people (household_identity's store.people()) as {id: {role, name, parents}}. Nothing else is kept."""
    return {str(r["id"]): {"role": r.get("role"), "name": r.get("name") or "", "parents": list(r.get("parents") or [])}
            for r in rows if r.get("id")}


# ── reading a notice ─────────────────────────────────────────────────────────────────────────────────────────────
def reading(scan: dict[str, Any], env: dict[str, Any]) -> dict[str, Any] | None:
    """{kind, day, start, place} when the scan is a notice with a day to put on a calendar, else None. By rules (the
    envelope's fields first, then the date the text puts after "on" or "by"); a model is not involved."""
    f = env["fields"]
    text = str(scan.get("text") or "")
    typ = f["type"]
    if f.get("instruction_like"):
        return None
    if typ == "letter" and not _LETTER_CUE.search(text):
        return None
    if typ not in NOTICE_TYPES + ("letter",):
        return None
    if typ == "deadline":
        day = f["due_date"]
    else:
        day = f["event_date"]
    if not day:
        m = _DATE_AFTER.search(text)
        day = E._iso(m.group(m.lastindex)) if m else None
    if not day:
        return None
    start = None
    tm = _TIME.search(text)
    if tm and typ != "deadline":
        h, mi, ap = int(tm.group(1)), int(tm.group(2)), (tm.group(3) or "").lower()
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if 0 <= h < 24 and 0 <= mi < 60:
            start = f"{h:02d}:{mi:02d}"
    pm = _PLACE.search(text)
    place = pm.group(1).strip() if pm else None
    return {"kind": "deadline" if typ == "deadline" else "appointment", "day": day, "start": start, "place": place}


def key_of(sender: str | None, kind: str, day: str) -> str:
    """The key the scorer's benchmark files an event under: event:<sender>:<day>, plus :deadline for a deadline."""
    k = f"event:{D.E._slug(sender or '')}:{day}"
    return k + ":deadline" if kind == "deadline" else k


def title_of(sender: str | None, kind: str) -> str | None:
    import mail_suggestions as M
    s = M._safe_event_title(sender or "")
    if not s:
        return None
    return M._safe_event_title(f"{s} deadline" if kind == "deadline" else f"Appointment at {s}")


# ── whose it is ──────────────────────────────────────────────────────────────────────────────────────────────────
def recipients(owner: str, people: dict[str, dict[str, Any]]) -> list[str]:
    """Who may be asked: an adult, only themselves; a kid, only their parents who are adults here (nobody when none are set);
    a shared screen or someone unknown, nobody."""
    who = people.get(owner)
    if not who:
        return []
    role = who.get("role")
    if role in ADULT_ROLES:
        return [owner]
    if role == "kid":
        return sorted({p for p in who.get("parents") or [] if (people.get(p) or {}).get("role") in ADULT_ROLES})
    return []


def audience_of(owner: str, people: dict[str, dict[str, Any]]) -> str:
    """The edge's audience: the owner, and a kid's parents (who manage the kid's schedule). Never wider."""
    import audience
    who = people.get(owner) or {}
    if who.get("role") == "kid":
        return audience.normalize([owner, *recipients(owner, people)])
    return "private"


def _owner_of_event(n: dict[str, Any]) -> str:
    d = n.get("data") or {}
    prov = d.get("provenance") if isinstance(d.get("provenance"), dict) else {}
    return str(prov.get("owner") or "")


def find_event(events: list[dict[str, Any]], allowed: set[str], day: str, title: str, place: str | None) -> dict[str, Any] | None:
    """An event of an allowed person's on that day sharing a word of the title (the sender's name) with its title or location, or the notice's place."""
    import mail_suggestions as M
    words = M._words(title) - M.GENERIC
    pwords = M._words(place or "")
    for n in sorted(events, key=lambda x: str(x.get("node_id"))):
        d = n.get("data") or {}
        if _owner_of_event(n) not in allowed or str(d.get("start") or "")[:10] != day:
            continue
        have = M._words(d.get("title") or "")
        where = M._words(d.get("location") or "")
        if words & (have | where) or (pwords and pwords & (have | where)):
            return n
    return None


# ── the pass ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _household_naive(now: datetime) -> datetime:
    import local_time
    z = local_time.zone()
    if now.tzinfo is None:
        return now
    return (now.astimezone(z) if z is not None else now.astimezone()).replace(tzinfo=None)


def plan(scans: list[dict[str, Any]], envs: dict[str, dict[str, Any]], dedupe: dict[str, Any], now: datetime,
         events: list[dict[str, Any]] | None = None, people: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One decision per notice cluster (the kept copy), in a stable order. `now`: the household's clock."""
    people = BENCH_PEOPLE if people is None else people
    events = events or []
    by_id = {s["id"]: s for s in scans}
    now_h = _household_naive(now)
    members: dict[str, list[str]] = defaultdict(list)
    for sid, root in dedupe["cluster"].items():
        members[root].append(sid)
    out: list[dict[str, Any]] = []
    for root in sorted(members):
        scan, env = by_id[root], envs[root]
        r = reading(scan, env)
        if not r:
            continue
        owner = env["record"]["owner"]
        sender = env["fields"]["sender"]
        title = title_of(sender, r["kind"])
        d: dict[str, Any] = {"scan": root, "members": sorted(members[root]), "owner": owner, "kind": r["kind"], "day": r["day"],
                             "start": r["start"], "key": key_of(sender, r["kind"], r["day"]), "title": title,
                             "place": r["place"], "recipients": [], "action": "skip", "event": None}
        out.append(d)
        to = recipients(owner, people)
        if not title:
            d["action"] = "unsafe-title"
            continue
        if not to:
            d["action"] = "no-recipient"
            continue
        at = datetime.fromisoformat(r["day"] + ("T" + r["start"] if r["start"] else "T23:59"))
        if at <= now_h and r["start"] or (not r["start"] and at.date() < now_h.date()):
            d["action"] = "past"
            continue
        allowed = {owner, *to}
        found = find_event(events, allowed, r["day"], title, r["place"])
        if found:
            d.update(action="link", event=str(found.get("node_id")), recipients=to)
        else:
            d.update(action="card", recipients=to)
    return out


def edges(decisions: list[dict[str, Any]], envs: dict[str, dict[str, Any]], people: dict[str, dict[str, Any]] | None = None) -> list[dict]:
    """The `about` edges for the decisions that link: scan -> calendar event, measured, accepted, owner-only (graph_edges)."""
    import graph_edges as G
    people = BENCH_PEOPLE if people is None else people
    out = []
    for d in decisions:
        if d["action"] != "link":
            continue
        env = envs[d["scan"]]
        src = [G.prov("scans", d["scan"], d["owner"], audience_of(d["owner"], people), env["record"].get("observed_at"), item=d["scan"])]
        out.append(G.Edge(d["scan"], "about", f"event:{d['event']}", plane="measured", state="accepted", rung="attributed",
                          conf=0.7, why="same day and name as the event on the calendar", src=src))
    return out


def card(d: dict[str, Any], people: dict[str, dict[str, Any]], household_cal: str | None = None) -> tuple[str, str, list[dict[str, str]]]:
    """(title, detail, buttons): the words of event_proposals.card for a scan. All-day when the notice names no time."""
    import mail_suggestions as M
    span = M.title_span(d["title"])
    place = d.get("place") or ""
    loc = ""
    if place:
        import event_proposals as EP
        loc = place if EP.PLACE_RX.fullmatch(place) and not M.IMPERATIVE.search(place) else M.title_span(place)
    tail = f", location: {loc}" if loc else ""
    if d["start"]:
        when = M._when(d["day"], d["start"])
        base = f"{M.CARD_PROMPT_MARK} Create a calendar event {span} on {d['day']} from {d['start']}{tail}"
    else:
        when = M._when(d["day"])
        base = f"{M.CARD_PROMPT_MARK} Create an all-day calendar event {span} on {d['day']}{tail}"
    kid = (people.get(d["owner"]) or {}).get("role") == "kid"
    whose = f"{str((people.get(d['owner']) or {}).get('name') or 'a child').split(' ')[0]}'s" if kid else "your"
    title = f"“{d['title']}”, {when}, is in {whose} scanned mail. Add to calendar?"
    detail = f"From {whose} scanned mail." + (f" Where: {place}." if place else "")
    buttons = [{"label": "Add", "kind": "prompt", "value": (base + " on my calendar.")[:300]}]
    if household_cal:
        buttons.append({"label": "Add to the household calendar", "kind": "prompt",
                        "value": (base + f" on the household calendar {M.title_span(household_cal)}.")[:300]})
    buttons.append({"label": "Not now", "kind": "dismiss"})
    return title, detail, buttons


def queue(decisions: list[dict[str, Any]], cards: sqlite3.Connection, people: dict[str, dict[str, Any]] | None = None,
          household_cal: str | None = None) -> int:
    """Hold the cards in mail_suggestions' queue (its send_held sends them), one per recipient, keyed so the same notice
    again replaces its card. Nothing else is written. → cards held."""
    import mail_suggestions as M
    people = BENCH_PEOPLE if people is None else people
    n = 0
    for d in decisions:
        if d["action"] != "card":
            continue
        for who in d["recipients"]:
            key = KEY_PREFIX + hashlib.sha1(f"{who}\x00{d['key']}".encode()).hexdigest()[:16]
            expires = f"{d['day']}T{d['start'] or '23:59'}"
            hcal = household_cal if (people.get(who) or {}).get("role") in ADULT_ROLES else None
            n += M._store(cards, key, d["scan"], who, card(d, people, hcal), expires, event=None, target=f"{d['day']}T{d['start'] or '00:00'}")
    cards.commit()
    return n


# ── the scorer's pipeline ────────────────────────────────────────────────────────────────────────────────────────
def pipeline(scans: list[dict], now: datetime | None = None, events: list[dict[str, Any]] | None = None,
             people: dict[str, dict[str, Any]] | None = None) -> dict:
    """The dedupe pipeline's prediction, plus: every notice's event key (assoc: event) and, for a deadline still ahead,
    Waiting on you. Nothing is written: the calendar is `events` (none, on the benchmark) and the cards are only listed."""
    return stage(scans, D.pipeline(scans), now, events, people)


def stage(scans: list[dict], pred: dict, now: datetime | None = None, events: list[dict[str, Any]] | None = None,
          people: dict[str, dict[str, Any]] | None = None) -> dict:
    """Notices as a stage: takes a prediction that already has the dedupe (pred["dedupe"]); touches only the notices."""
    import local_time
    import scans_bench as B
    envs = {sid: d["envelope"] for sid, d in pred["docs"].items()}
    now = now or datetime.combine(date.fromisoformat(B.AS_OF), datetime.min.time().replace(hour=9), tzinfo=local_time.zone())
    people = BENCH_PEOPLE if people is None else people
    decisions = plan(scans, envs, pred["dedupe"], now, events, people)
    now_h = _household_naive(now)
    for d in decisions:
        for sid in d["members"]:
            doc = pred["docs"][sid]
            doc["assoc"] = {"kind": "event", "key": d["key"]}
            if d["kind"] == "deadline" and date.fromisoformat(d["day"]) >= now_h.date():
                doc["waiting"] = True
    pred["notices"] = [{k: d[k] for k in ("scan", "owner", "kind", "day", "key", "action", "recipients", "event")} for d in decisions]
    pred["edges"] = edges(decisions, envs, people)
    return pred
