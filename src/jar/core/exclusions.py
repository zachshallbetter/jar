#!/usr/bin/env python3
"""What the person excluded in their own message, so no write touches it (titan-node#952).

10-04 21:52 UTC: "I think all of those have been fixed except for providence". Titan completed the one reminder
created that day, the Providence payment, which was the item he had excluded. Facts in code: "all except X",
"everything but X", "other than X", "excluding X", "apart from X" name X; a change tool (complete, update, delete...)
whose target names every content word of X is refused with "Not run". Reading is never refused.
"""
from __future__ import annotations

import re

_EXCEPT_RX = re.compile(
    r"(?i)\b(?:except(?:\s+for)?|apart\s+from|aside\s+from|other\s+than|excluding|exclude|besides|but\s+not|"
    r"(?:all|everything|everyone|each|them|those|these|ones|of\s+them)(?:\s+[a-z']+){0,2}?\s+but)\s+([^.;!?\n]+)")
_CLAUSE_RX = re.compile(r"(?i)^(?:i|we|you|he|she|they|it|that|this|there)(?:\b|'[a-z]+\b)")   # "except I forgot"
_AND_RX = re.compile(r"(?i)\s*(?:\band\b|\bor\b|&|/)\s*")
_CUT_RX = re.compile(r"(?i)\s*(?:,|\band\s+(?:i|we|you|then|can|please|could|also)\b|\bbecause\b|\bsince\b|\bwhich\b|"
                     r"\bthat\s+(?:one\s+)?(?:is|was|isn't|wasn't|still)\b|\bis\b|\bwas\b|\bisn't\b|\bstill\b)")
_FILLER = frozenset("the a an my our your that this those these one ones thing things item items stuff for of to "
                    "bill's it them".split())


def excluded(message: str | None) -> list[str]:
    """The phrases the person set aside, in their words ("providence", "the xfinity bill")."""
    out = []
    for m in _EXCEPT_RX.finditer(message or ""):
        head = _CUT_RX.split(m.group(1), maxsplit=1)[0].strip(" '\"“”‘’")
        if _CLAUSE_RX.match(head):
            continue
        for phrase in _AND_RX.split(head):   # "the dentist one and the car": two things
            phrase = phrase.strip(" '\"“”‘’").lower()
            if words(phrase) and phrase not in out:
                out.append(phrase)
    return out[:4]


def words(phrase: str) -> list[str]:
    return [w for w in (re.sub(r"'s$", "", x) for x in re.findall(r"[a-z0-9][a-z0-9'&-]*", phrase.lower()))
            if w not in _FILLER and len(w) > 1][:4]


def hit(phrases: list[str], target: str) -> str | None:
    """The excluded phrase this write's target names (every one of its content words), or None."""
    low = " " + re.sub(r"'s\b", "", re.sub(r"[^a-z0-9'&-]+", " ", (target or "").lower())) + " "
    for p in phrases:
        ws = words(p)
        if ws and all(f" {w} " in low or f" {w}s " in low for w in ws):
            return p
    return None
