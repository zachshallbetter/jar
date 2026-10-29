#!/usr/bin/env python3
"""Asked for everyone, never set for one (#1251; turn_eval remind-everyone).

"Remind everyone dinner is at 6 tonight" made a reminder on the box brain, and a reminder reaches only the person who
asked. So in code, where every tool call runs: when the person's latest message asks to remind everyone (the household,
the family, all of us), a personal reminder or todo doesn't run, and the model is told to leave the household a card
with notify_user instead. That's what they asked for: the card goes to the people they named, nothing wider ("the
kids" are the asker's own kids by name, from Settings › Household; never the household roster).

Only when notify_user is declared for this asker (the runtime checks); otherwise the call runs as before. Never on a
background turn (its words aren't a person's own).
"""
from __future__ import annotations

import re

# Calls that reach only the person asking.
PERSONAL_TOOLS = frozenset({"create_reminder", "add_todo"})

_RX = re.compile(r"\bremind (?:everyone|everybody|all of us|us all|the (?:whole )?(?:house|household|family|kids))\b", re.I)


def everyone_words(message: str | None) -> str | None:
    """The words that ask to remind everyone ("remind everyone"), or None."""
    m = _RX.search(" ".join(str(message or "").split()))
    return m.group(0) if m else None


def means_kids(words: str) -> bool:
    return bool(re.search(r"\bkids\b", words or "", re.I))


def refusal(tool: str, words: str, kids: list[str] | None = None) -> str:
    """What the model gets instead of running the call. "The kids" are the asker's OWN kids by name (`kids`: the
    people whose parents include the asker, from Settings › Household), never the household roster; with none set,
    the model asks whom they mean."""
    if means_kids(words):
        if not kids:
            return (f"Not run: {tool} reaches only the person asking, and they asked “{words}”, but no kids are set as "
                    f"theirs in Settings › Household. Nothing changed. Ask them in one line whom to remind.")
        who = ", ".join(kids)
    else:
        who = "household"
    return (f"Not run: {tool} reaches only the person asking, and they asked “{words}”. Nothing changed. Call "
            f"notify_user with to set to {who}, a short title, and the reminder (with its time) as the detail; set "
            f"expires_in_min so it retires after that time.")
