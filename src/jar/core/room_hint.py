#!/usr/bin/env python3
"""A person's own room hint (titan-node#433; Zach's yes, 10-05; the design is on that issue). A person's own iPhone or
Watch tells titan which room they're in, so their OWN requests can say "here" ("turn the lights off in here", "play it
here"). Adults and the admin only for now; kids are a separate question for Zach.

  kept      in this process's memory only, per person: {room, confidence, ts, device}. The newest wins between the
            phone and the Watch. It lapses after ROOM_HINT_TTL_S (600) and a restart forgets it. Never written to
            disk, never in a log line or an error message, never read back over HTTP (there's no GET).
  read      only by the asker's own turn (agent_runtime_lib._my_room). A shared screen, the TV, the home feed,
            room_state.py and handoff.py never import this module (test_room_hint checks).
  refused   a hint below ROOM_HINT_MIN (0.6) is accepted but not kept, so a weak one never replaces a good one.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterable

TTL_S = float(os.environ.get("ROOM_HINT_TTL_S", "600"))
MIN_CONFIDENCE = float(os.environ.get("ROOM_HINT_MIN", "0.6"))
MAX_AGE_S = 120.0      # a hint sent this long after it was sensed is too old to act on
MAX_AHEAD_S = 30.0     # a device clock this far ahead is wrong
ROLES = frozenset({"admin", "adult"})
PLATFORMS = frozenset({"ios", "watchos"})

_lock = threading.Lock()
_hints: dict[str, dict[str, Any]] = {}


class HintError(ValueError):
    """A hint that can't be taken; `status` is the HTTP status. The message never holds a room the person sent."""

    def __init__(self, message: str, status: int = 400, rooms: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.rooms = rooms or []


def fold(name: Any) -> str:
    return " ".join(str(name or "").split()).lower()


def refusal(role: str | None, device: dict[str, Any] | None, via: str | None) -> str | None:
    """Why this caller may not send a hint (403), or None. Only a person's own paired iPhone or Watch, an adult's or
    the admin's; never a shared household device, the master token, a Mac, a TV, or a kid's device."""
    if not device:
        return "only a person's own iPhone or Watch sends a room hint"
    if role not in ROLES or via == "watch":
        return "room hints are for adults' own devices"
    if str(device.get("platform") or "").lower() not in PLATFORMS:
        return "only a person's own iPhone or Watch sends a room hint"
    return None


def put(person: str, device_id: str, room: Any, confidence: Any, ts: Any, rooms: Iterable[str],
        now: float | None = None) -> bool:
    """Take one hint. → True when kept, False when it was too weak to keep. Raises HintError."""
    now = time.time() if now is None else now
    names = {fold(r): str(r) for r in rooms if fold(r)}
    if not names:
        raise HintError("the household's rooms aren't known right now", 503)
    want = fold(room)
    if want not in names:
        raise HintError("not one of the household's rooms", 400, sorted(names.values()))
    try:
        conf, at = float(confidence), float(ts)
    except (TypeError, ValueError):
        raise HintError("confidence and ts are numbers") from None
    if not 0.0 <= conf <= 1.0:
        raise HintError("confidence is between 0 and 1")
    if at < now - MAX_AGE_S or at > now + MAX_AHEAD_S:
        raise HintError("ts is too old or ahead of titan's clock")
    if conf < MIN_CONFIDENCE:
        return False
    with _lock:
        cur = _hints.get(person)
        if cur and cur["ts"] > at and now - cur["ts"] < TTL_S:
            return True       # an older hint than the one held (the other device's): the newer stands
        _hints[person] = {"room": names[want], "confidence": conf, "ts": at, "device": device_id}
    return True


def clear(person: str) -> None:
    with _lock:
        _hints.pop(person, None)


def get(person: str | None, now: float | None = None) -> dict[str, Any] | None:
    """The person's own fresh hint, {room, confidence, age_s}, or None (none, or lapsed: a lapsed one is dropped)."""
    if not person:
        return None
    now = time.time() if now is None else now
    with _lock:
        h = _hints.get(person)
        if not h:
            return None
        if now - h["ts"] >= TTL_S:
            _hints.pop(person, None)
            return None
        return {"room": h["room"], "confidence": h["confidence"], "age_s": max(0.0, now - h["ts"])}


def ago(age_s: float) -> str:
    m = int(age_s // 60)
    return "just now" if m < 1 else ("a minute ago" if m == 1 else f"{m} minutes ago")
