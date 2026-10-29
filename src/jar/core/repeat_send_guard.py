#!/usr/bin/env python3
"""A repeating text to someone, never a one-off reminder (#1251; turn_eval routine-cant-text).

"Every weekday at 4:30, text Ava to get ready for ballet" made one reminder for the asker on the box brain, 6 of 6
(the 10-08 diagnosis): a reminder neither repeats nor sends a text, so Ava never hears and the asker is reminded once.
So in code, where every tool call runs: when the person's latest message asks to text or message someone on a
repeat, a create_reminder call doesn't run, and the model is told to use schedule_action with the repeat (it sends the
text each time) or save_routine (it shows the person a card then). Nothing is written, so the model picks again in the
same turn.

Not held: a reminder they asked for themselves ("remind me every weekday to text Ava", "remind me every weekday"), and
a text with no repeat ("text Ava at 4:30 today": that's schedule_action's own one-off, and a reminder there is left
to the other checks). Never on a background turn (its words aren't a person's own).
"""
from __future__ import annotations

import re

HELD_TOOLS = frozenset({"create_reminder"})

_SEND_RX = re.compile(r"\b(?:text|message|send (?:a |an )?(?:text|message|sms|imessage)(?: to)?)\s+(?!me\b)[A-Za-z]", re.I)
_REPEAT_RX = re.compile(r"\b(?:every (?:day|weekday|weeknight|morning|night|evening|afternoon|week|weekend|other day|"
                        r"(?:mon|tues|wednes|thurs|fri|satur|sun)day)|each (?:day|weekday|week|morning|night|evening)|"
                        r"daily|weekly|on weekdays|weekdays at|weeknights|(?:mon|tues|wednes|thurs|fri|satur|sun)days)\b", re.I)
_REMIND_SELF_RX = re.compile(r"\bremind me\b", re.I)


def repeat_send_words(message: str | None) -> str | None:
    """The repeat words ("every weekday") when the latest message asks to text or message someone on a repeat, else
    None. A reminder the person asked for themselves never counts."""
    text = " ".join(str(message or "").split())
    if not text or _REMIND_SELF_RX.search(text) or not _SEND_RX.search(text):
        return None
    m = _REPEAT_RX.search(text)
    return m.group(0) if m else None


def refusal(tool: str, words: str) -> str:
    """What the model gets instead of running the call."""
    return (f"Not run: they asked for a text to someone on a repeat (“{words}”), and {tool} neither repeats nor sends "
            f"a text. Nothing changed. Call schedule_action with tool_name \"send_message\", its tool_args, at (the time "
            f"of day) and repeat (“{words}”): it sends the text each time. Or, if a card for them to send it themselves "
            f"fits better, call save_routine and tell them it shows a card at that time.")
