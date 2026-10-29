#!/usr/bin/env python3
"""Calendar writes without the Mac (#1612): held until a device is back, out-of-date ones expire, never a silent wait.

10-08 to 10-13, Zach's turns: calendar changes waited on a Mac that was asleep and timed out (turns of 249 s, 246 s,
123 s), and an earlier held action was approved after its event had moved, re-creating the old time. Here:

  hold()          a calendar write that provably no device of the person's took (their Mac didn't wake or answer, the turn
                  has no device) is kept as a durable held action (held_actions kind "device_wait") with a card on
                  their own devices: "I'll add it when your Mac or iPhone is back", with Do it (run it now, from
                  whichever device they're on: the iPhone runs calendar writes itself) and Don't.
  drain()         every runner tick: a person's Mac that is connected again gets their waiting writes, oldest first,
                  through cross_device like a scheduled action (the Mac's own trust settings still decide whether it
                  asks; when it does, nobody is there, so the card asks them to tap Do it instead).
  check()         a calendar write a device took and never answered: it may have happened, so it is never resent; a
                  check reads that day back when the Mac is there, and the card offers Check now (a fresh read).
  expire_stale()  a held calendar write (either kind) whose event time has passed, or a waiting one older than
                  WAIT_MAX_S, ends with a card saying so, instead of firing late.

Which devices serve calendar writes (agent_runtime_lib): the Mac runs every device tool; an iPhone runs create, update
and delete in its own turn (PHONE_DEVICE_TOOLS, when it reports them). titan reaches an iPhone in the background only
for item_body (#776), so a waiting write is sent in the background to the Mac only; on the iPhone it goes when the
person taps Do it there.

Privacy: rows, cards and the reply are the owner's only (to=[owner]); a card names what and when, never the notes,
location or attendees. Nothing here reads someone else's rows: a person's call always passes their own id.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import held_actions  # noqa: E402
import local_time  # noqa: E402

CALENDAR_WRITES = frozenset({"create_calendar_event", "update_calendar_event", "delete_calendar_event"})
WAIT_MAX_S = 24 * 3600        # a write waiting for a device longer than this is out of date: it's asked again, not sent
RETRY_S = 300.0               # a Mac that went again before it took the write: tried again no sooner than this
SOURCE = "titan"

# Two kinds of no-answer, kept apart (Codex on #1680). NOT_DELIVERED: no device ever took the call (cross_device's
# NO_WAKE, which it returns only after withdrawing the request undelivered; a turn with no device; scheduled's NO_MAC):
# nothing can have happened, so it is held and sent later. MAYBE_DONE: a device took it and never said how it went
# (cross_device's NO_ANSWER comes after delivery; the turn hook's own timeouts come after the app was sent the
# request): it may have happened, so it is never resent; the person is told so, and a check reads the calendar back
# when a device is there. A refusal, a decline or a failure on the device is an answer, and in neither list.
_NOT_DELIVERED = ("Your Mac didn't wake up", "No device is connected to this turn", "titan can't reach your Mac")
_MAYBE_DONE = ("Your Mac didn't answer in time", "Confirmation timed out", "No result from the device")

_VERB = {"create_calendar_event": ("add it", "add the event", "added"),
         "update_calendar_event": ("make that change", "change the event", "changed"),
         "delete_calendar_event": ("cancel it", "cancel the event", "cancelled")}


def _bare(result: Any) -> str:
    text = str(result or "")
    p = "Device action was not completed: "
    return text[len(p):] if text.startswith(p) else text


def not_delivered(result: Any) -> bool:
    """Whether a device call's result proves no device took it: safe to hold and send later."""
    return _bare(result).startswith(_NOT_DELIVERED)


def maybe_done(result: Any) -> bool:
    """Whether a device took the call and never answered: it may have happened, so it's never resent."""
    return _bare(result).startswith(_MAYBE_DONE)


def unreachable(result: Any) -> bool:
    """Either kind of no-answer (not a refusal, a decline or a failure)."""
    return not_delivered(result) or maybe_done(result)


# An event's identity, as a hold's `match` (Codex on #1680: a title alone made Monday's and Tuesday's Dentist one
# event). "id:<event_id>" when the call names one; else "occ:" + title, day and time ("" when only a day is known),
# joined by a unit separator. A create's is the occurrence it would make; a change's or a cancel's the one it aims at.
_SEP = "\x1f"


def match_key(tool: str, args: dict[str, Any]) -> str | None:
    """The event a calendar write is about (see above), or None."""
    if tool not in CALENDAR_WRITES:
        return None
    a = args or {}
    if str(a.get("event_id") or "").strip():
        return "id:" + str(a["event_id"]).strip()
    title = " ".join(str(a.get("title") or "").split()).lower()
    when = str((a.get("start") if tool == "create_calendar_event" else a.get("on")) or "").strip()
    if not title or not when:
        return None
    ts = _at(when, end_of_day=False)
    if ts is None:
        return None
    day = local_time.clock(ts, "%Y-%m-%d")
    hm = "" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", when) else local_time.clock(ts, "%H:%M")
    return _SEP.join(("occ:" + title, day, hm))


def same_event(target: str | None, held: str | None) -> bool:
    """Whether a change or cancel aimed at `target` is about the event an older hold (`held`) is about: the same
    event_id; or the same title on the same day, and the same time when both name one."""
    if not target or not held:
        return False
    if target.startswith("id:") or held.startswith("id:"):
        return target == held
    t, h = target.split(_SEP), held.split(_SEP)
    if len(t) != 3 or len(h) != 3 or t[0] != h[0] or t[1] != h[1]:
        return False
    return not t[2] or not h[2] or t[2] == h[2]


def _supersede(owner: str, tool: str, args: dict[str, Any], *, keep: str | None = None, now: float | None = None,
               db: Path | None = None) -> list[dict[str, Any]]:
    """Only a change or a cancel supersedes, and only holds on its own event: two creates with one title are two
    events, never revisions of one another."""
    if tool == "create_calendar_event":
        return []
    target = match_key(tool, args)
    if not target:
        return []
    return held_actions.supersede(owner, lambda m: same_event(target, m), keep=keep, now=now, db=db)


def _at(value: Any, *, end_of_day: bool) -> float | None:
    """An ISO date or date/time from a tool call as an epoch, in the household's zone when it has none. A bare date:
    the end of that day when end_of_day (the event is on it some time), else its start."""
    s = str(value or "").strip()
    if not s:
        return None
    z = local_time.zone()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            d = datetime.strptime(s, "%Y-%m-%d")
        except ValueError:
            return None
        if end_of_day:
            d = d + timedelta(days=1)
        d = d.replace(tzinfo=z) if z is not None else d.astimezone()
        return d.timestamp()
    d = local_time.parse_iso(s)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=z) if z is not None else d.astimezone()
    return d.timestamp()


def stale_at(tool: str, args: dict[str, Any]) -> float | None:
    """When a held calendar write stops making sense: its event's time has passed. A new event: its start (a date
    only: the end of that day). A change or a cancel: the earliest of the day it's on and a new start. A repeating
    one (a new series, or "this and every later one"): None, only the age bound applies."""
    a = args or {}
    if tool not in CALENDAR_WRITES or a.get("repeat") or str(a.get("span") or "").lower() == "future":
        return None
    if tool == "create_calendar_event":
        return _at(a.get("start"), end_of_day=True)
    times = [t for t in (_at(a.get("on"), end_of_day=True), _at(a.get("start"), end_of_day=True)) if t is not None]
    return min(times) if times else None


def when_text(tool: str, args: dict[str, Any]) -> str:
    """The day and time a card names (never the title, notes, place or people), in the household's zone."""
    a = args or {}
    raw = a.get("start") if tool == "create_calendar_event" else (a.get("on") or a.get("start"))
    s = str(raw or "").strip()
    if not s:
        return ""
    ts = _at(s, end_of_day=False)
    if ts is None:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return local_time.clock(ts, "%a %b %-d")
    return local_time.clock(ts, "%a %b %-d, %-I:%M %p")


def _card(card: Callable[..., Any] | None, owner: str, hid: str, title: str, detail: str, *, actions: Any = None,
          expires_in: float = 86400.0) -> None:
    if card is None:
        import proactive
        card = proactive.add_card
    try:
        card(title, detail, kind="info", urgency=1, source=SOURCE, to=[owner], key=f"held-{hid}",
             expires_in=expires_in, actions=actions)
    except Exception as exc:  # noqa: BLE001 - the row stands; a missing card never runs or drops it
        print(f"[calendar-hold] card failed: {type(exc).__name__}", file=sys.stderr)


def _buttons(hid: str) -> list[dict[str, str]]:
    return [{"label": "Do it now", "kind": "prompt", "value": f"Yes, do held action {hid}"},
            {"label": "Don't", "kind": "prompt", "value": f"Drop held action {hid}"}]


def hold(owner: str, tool: str, args: dict[str, Any], *, now: float | None = None, db: Path | None = None,
         card: Callable[..., Any] | None = None) -> str:
    """Keep a calendar write no device could take, for its owner, with a card. → its id. An older hold on the same
    event is superseded (its card says so)."""
    now = now or time.time()
    key = match_key(tool, args)
    stale = stale_at(tool, args)
    hid = held_actions.put(owner, tool, args, now=now, db=db, kind="device_wait", ttl=WAIT_MAX_S + 3600,
                           stale_at=stale, match=key)
    for old in _supersede(owner, tool, args, keep=hid, now=now, db=db):
        _card(card, owner, old["id"], "Replaced by a newer change",
              "You asked for a newer change to this event, so the earlier one won't be done.", expires_in=6 * 3600)
    when = when_text(tool, args)
    verb = _VERB[tool][1]
    until = min(WAIT_MAX_S, (stale - now) if stale else WAIT_MAX_S)
    _card(card, owner, hid, "Waiting for your Mac or iPhone",
          f"I'll {verb}{f' ({when})' if when else ''} when your Mac or iPhone is back. Nothing has changed on your "
          "calendar yet. Do it now runs it from the device you tap it on.",
          actions=_buttons(hid), expires_in=max(60.0, until))
    return hid


def check(owner: str, tool: str, args: dict[str, Any], *, now: float | None = None, db: Path | None = None,
          card: Callable[..., Any] | None = None) -> str:
    """A calendar write a device took and never answered: it may or may not have happened, so it's never resent. A
    check (held_actions kind "check", the call kept only to know which day to read) reads that day back when the
    person's Mac is there, and a card says so meanwhile, with a Check now that asks for the day in a fresh turn.
    → its id."""
    now = now or time.time()
    hid = held_actions.put(owner, tool, args, now=now, db=db, kind="check", ttl=WAIT_MAX_S + 3600)
    day = _check_day(tool, args)
    shown = local_time.clock(_at(day, end_of_day=False), "%a %b %-d") if day else ""
    verb = _VERB[tool][1]
    acts = ([{"label": "Check now", "kind": "prompt", "value": f"What's on my calendar on {shown}?"}] if shown else None)
    _card(card, owner, hid, "Not sure it went through",
          f"I asked your device to {verb}{f' ({when_text(tool, args)})' if when_text(tool, args) else ''}, and it "
          "never said whether it did, so it may or may not have happened. I won't send it again; I'll read your "
          f"calendar{f' for {shown}' if shown else ''} back when your Mac is there.", actions=acts, expires_in=WAIT_MAX_S)
    return hid


def _check_day(tool: str, args: dict[str, Any]) -> str | None:
    """The day a check reads (ISO date, household zone): where the event is or would be."""
    a = args or {}
    for k in (("start",) if tool == "create_calendar_event" else ("on", "start")):
        ts = _at(a.get(k), end_of_day=False)
        if ts is not None:
            return local_time.clock(ts, "%Y-%m-%d")
    return None


def check_reply(tool: str) -> str:
    """The tool result the model reads when a device took the write and never answered."""
    return ("Not confirmed: the person's device took this calendar change but never said whether it did it, so it may "
            "or may not have happened. Tell them exactly that, plainly, in one line, and that titan will read their "
            "calendar back to check when their Mac is there (a card has Check now). Don't try it again: a second try "
            "could make it twice.")


def reply(tool: str) -> str:
    """The tool result the model reads for a held write: what to tell the person, plainly."""
    short = _VERB.get(tool, _VERB["create_calendar_event"])[0]
    return ("Not done yet: none of the person's devices that can change the calendar (their Mac or iPhone) is "
            f"reachable right now, so titan is holding it. Tell them exactly this, in one line: \"I'll {short} when "
            "your Mac or iPhone is back.\" Nothing has changed on the calendar yet; a card lets them do it sooner "
            "or call it off. Don't try again this turn.")


def superseded_by_write(owner: str, tool: str, args: dict[str, Any], *, now: float | None = None,
                        db: Path | None = None, card: Callable[..., Any] | None = None) -> int:
    """A calendar write that went through: older holds on the same event end (they'd re-do an old version)."""
    n = 0
    for old in _supersede(owner, tool, args, now=now, db=db):
        n += 1
        _card(card, owner, old["id"], "Replaced by a newer change",
              "That event was changed since, so the earlier held change won't be done.", expires_in=6 * 3600)
    return n


def stale_reason(r: dict[str, Any], now: float) -> str | None:
    """Why a held calendar write is out of date, or None."""
    if r.get("stale_at") is not None and float(r["stale_at"]) <= now:
        return "time"
    if r.get("kind") in ("device_wait", "check") and now - float(r["created_at"]) > WAIT_MAX_S:
        return "age"
    return None


def expire_stale(*, now: float | None = None, db: Path | None = None, card: Callable[..., Any] | None = None) -> list[str]:
    """Every held calendar write that's out of date ends, with a card to its owner. → their ids."""
    now = now or time.time()
    out = []
    for r in held_actions.pending(tools=CALENDAR_WRITES, now=now, db=db):
        why = stale_reason(r, now)
        if not why or not held_actions.expire(r["id"], now=now, db=db):
            continue
        out.append(r["id"])
        verb, when = _VERB[r["tool"]][1], when_text(r["tool"], r["args"])
        if r["kind"] == "check":
            _card(card, r["owner"], r["id"], "Couldn't check your calendar",
                  "Your Mac wasn't back within a day, so I couldn't read it back. Ask me what's on that day to check.",
                  expires_in=86400)
            continue
        detail = (f"Its time{f' ({when})' if when else ''} passed before it could be done, so I didn't {verb} late."
                  if why == "time" else
                  f"It waited more than a day for your Mac or iPhone, so I didn't {verb}{f' ({when})' if when else ''}.")
        _card(card, r["owner"], r["id"], "Didn't happen: out of date", detail + " Ask me again if you still want it.",
              expires_in=86400)
    return out


def drain(*, live: Callable[[str], bool], send: Callable[[dict[str, Any]], tuple[str, str]],
          read: Callable[[dict[str, Any], str], tuple[str, str]] | None = None,
          now: float | None = None, db: Path | None = None, card: Callable[..., Any] | None = None) -> list[dict[str, str]]:
    """Waiting writes whose owner's Mac is connected again (live(owner)) go to it, oldest first, each once.
    send(row) → (status, text): "done", "needs_ok" (the Mac wants a yes; nobody's there), "unreachable" (it went
    again), or "failed". A check row is never sent: read(row, day) reads that day back (same statuses) for a card.
    → [{id, status}]."""
    now = now or time.time()
    out: list[dict[str, str]] = []
    expire_stale(now=now, db=db, card=card)
    gone: set[str] = set()
    rows = [r for r in held_actions.pending(tools=CALENDAR_WRITES, now=now, db=db) if r["kind"] in ("device_wait", "check")]
    for r in rows:
        owner = r["owner"]
        if owner in gone or (r.get("next_try_at") or 0) > now or not live(owner):
            continue
        if not held_actions.claim(r["id"], now=now, db=db):
            continue
        if r["kind"] == "check":   # never the write again: only the day it's on, read back
            day = _check_day(r["tool"], r["args"])
            try:
                status, text = read(r, day) if (read is not None and day) else ("failed", "")
            except Exception as exc:  # noqa: BLE001
                status, text = "unreachable", type(exc).__name__
            if status == "unreachable":
                held_actions.release(r["id"], retry_at=now + RETRY_S, db=db)
                gone.add(owner)
            else:
                shown = local_time.clock(_at(day, end_of_day=False), "%a %b %-d") if day else "that day"
                _card(card, owner, r["id"], f"Checked your calendar for {shown}",
                      ("Here's what's on it, so you can see whether the change went through: "
                       + " ".join(str(text or "").split())[:900]) if status == "done" else
                      "I couldn't read it back. Ask me what's on that day to check.", expires_in=86400)
            out.append({"id": r["id"], "status": "checked" if status == "done" else status})
            continue
        try:
            status, text = send(r)
        except Exception as exc:  # noqa: BLE001 - an error is a retry, never a lost write
            status, text = "unreachable", type(exc).__name__
        verb, when = _VERB[r["tool"]], when_text(r["tool"], r["args"])
        at = f" ({when})" if when else ""
        if status == "done":
            _card(card, owner, r["id"], f"Done: {verb[1]}{at}",
                  f"Your Mac was back, so it's {verb[2]} now.", expires_in=86400)
        elif status == "needs_ok":
            held_actions.release(r["id"], retry_at=float("inf"), kind="confirm", db=db)
            _card(card, owner, r["id"], "Your Mac is back: tap to finish",
                  f"Your Mac asks before it'll {verb[1]}{at}. Tap Do it now to finish it, or Don't.",
                  actions=_buttons(r["id"]), expires_in=max(60.0, (r.get("stale_at") or now + 86400) - now))
        elif status == "unreachable":
            held_actions.release(r["id"], retry_at=now + RETRY_S, db=db)
            gone.add(owner)   # its Mac went again: the rest wait for the next tick
        else:
            _card(card, owner, r["id"], f"Couldn't {verb[1]}{at}",
                  "Your Mac tried and it didn't work: " + " ".join(str(text or "").split())[:200], expires_in=86400)
        out.append({"id": r["id"], "status": status})
    return out


def drop(hid: str, owner: str, *, now: float | None = None, db: Path | None = None,
         card: Callable[..., Any] | None = None) -> str:
    """A held card's Don't: the owner's own pending hold ends. The reply, in a sentence."""
    if not held_actions.ID_RX.match(str(hid or "")):
        return "That isn't a held action's id."
    if not held_actions.expire(hid, now=now, owner=owner, db=db):
        return "There's nothing held with that id for you any more."
    _card(card, owner, hid, "Called off", "Nothing was changed.", expires_in=3600)
    return "Called off: nothing was changed."


def classify(ok: bool, text: str) -> str:
    """A scheduled-style run's (ok, text) as drain()'s status. Only a proven non-delivery is "unreachable" (sent again
    later); a taken-but-unanswered one is "failed" (it may have happened: never sent again)."""
    if ok:
        return "done"
    if not_delivered(text):
        return "unreachable"
    if "nobody was there to say yes" in str(text or ""):
        return "needs_ok"
    return "failed"
