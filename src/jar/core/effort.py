#!/usr/bin/env python3
"""How hard a turn thinks (#876; Zach 10-04: an explicit "Think harder", and titan raising it itself for hard
questions). High effort = Gemini's thinkingLevel high for the turn plus one self-check pass over the draft reply
(agent_runtime_lib._self_checked). Decided here, in code:

  asked      the request's `effort`: "high" (the composer's Think harder) or "normal" (never raised)
  auto       otherwise: raised for questions that are hard to get right in one pass: money (amounts, totals,
             splits, budgets), schedules (fitting, conflicts, rescheduling, planning a stretch of days), several
             steps or several questions at once, and long questions. Everyday lookups stay normal.
  hint       the model may only lower an auto raise ("normal"), never raise one

`decide` returns (level, why); the turn log keeps both.
"""
from __future__ import annotations

import os
import re

LEVELS = ("normal", "high")


def auto_on() -> bool:
    """AGENT_EFFORT_AUTO=1: titan may raise effort itself. Off: only an explicit "high" does (10-04 A/B: +1.5 s a turn,
    no accuracy change on truth_eval; auto would raise ~9% of real questions)."""
    return os.environ.get("AGENT_EFFORT_AUTO", "") == "1"
# "total" and "sum" alone are arithmetic, not money ("the total number of letters": 10-10, the canary's question got
# effort=high and a self-check pass on every run since 10-04); they count only near a money word.
_MONEY_WORD = r"(?:\$|dollars?|usd|cost|costs|price|prices|bill|bills|spend|spent|paid|pay|owe|owed|budget|charges?|fees?)"
_MONEY = re.compile(r"(?i)(\$\s?\d|\b\d+(?:\.\d\d)?\s?(?:dollars|usd)\b|\b(?:how much|split|budget|afford|cost(?:s)?|"
                    r"owe|refund|interest|per month|monthly|annual|tax(?:es)?|compare (?:the )?(?:price|cost|plan)s?)\b|"
                    rf"\b(?:total|sum)\b[^.?!]{{0,40}}{_MONEY_WORD}|{_MONEY_WORD}[^.?!]{{0,40}}\b(?:total|sum)\b)")
_SCHEDULE = re.compile(r"(?i)\b(fit|conflicts?|overlap\w*|clash\w*|reschedul\w*|move (?:it|the|my)|free (?:time|slot)|"
                       r"when (?:can|could|should) (?:we|i)|plan (?:the|my|our|out)|this week and|next week and|"
                       r"both (?:days|weekends)|every (?:other )?(?:mon|tues|wednes|thurs|fri|satur|sun)day|itinerary|"
                       r"carpool|drop[- ]?offs? and pick[- ]?ups?)\b")
_STEPS = re.compile(r"(?i)\b(step[- ]by[- ]step|first .{0,60}\bthen\b|and then|figure out|work out|calculate|compare|"
                    r"which (?:is|would be) (?:better|best|cheaper)|pros and cons|trade-?offs?|why (?:did|does|is|would))\b")


def _why_auto(question: str) -> str | None:
    q = str(question or "")
    if _MONEY.search(q):
        return "auto: money"
    if _SCHEDULE.search(q):
        return "auto: schedule"
    if q.count("?") >= 2:
        return "auto: several questions"
    if _STEPS.search(q):
        return "auto: several steps"
    if len(q.split()) > 60:
        return "auto: long question"
    return None


def decide(question: str, asked: str | None = None, hint: str | None = None) -> tuple[str, str]:
    """(level, why) for one turn."""
    asked = str(asked or "").strip().lower()
    if asked in LEVELS:
        return asked, f"asked: {asked}"
    why = _why_auto(question)
    if not why:
        return "normal", "auto: everyday"
    if not auto_on():
        return "normal", f"{why} (auto off)"
    if str(hint or "").strip().lower() == "normal":
        return "normal", f"{why}, lowered by the model's hint"
    return "high", why
