#!/usr/bin/env python3
"""Private peek (jellybean#426): a personal question asked on a shared screen is answered on the asker's own phone.

A shared device (the household role: the Apple TV, a kitchen screen) reads only the household's things, whoever is
in the room (titan-node#418), and titan can't yet tell who's speaking (#410). So "what's my day tomorrow?" there
isn't answered there. It becomes a card on that person's own devices, and the card's button asks the question on
their phone, signed in as them. The shared screen says "Sent to Alex's phone."

Who's asking: a name in the question ("it's Alex", "for Ava", "Alex's day"), else titan asks, and the next thing
said on that device within PENDING_S is the name. Naming someone else only sends them their own question: nothing
private ever reaches the shared screen, which is what makes it safe before voice identity. All in code, no model.

The reply text is the contract every client already reads. A client that wants to draw it (the TV's "Sent to Robin's
phone" panel, name buttons for "who's asking?") also gets `peek` on the turn (jellybean#426): {"state": "ask", "names":
[first names of people with a paired phone]}, {"state": "sent", "name"} or {"state": "no_phone", "name"}.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

PENDING_S = 120          # how long a shared device waits for "who's asking?"
CARD_HOURS = 2           # how long the card waits on the phone
# The card's button text starts with agent_memory.CARD_PROMPT_MARK: memory never learns from it (anyone in the room
# could have said it).
CARD_PROMPT_MARK = "(from a suggestion card)"

_PERSONAL_RX = re.compile(r"\b(?:my|mine|me|myself|i|i'm|im|i've|i'd|i'll)\b", re.I)
# First person that's about the household, not the asker ("remind us", "our"), or the device itself, isn't personal.
_NOT_PERSONAL_RX = re.compile(r"^\s*(?:(?:can|could|would) you |please )?(?:play|pause|resume|skip|stop|turn|set the|"
                              r"dim|show me (?:the )?(?:household|family|house)|what'?s for dinner)\b", re.I)
_lock = threading.Lock()
_pending: dict[str, tuple[str, float]] = {}   # device id → (question, when asked)


def personal(question: str) -> bool:
    """A question about the asker themselves: their day, their bills, their messages."""
    q = str(question or "")
    return bool(_PERSONAL_RX.search(q)) and not _NOT_PERSONAL_RX.match(q)


def _first(p: dict[str, Any]) -> str:
    return str(p.get("name") or "").split(" ")[0]


def named(text: str, people: list[dict[str, Any]], whole: bool = False) -> dict[str, Any] | None:
    """The household member a sentence says it's for ("it's Alex", "for Ava", "Alex here", "Alex's day"), or, with
    whole, a reply that's just a name ("Alex", "it's me, Robin")."""
    t = " ".join(str(text or "").split()).strip(" .!?,")
    hits = []
    for p in people:
        f = _first(p)
        if len(f) < 2:
            continue
        e = re.escape(f)
        if whole and re.fullmatch(rf"(?:(?:it'?s|this is|i'?m|i am)\s+)?(?:me,?\s+)?{e}(?:\s+here)?", t, re.I):
            hits.append(p)
        elif not whole and re.search(rf"\b(?:it'?s|this is|i'?m|i am|for|from)\s+{e}\b|\b{e}\s+here\b|\b{e}(?:'s|’s)\b", t, re.I):
            hits.append(p)
    return hits[0] if len(hits) == 1 else None


def handle(question: str, device_id: str | None, device_name: str, people: list[dict[str, Any]],
           has_device: Callable[[str], bool], send: Callable[..., Any], now: float | None = None,
           report: Callable[[dict[str, Any]], None] | None = None) -> str | None:
    """On a shared device: the reply when this turn is a private peek (sent, or "who's asking?"), else None and the
    turn goes on as a household turn. has_device(person_id): they have a paired device of their own.
    send(title, detail, to, action_prompt, key, expires_in): proactive.add_card. report(peek): the same outcome as
    data, for a client that draws it (see the module's docstring); the text is unchanged with or without it."""
    report = report or (lambda _peek: None)
    now = time.time() if now is None else now
    dev = str(device_id or "")
    with _lock:
        for k in [k for k, (_, at) in _pending.items() if now - at > PENDING_S]:
            del _pending[k]
        waiting = _pending.get(dev)
    if waiting:
        who = named(question, people, whole=True)
        if who is not None:
            with _lock:
                _pending.pop(dev, None)
            return _send(waiting[0], who, device_name, has_device, send, report)
        with _lock:
            _pending.pop(dev, None)       # not a name: the question was dropped, this one goes on
    if not personal(question):
        return None
    who = named(question, people)
    if who is None:
        with _lock:
            _pending[dev] = (question, now)
        report({"state": "ask", "names": [_first(p) for p in people if len(_first(p)) >= 2 and has_device(str(p["id"]))]})
        return ("That sounds like it's about you, and this screen is shared, so I'll answer it on your phone. "
                "Who's asking? Say your name.")
    return _send(question, who, device_name, has_device, send, report)


def _send(question: str, who: dict[str, Any], device_name: str, has_device: Callable[[str], bool],
          send: Callable[..., Any], report: Callable[[dict[str, Any]], None]) -> str:
    first = _first(who)
    if not has_device(str(who["id"])):
        report({"state": "no_phone", "name": first})
        return (f"{first} doesn't have a phone paired with titan, so I can't answer that privately. "
                f"Ask me on your own device.")
    where = device_name or "a shared screen"
    send(f"You asked on the {where}", f"“{question.strip()}”\nTap to see the answer here.", to=[who["id"]],
         action_prompt=f"{CARD_PROMPT_MARK} {question.strip()}", key=f"peek-{who['id']}",
         expires_in=CARD_HOURS * 3600)
    report({"state": "sent", "name": first})
    return f"Sent to {first}'s phone. Open the card there to see the answer."
