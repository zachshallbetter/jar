#!/usr/bin/env python3
"""A ticket is a ticket, never a task titan runs by itself (#1614, Zach's conversations 10-12).

"Ticket it as a task to explore and test" (a Nature paper) and "add the items" (Checkmk, from a YouTube video) both
became plan_task durable tasks: titan then worked on them unseen, one said it was "setting up Checkmk on Titan", and
neither could be found afterwards. So, where every tool call runs:

  - "ticket it", "file a ticket", "make an issue", "add the items" ask for something to be written down: a ticket on
    the admin's Claude list (it becomes a GitHub issue, todo_issues.py) or, for anyone else, a to-do on their own list.
    Never a durable task.
  - a durable task (plan_task) only when the person's own message asks titan to plan or work on something over time.
  - a durable task never installs software ("set up Checkmk on titan", "docker run …", "apt install …"), whoever asks:
    that is a ticket for a person.

The words judged are the person's own in this turn's message (the caller passes _TURN_IO.asker_words), never a tool's
result or the model's arguments. A call with no such words (a direct call, a background turn) is judged only on
installing software.
"""
from __future__ import annotations

import re

# Asking for a ticket. "issue" only as something to make or file, never "the issue with the TV". The patterns are
# lower case: tool narrowing (agent_runtime_lib.TOOL_GROUPS) matches ASK_PATTERN against lowered words, without re.I.
_TICKET = (
    r"\bticket\s+(?:it|that|this|them|those)\b"
    r"|\b(?:file|open|make|create|add|raise|log|write|cut|put in)\s+(?:a|an|the|that|this|it as an?|it as the)?\s*"
    r"(?:new\s+|github\s+)?(?:ticket|issue|bug)s?\b"
    r"|\b(?:as|into)\s+(?:a|an)\s+(?:new\s+|github\s+)?(?:ticket|issue)\b"
    r"|\bgithub\s+issue\b")
_TICKET_RX = re.compile(_TICKET, re.I)
# Asking for things to be written down that came from something they saw ("add the items" from a video): a list, not
# work titan does.
_ITEMS = r"\badd\s+(?:the|these|those|all the|all of the|all those)\s+(?:items|things|steps|points|ideas)\b"
_ITEMS_RX = re.compile(_ITEMS, re.I)
# Every phrase the guard treats as a ticket or items: narrowing offers add_todo for each (one matcher, #1614 review).
ASK_PATTERN = f"{_TICKET}|{_ITEMS}"
# An explicit request that titan plan or work on something over time. Strong words count even beside ticket words.
_DURABLE_STRONG_RX = re.compile(
    r"\b(?:durable|multi-?step|background|long-running|autonomous|ongoing)\s+(?:task|project|job|plan)\b"
    r"|\bover\s+(?:time|the\s+(?:next|coming)\s+(?:few\s+)?(?:days?|weeks?))\b"
    r"|\bon\s+(?:your|its)\s+own\b|\bby\s+(?:your|it)self\b"
    r"|\b(?:you|titan|jb|jellybean)\b[^.?!\n]{0,40}\b(?:work\s+(?:on|through)|keep\s+(?:working|going)|chip\s+away)\b"
    r"|\bdeep\s+work\b|\bstart\s+(?:a|the)\s+task\b"
    # an imperative at a sentence's start: "Keep working on the move until it is done", "Work on this until it's complete"
    r"|(?:^|[.!?;:]\s*)(?:please\s+|now\s+|ok(?:ay)?,?\s+)?(?:keep\s+(?:working|going|at\s+it|chipping\s+away)|"
    r"work\s+(?:on|through|at)|carry\s+on\s+(?:with|working)|chip\s+away\s+at|stick\s+with)\b", re.I | re.M)
# Weaker: "plan a dinner party", "sort out the car registration". Only without ticket words.
_DURABLE_WEAK_RX = re.compile(r"\bplan(?:ning)?\b(?!\s+(?:to|on)\b)|\bsort\s+out\b|\bwork\s+through\b", re.I)
# Installing or deploying software. Not "install the car seat": it needs a software word or a package manager.
_INSTALL_RX = re.compile(
    r"\b(?:install(?:ing|s)?|re-?install|set(?:ting)?\s*up|deploy(?:ing|s)?|self-?host(?:ing)?|spin(?:ning)?\s+up)\b"
    r"[^.\n;]{0,60}?\b(?:on\s+(?:titan|the\s+server|the\s+host|the\s+box|my\s+mac|the\s+mac)|software|package|"
    r"container|docker|server|service|daemon|agent|plugin|monitoring)\b"
    r"|\b(?:apt(?:-get)?|brew|pip3?|npm|cargo|docker|podman|helm|kubectl|snap|dnf|yum)\s+(?:install|run|pull|add|compose)\b"
    r"|\bdocker[- ]compose\b|\bcurl\b[^\n]{0,80}\|\s*(?:ba)?sh\b", re.I)


def _norm(text: str | None) -> str:
    return " ".join(str(text or "").split())


def ticket_asked(words: str | None) -> bool:
    """The person asked for a ticket ("ticket it", "file a ticket", "make an issue")."""
    return bool(_TICKET_RX.search(_norm(words)))


def items_asked(words: str | None) -> bool:
    """The person asked for items to be added ("add the items")."""
    return bool(_ITEMS_RX.search(_norm(words)))


def durable_asked(words: str | None) -> bool:
    """The person's own words ask titan to plan or work on something over time. Beside ticket words, only the strong
    phrasing counts ("ticket it so you can work on it over time")."""
    text = _norm(words)
    if _DURABLE_STRONG_RX.search(text):
        return True
    return not (ticket_asked(text) or items_asked(text)) and bool(_DURABLE_WEAK_RX.search(text))


def installs_software(*texts: str | None) -> str | None:
    """The words in a plan that install or deploy software, or None."""
    for t in texts:
        m = _INSTALL_RX.search(_norm(t))
        if m:
            return m.group(0)
    return None


_YES_RX = re.compile(r"^\W*(?:yes|yep|yeah|sure|ok(?:ay)?|please do|do it|go ahead|sounds good)\b[\w\s,.!']{0,30}$", re.I)


def yes_to_offer(shown: str | None, words: str | None) -> bool:
    """A short yes to titan's own previous question offering a task or plan ("Want me to plan it as a task?")."""
    prev, text = _norm(shown), _norm(words)
    return (bool(text) and len(text.split()) <= 6 and bool(_YES_RX.match(text)) and "?" in prev
            and bool(re.search(r"\b(?:task|plan)\b", prev, re.I)))


def plan_refusal(words: str | None, goal: str, steps: str, shown: str | None = None) -> str | None:
    """Why plan_task must not run for this message and plan, or None when it may. `words`: the person's own words this
    turn (None: no person's message, only the software rule applies); `shown`: titan's previous reply, whose offer of a
    task a bare yes accepts."""
    soft = installs_software(goal, steps)
    if soft:
        return (f"Not planned: titan never installs or sets up software in a task it runs by itself (“{soft}”). File "
                "it as a ticket instead with add_todo (the admin's: for='claude'), and say plainly that it's a ticket "
                "for a person to do, not something titan is doing.")
    if words is None:
        return None
    if ticket_asked(words) or items_asked(words):
        if durable_asked(words):
            return None
        return ("Not planned: they asked for a ticket, not a task titan works on by itself. Use add_todo: for the "
                "admin, for='claude' (it becomes a GitHub issue); for anyone else, their own list. Then say what you "
                "made and where it is.")
    if not durable_asked(words) and not yes_to_offer(shown, words):
        return ("Not planned: a durable task is only for when the person asks titan to plan or work on something over "
                "time, and they didn't. If it's something to do, use add_todo; if you think a task titan works "
                "through would help, ask them first.")
    return None
