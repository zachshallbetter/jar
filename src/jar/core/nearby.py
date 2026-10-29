#!/usr/bin/env python3
"""Local suggestions start from where the asker is (#1613).

10-13: asked for indoor activities while in another town, titan suggested places near home twice. Nothing in the turn
said where the person was, so the model fell back on home. For a question that wants something nearby ("things to do",
"indoor activities", "where to eat", "near here") and names no other place, the per-turn context now carries one line
that says which place to anchor on, and asks the model to say so in one clause:

  1. the asker's own fresh place: their iPhone's latest arrival (places.where_now, the same events the graph's
     located_at edges come from), at a place other than home, within FRESH_HOURS and not left since;
  2. on an iPhone turn that offers where_am_i, with no fresh place: call where_am_i first and use that address;
  3. otherwise home (the household's home town when it's set), saying that's what was used.

An arrival at home with no departure since means they're home, however long ago. Location stays per person: only the
asker's own events are read, never another person's, and a shared household device has no person, so it gets home.
Nothing here is stored. Pure: the caller passes the place reader, the clock and what the turn can do.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

FRESH_HOURS = 6     # situation.HERE_HOURS: "where you are" is an arrival within this many hours, not since left

NEARBY_Q_RX = re.compile(
    r"\b(?:things?|stuff|something|anything|places?|somewhere) (?:fun )?(?:to|we can|i can|you can) "
    r"(?:do|see|go|visit|eat|get food)\b|"
    r"\b(?:indoor|outdoor|rainy[- ]day|kid[- ]friendly|family|fun|local) (?:activit(?:y|ies)|things|places|stuff)\b|"
    r"\bactivit(?:y|ies) (?:for|near|around|nearby|today|tonight|this)\b|"
    r"\bwhere (?:can|should|could) (?:we|i) (?:eat|go|get (?:food|lunch|dinner|breakfast|coffee))\b|"
    r"\bwhere to (?:eat|go)\b|\b(?:good |best )?(?:restaurants?|caf[eé]s?|coffee shops?|playgrounds?|parks?|museums?) "
    r"(?:near|around|nearby|close)\b|"
    r"\bnear (?:here|me|us|by)\b|\bnearby\b|\baround here\b|\bclose by\b|\bin (?:the|this) area\b|"
    r"\bwhat(?:'s| is) (?:there )?to do\b",
    re.I)
# "things to do in Brightwater", "restaurants near Larkspur": they named the place, so theirs wins.
_NAMED_ELSEWHERE_RX = re.compile(r"\b(?:in|near|around|at|by|to) (?:the )?(?!(?:I|We|Me|Us)\b)[A-Z][\w'’.-]+")


def asks_nearby(words: str) -> bool:
    """A request for something near where the asker is, naming no other place."""
    w = str(words or "")
    return bool(NEARBY_Q_RX.search(w)) and not _NAMED_ELSEWHERE_RX.search(w)


def _event_ts(ev: dict) -> float:
    import places
    return places.event_ts(ev)


def current_place(person: str | None, now: datetime,
                  where_now: Callable[[str], dict | None]) -> tuple[str, dict] | None:
    """("away", event) for the asker's fresh arrival somewhere other than home, ("home", event) when their latest
    event is an arrival at home, None when neither (no events, a stale arrival, or a departure since)."""
    if not person:
        return None
    ev = where_now(person)
    if not ev or ev.get("event") != "arrived":
        return None
    place = str(ev.get("place") or "").strip()
    if not place:
        return None
    if place.lower() == "home":
        return "home", ev
    age = now.timestamp() - _event_ts(ev)
    if 0 <= age <= FRESH_HOURS * 3600:
        return "away", ev
    return None


def line(words: str, person: str | None, role: str | None, now: datetime, *,
         where_now: Callable[[str], dict | None], can_locate: bool, home_name: str | None = None,
         when_text: Callable[[dict, float], str] | None = None) -> str | None:
    """The per-turn line for a nearby question (None for any other question). `can_locate`: this turn offers
    where_am_i (the asker's own iPhone). `home_name`: the household's home town, when it's set."""
    if not asks_nearby(words):
        return None
    if role == "household":
        person, can_locate = None, False      # a shared device: nobody's location, ever
    home = f"home ({home_name})" if home_name else "home"
    got = current_place(person, now, where_now)
    if got and got[0] == "away":
        ev = got[1]
        place = str(ev["place"])[:60]
        when = f" {when_text(ev, now.timestamp())}" if when_text else ""
        return (f"Where you are, for this nearby question: their own iPhone says they arrived at {place}{when}. "
                f"Suggest places near {place}, not near {home}, and say so in one clause (\"Near {place}, where "
                "you are now: …\"). If they name another place, use theirs.")
    if got and got[0] == "home":
        return (f"Where you are, for this nearby question: at {home} (their own iPhone's latest arrival). Suggest "
                f"places near {home} and say so in one clause. If they name another place, use theirs.")
    if can_locate:
        return ("Where you are, for this nearby question: not known yet. Call where_am_i first and suggest places "
                "near the address it gives, saying so in one clause (\"Near you in <town>: …\"). Only if it gives no "
                f"location, use {home} and say that's what you used. If they name another place, use theirs.")
    return (f"Where you are, for this nearby question: not known (no recent arrival from their iPhone). Suggest places "
            f"near {home}, say in one clause that you used home, and offer to look somewhere else if they're away. "
            "If they name another place, use theirs.")


def for_turn(words: str, person: str | None, role: str | None, now: datetime, can_locate: bool,
             home: dict[str, Any] | None = None) -> str | None:
    """line() with the live readers: the asker's own place events and the household's saved home town."""
    if not asks_nearby(words):
        return None
    import places
    if home is None:
        try:
            import weather
            home = weather.home()
        except Exception:  # noqa: BLE001 - no saved home: "home" with no name
            home = None
    return line(words, person, role, now, where_now=places.where_now, can_locate=can_locate,
                home_name=str((home or {}).get("name") or "") or None, when_text=places.when_text)
