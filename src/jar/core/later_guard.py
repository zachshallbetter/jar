#!/usr/bin/env python3
"""Asked for later, never done now (#1203, 10-07; Coordination: a safety rule on every brain, Gemini included).

"Set the thermostat to 66 at 11 PM" and "when I get home, set it to 70" changed the thermostat on the spot on the box
brain (turn_eval lights-later, place-later). So in code, where every tool call runs: when the person's latest message
asks for a later time or a place, and the call is one that acts at once (the house, music, a Mac action),
nothing runs. The model is told to schedule it with schedule_action instead, or to ask if they meant now.

What counts as later (in the person's own latest message, never an earlier one or someone else's text):
  - a clock time the action is for: "at 11 PM", "at 4:30", "at noon" (a time the thing being said is at,
    "dinner is at 6:30", "the game starts at 7 PM", doesn't count)
  - a delay: "in an hour", "in 20 minutes", "in half an hour" (again not one the message describes)
  - a place: "when I get home", "when I arrive at…", "when I leave work", "once I'm home"
  - "later" ("later tonight", "do it later")
"now" / "right now" / "immediately" anywhere in the message turns it off: they asked for it now.
"""
from __future__ import annotations

import re

# Calls that act at once and that schedule_action can run later instead.
# Not send_message or speak (Coordination, 10-07): their arguments carry the person's words, so a time or "later" in the
# message is usually what to say ("Text Robin I'll be home later", "Tell Ava pickup is at 5:30"), not when to act. A
# scheduled text, if it needs a guard, needs its own send-time pattern ("text her at 6 that…"), never this broad one.
LATER_TOOLS = frozenset({"home_control", "play_music", "music_control", "run_shortcut", "run_applescript", "run_shell"})

_DESCRIBES = r"(?:is|are|'s|’s|was|be|starts?|begins?|ends?|opens?|closes?|arrives?|lands?|leaves?|happens?)"
_CLOCK = (r"\bat\s+(?:\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?|\d{1,2}\s*[ap]\.?m\.?|noon|midnight)\b"
          r"|\bat\s+\d{1,2}\s+(?:tonight|tomorrow|this (?:evening|afternoon|morning))\b")
_DELAY = r"\bin\s+(?:an?|one|two|three|a few|half an|\d+)\s+(?:minutes?|mins?|hours?|hrs?)\b"
_PLACE = (r"\b(?:when|once|after|as soon as)\s+(?:i|we)(?:'m|’m| am| are|'re|’re)?\s+"
          r"(?:get(?:s)?\s+(?:home|back|to\b)|home\b|arrive|leave|left|back\b|reach)")
_LATER = r"\blater\b"
_RX = re.compile(f"{_CLOCK}|{_DELAY}|{_PLACE}|{_LATER}", re.I)
_NOW = re.compile(r"\b(?:right now|now|immediately|straight away|right away)\b", re.I)
_DESCRIBED = re.compile(rf"\b{_DESCRIBES}\s+(?:\w+\s+)?$", re.I)


def later_words(message: str | None) -> str | None:
    """The words that ask for later in the person's latest message ("at 11 PM", "when I get home"), or None."""
    text = " ".join(str(message or "").split())
    if not text or _NOW.search(text):
        return None
    for m in _RX.finditer(text):
        if m.group(0).lower().startswith(("at ", "in ")) and _DESCRIBED.search(text[:m.start()]):
            continue   # "dinner is at 6:30", "practice is in an hour": a time the message describes, not when to act
        return m.group(0).strip()
    return None


# #1251 B (Zach, 10-08): on the local brain, a later time or place is never scheduled on titan's own say-so. The action
# is held and the person asked, in one line, whether to schedule it; schedule_action runs only after their yes (the
# next turn), or when their own message already asked to schedule it.
# A request to schedule, not the noun: "schedule it for 11", "can you schedule the thermostat", "get it scheduled" —
# never "check my schedule, then set it to 66 at 11 PM" or "is it on the schedule?" (Codex on #1277).
_SCHEDULE_ASKED_RX = re.compile(
    r"(?<!\bmy )(?<!\bthe )(?<!\bour )(?<!\byour )(?<!\ba )\bschedule\s+(?:it|that|this|them|those|the|a|an|my|our|for|to)\b"
    r"|\bscheduling\s+(?:it|that|this|them|the|a|an|my|our)\b|\b(?:get|have|keep)\s+(?:it|that|this|them)\s+scheduled\b"
    r"|\bset (?:it|that|this) (?:up )?for (?:later|then)\b", re.I)


def asked_to_schedule(message: str | None) -> bool:
    """Whether the person's own latest message already asks for it to be scheduled ("schedule it for 11")."""
    return bool(_SCHEDULE_ASKED_RX.search(" ".join(str(message or "").split())))


# The person's bare yes to titan's own "…at 11 PM tonight? I'll schedule it if you say yes." (#1251 B, 10-08 A/B: on
# "Yes." the box's 8B changed the thermostat at once; the yes carries no later words of its own).
_YES_WORD = r"(?:yes|yep|yeah|yup|sure|ok(?:ay)?|please do|please|do it|go ahead|sounds good|that'?s right|correct)"
_YES_RX = re.compile(rf"^\W*{_YES_WORD}\b(?:[\s,.!]+{_YES_WORD}\b)?[\s\W]*(?:please|thanks|thank you)?[\s\W]*$", re.I)


def confirmed_later(previous_reply: str | None, message: str | None) -> str | None:
    """The later words of titan's own previous question when the person's latest message is a bare yes to it, else None.
    The previous reply must be titan's (the caller passes the last assistant message) and must ask (a "?")."""
    text = " ".join(str(message or "").split())
    if not text or len(text.split()) > 6 or not _YES_RX.match(text):
        return None
    prev = " ".join(str(previous_reply or "").split())
    if "?" not in prev:
        return None
    m = _RX.search(prev)   # the time or place titan asked about ("at 11 PM", "when you get home" → "when I get home")
    if not m:
        m = re.search(r"\b(?:when|once|after|as soon as)\s+you(?:'re|’re| are)?\s+(?:get(?:s)?\s+(?:home|back)|home\b|arrive|"
                      r"leave|back\b)", prev, re.I)
    if not m:
        return None
    # titan says "when you get home"; the guard reads the person's words ("when I get home")
    return re.sub(r"\byou(?:'re|’re| are)\b", "I'm", re.sub(r"\byou\b", "I", m.group(0).strip()))


def refusal(tool: str, words: str, ask_first: bool = False) -> str:
    """What the model gets instead of running the call. ask_first (the local brain, #1251 B): ask, don't schedule."""
    place = bool(re.match(_PLACE, words, re.I))
    if ask_first:
        when = "when they get there" if place else "at the time they said"
        return (f"Not run: they asked for this later (“{words}”), not now. Nothing changed, and nothing is scheduled. "
                f"Ask them in one line whether to schedule it, saying what and {when} (for example: “Set it to 66 at "
                f"11 PM tonight? I'll schedule it if you say yes.”). Don't call schedule_action now: only after they "
                f"say yes.")
    how = ("schedule_action with place and on (\"home\", on arrive)" if place
           else "schedule_action with at (the time they said)")
    return (f"Not run: they asked for this later (“{words}”), not now. Nothing changed. Call {how}, with tool_name "
            f"\"{tool}\" and these same arguments as tool_args. If they meant now, ask them in one line instead.")


def schedule_refusal(words: str) -> str:
    """schedule_action on the local brain in a turn whose message asks for later, when the person hasn't asked to
    schedule it (#1251 B): titan asks first, whether or not it tried the action now."""
    return (f"Not run: ask them first. They haven't said yes to scheduling it (“{words}”). Nothing is scheduled. Ask in "
            f"one line whether to schedule it, saying what and when; schedule it only after they say yes.")
