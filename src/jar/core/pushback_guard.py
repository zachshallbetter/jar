#!/usr/bin/env python3
"""Pushback triggers a re-check, not agreement (#1610, a review of Zach's conversations 10-05..10-13).

When the person disputed an answer, titan flipped: on location tracking (its first answer was right), on a Mac app
being a Mac app, on "cloud" becoming "a local socket hang", usually opening with "You're completely right". So:

  - disputes(): the person's latest message disputes the previous answer ("no, that's wrong", "are you sure", "that's
    not right", "actually it's X"). The caller then puts RECHECK_NOTE, a fixed system line, before their message:
    re-check with the tools or sources first, keep the answer if the check supports it, change it if the check shows
    it was wrong, and don't open with agreement. The person's words are only matched here, never copied into the note
    (#473: untrusted text never reaches the model as titan's own words, and nothing here is added to the user message).
  - without_caving_opener(): a reply that opens with "You're completely/absolutely right" on a turn where no tool ran
    has agreed without checking anything. The opener goes and the reply starts at what it actually says; the caller
    counts it. After a check (a tool ran) the reply is left as it is: agreeing with what a source showed is fine.

Pure functions: no titan state, so they're tested on their own (test_pushback_recheck.py).
"""
from __future__ import annotations

import re

# The person disputes what was just said. Conservative: each needs a disputing phrase, not just a "no" (a "No, thanks"
# answers an offer) or an "actually" (a "Actually, add milk too" is a new request).
_DISPUTE_RX = re.compile(
    r"\bare you (?:sure|certain|positive)\b"
    r"|\b(?:that|this|it)(?:'s|’s| is)\s+(?:not (?:right|true|correct|accurate|what (?:i|it) said)|wrong|incorrect|false|"
    r"inaccurate|made up)\b"
    r"|\b(?:that|this|it)\s+(?:isn't|isn’t|is not|wasn't|wasn’t|was not)\s+(?:right|true|correct|accurate)\b"
    r"|\byou(?:'re|’re| are)\s+(?:wrong|mistaken|incorrect|making (?:that|this|it) up)\b"
    r"|\bthat(?:'s|’s| is) not how (?:it|that|this) works\b"
    r"|\b(?:double|re)-?check (?:that|this|it)\b"
    r"|\bcheck (?:that|it) again\b"
    r"|^\s*(?:no|nope|nah)\b[\s,.!-]*(?:it|that|she|he|they)(?:'s|’s| is| are|'re|’re)\s+(?:not|actually|wrong)\b"
    r"|^\s*(?:no|nope)[\s,.!-]+actually\b"
    r"|^\s*actually[\s,]+(?:it|that|she|he|they|(?:the|my|our|his|her|their|\w+(?:'s|’s))(?:\s+\w+){1,2})"
    r"(?:'s|’s| is| are|'re|’re| was| were)\s+(?!fine\b|ok\b|okay\b|good\b)",
    re.I)

RECHECK_NOTE = (
    "[RECHECK] The person is disputing your previous answer. Before you change it, check again: read the tools or "
    "sources that answer it now, not your memory and not their message. If the check supports your previous answer, "
    "keep it and say what you checked. If the check shows you were wrong, say what is right and where that came from. "
    "If nothing you can check settles it, say so plainly and don't pick a side. Don't open with agreement or an "
    "apology (no \"You're right\"): lead with what the check found.")

# "You're completely right" and its kin at the very start of a reply. Plain "You're right" is left alone (a small
# agreement after an actual answer is ordinary); these are the cave-in openers the review found.
_OPENER_RX = re.compile(
    r"^\s*(?:(?:oh|ah|okay|ok)[,!]?\s+)?you(?:'re|’re| are)\s+(?:completely|absolutely|totally|entirely|100%|so)\s+right\b",
    re.I)
# What may follow the opener and goes with it: ", and I apologize for the confusion", "! Sorry about that.", " that"
_AFTER_OPENER_RX = re.compile(
    r"^(?:\s*(?:[,;:—–-]+|\s)\s*(?:and\s+)?(?:I\s+apologi[sz]e|sorry|my apologies|thanks? (?:you )?for)[^.!?\n]*[.!?]*"
    r"|\s*[.!]+(?:\s*(?:I\s+apologi[sz]e|sorry|my apologies|thanks? (?:you )?for)[^.!?\n]*[.!?]+)*"
    r"|\s*(?:,|—|–|-|:|;)?\s*(?:and\s+)?that\b"
    r"|\s*[,;:—–-]+)\s*", re.I)
EMPTY_AFTER_OPENER = "I haven't checked that."


def disputes(words: str) -> bool:
    """The person's latest message disputes the previous answer (their own words only)."""
    return bool(words and _DISPUTE_RX.search(words.strip()))


def caving_opener(text: str) -> bool:
    return bool(text and _OPENER_RX.match(text))


def without_caving_opener(text: str) -> tuple[str, bool]:
    """(the reply without a "You're completely right" opener, whether one was removed). The rest is kept as written,
    its first letter capitalised; nothing left: EMPTY_AFTER_OPENER."""
    m = _OPENER_RX.match(text or "")
    if not m:
        return text, False
    rest = text[m.end():]
    after = _AFTER_OPENER_RX.match(rest)
    if after:
        rest = rest[after.end():]
    rest = rest.lstrip()
    if not re.search(r"\w", rest):
        return EMPTY_AFTER_OPENER, True
    return rest[:1].upper() + rest[1:], True
