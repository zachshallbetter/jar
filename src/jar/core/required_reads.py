#!/usr/bin/env python3
"""Questions that need a read before they can be answered (#1203 fix 2, #1244).

The box's local brain answered these from its context, with no tool call at all, and was wrong or stale (turn_eval,
10-07): the routines from an old chat turn, who's in the household from the prefix, invented fences, "no info" on a
trade-in its threads hold, "no new updates" from a group chat it never read, a photo follow-up from memory. Each row is
a question shape and the reader that answers it. When a question matches a row, the reader is declared for this asker
and none of that row's readers ran this turn, the reply gets one retry: read it, then answer.

What this never does:
  - ask for a reader the asker's role doesn't have: only declared tools count (a kid has no search_messages, a shared
    screen no list_private), so the retry can't widen what anyone reads;
  - read anything itself: the model makes the call, through the tool's own rules (search_messages reads the asker's own
    Mac; threads, list_private and list_routines are per viewer), so a group the asker isn't in yields nothing of anyone
    else's;
  - match broadly: each row is a shape turn_eval and the truth banks pin, nothing more. (No photo row: "What's in the
    bottom corner of it?" names its photo only through the conversation, and a question-only pattern for photos
    would catch "draw the picture"; attachment-follow-up stays open on #1203.)
"""
from __future__ import annotations

import re

import file_tool

# (what it's about, in the retry's words; the question shape; its readers, in order of preference)
ROWS: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("the routines", re.compile(r"\broutines?\b", re.I), ("list_routines",)),
    ("what's set up here", re.compile(
        r"\bwho(?:'s|’s| is| are)? in (?:the|my|our|this) (?:household|family|house)\b|\bwhat(?:'s|’s| is) (?:set up|connected)\b|"
        r"\bwhat could i give you\b|\bimprove your abilit", re.I), ("capabilities",)),
    ("what's kept private", re.compile(
        r"\bfence[sd]?\b|\bwhat(?:'s|’s| is| do you keep| have i kept)? ?(?:kept )?private\b|\bkept (?:it )?private\b", re.I),
     ("list_private",)),
    ("the threads", re.compile(r"\bthreads?\b|\btrade-?in\b", re.I), ("threads",)),
    # #1244: a group's or a person's messages ("Anything new from the soccer group?", "What did Robin text me?")
    ("those messages", re.compile(
        r"\b(?:anything|what(?:'s|’s| is)?) (?:new )?(?:from|in) (?:the |my |our )?[\w'’ -]{1,30}?\b(?:group|chat)\b|"
        r"\bwhat did (?:the |my |our )?[\w'’ -]{1,30}?\b(?:group|chat) (?:say|send)\b|"
        r"\bwhat did [A-Z][\w'’-]{1,20} (?:text|message|send)\b", re.I), ("search_messages",)),
    # #411 day one (10-09): the house right now ("What's on in the house right now?", "What lights are on?", "Is the
    # garage open?"). A first-run box's 8B answered "the devices are offline" with no read, 0/3. Questions only: a
    # command ("turn on the light") is home_control's, and never forced into a read here.
    ("the house right now", re.compile(
        r"\bwhat(?:'s|’s| is| are)? (?:on|running|playing|open|unlocked)\b(?=.*\b(?:house|home)\b)|"
        r"\bwhat(?:'s|’s| is)? the (?:house|home) doing\b|\b(?:status|state) of (?:the |my |our )?(?:house|home)\b|"
        r"\bwhat (?:lights?|devices?|switch(?:es)?|fans?|tvs?) (?:are|is) (?:on|off|open|running|playing)\b|"
        r"\bis (?:anything|something) (?:on|left on|running|open|unlocked)\b|"
        r"^\W*(?:is|are) (?:the |my |our )?[\w'’ ]{1,30}? (?:on|off|locked|unlocked|open|closed|running|playing)\W*$",
        re.I | re.S), ("home_status",)),
    # #1251: the person's Mac right now ("What app am I using on my Mac right now?"): the Mac's own reads, never the
    # calendar. Declared only when the Mac can be reached this turn (a phone turn gets them through cross-device).
    ("your Mac right now", re.compile(
        r"(?=.*\bmy mac(?:book)?\b)(?=.*(?:\b(?:what|which) (?:app|window)\b|\bam i (?:using|looking at|in)\b|"
        r"\bin front\b|\bfrontmost\b|\bon (?:my |the )?screen\b))", re.I | re.S),
     ("get_frontmost_app", "get_screen", "get_field_state")),
)

# #1251: requests that need one act, not a read: the box's 8B read reminders and the calendar instead (turn_eval
# task-next-later, task-keep-going, task-continue). Same rule as the reads: only a tool declared for this asker (the
# task tools are the admin's, schedule_action never a shared screen's), and the model makes the call, through the
# tool's own confirmations. Never asked when anything was already changed this turn (the retry's own rule).
# (what it is, the request's shape, its tool, what else the asker must have: a task's step scheduled runs run_task)
ACTS: tuple[tuple[str, re.Pattern[str], tuple[str, ...], frozenset[str]], ...] = (
    ("passing the answer to the task", re.compile(r"\bcontinue (?:the |my )?task\b|\b(?:answer|reply to) (?:the |my )?task\b",
                                                  re.I), ("task_answer",), frozenset()),
    ("scheduling the task's next step", re.compile(
        r"(?=.*\b(?:next|following) step\b)(?=.*\btask\b)(?=.*(?:\bat \d|\bon (?:mon|tue|wed|thu|fri|sat|sun)|"
        r"\btomorrow\b|\btonight\b|\bin (?:an?|\d+) (?:hour|minute)s?\b))", re.I | re.S), ("schedule_action",),
     frozenset({"run_task"})),
    ("setting the task to keep going on its own", re.compile(
        r"(?=.*\btask\b)(?=.*\b(?:on (?:your|its) own|by (?:your|it)self|once a day|every day|each day|daily|"
        r"every (?:morning|evening|night))\b)", re.I | re.S), ("task_autonomy",), frozenset()),
    # #1251's brain-size rows (turn_eval name-images, gap-keep): the 8B, even told to act, didn't call the tool. On the
    # local brain this retry's first round can only be that call (agent_runtime_lib.tool_call_grammar). Never a row
    # for set_preference ("stop asking me for permission"): how titan asks before acting is set by the person
    # themselves, never forced (Coordination, 10-08); FORCE_NEVER holds it out in code too.
    ("naming the pictures by what's in them", re.compile(
        r"\b(?:re)?name\b.*\b(?:pictures?|photos?|images?|captures?|screenshots?|pics)\b.*"
        r"\bwhat(?:'s|’s| is)? (?:in|on|shown)\b", re.I | re.S), ("name_images",), frozenset()),
    ("saving that as a skill", re.compile(
        r"\bi(?:'|’)?ll want (?:that|it|this) again\b|\b(?:save|keep) (?:that|it|this) (?:as a skill|for next time|for later)\b|"
        r"\bmake (?:that|it|this) (?:a|into a) skill\b", re.I), ("create_skill",), frozenset()),
)


def _declared_as(tool: str, declared: set[str]) -> str | None:
    """The name a row's tool is declared under this turn: itself, or `files` for the old file tools (#1251 C: they're
    its modes now, never declared, still what runs). None when neither is declared for this asker."""
    if tool in declared:
        return tool
    return file_tool.MERGED if tool in file_tool.ALIASES and file_tool.MERGED in declared else None


def _ran(used: set[str]) -> set[str]:
    """What ran, with `files` counted when one of its modes' tools did (a `files` call runs as that tool)."""
    return set(used) | ({file_tool.MERGED} if set(used) & file_tool.ALIASES else set())


def needed(question: str, declared: set[str], used: set[str]) -> list[tuple[str, str]]:
    """[(what it's about, the reader to call)] for each row the question matches whose reader is declared for this
    asker and none of whose readers ran this turn. [] when nothing is missing."""
    out: list[tuple[str, str]] = []
    used = _ran(used)
    for about, rx, readers in ROWS:
        if not rx.search(question or "") or used & set(readers):
            continue
        have = [d for d in (_declared_as(r, declared) for r in readers) if d]
        if have:
            out.append((about, have[0]))
    return out


# Tools a forced call may never be: a preference about asking first, or anything that changes approvals or
# confirmations, is the person's own explicit choice (Coordination, 10-08).
FORCE_NEVER = frozenset({"set_preference", "set_trust", "grant", "set_grant"})


def needed_act(question: str, declared: set[str], used: set[str]) -> tuple[str, str] | None:
    """(what it is, the tool) for the first ACTS row the question matches whose tool (and what it needs) is declared for
    this asker and didn't run OK this turn (`used`: the tools that ran OK; a call that failed, e.g. name_images with no
    paths, still leaves the act needed: #1251, 10-08); None otherwise. One act at most: two different acts in one retry is a guess."""
    used = _ran(used)
    for about, rx, tools, needs in ACTS:
        if not rx.search(question or ""):
            continue
        if used & set(tools) or not all(_declared_as(n, declared) for n in needs):
            return None
        have = [d for d in (_declared_as(t, declared) for t in tools) if d]
        return (about, have[0]) if have else None
    return None


def mac_now(question: str) -> bool:
    """Whether the question asks about the person's Mac right now (the "your Mac right now" row's shape)."""
    return any(about == "your Mac right now" and rx.search(question or "") for about, rx, _ in ROWS)


# #1249: a follow-up about an attachment earlier in the SAME conversation ("What's in the bottom corner of it?"). The
# question alone can't say it's about a photo, so this needs both: titan found an attachment in the asker's own earlier
# messages (agent_runtime_lib._earlier_attachment), and the question points back at something visual in it.
_DEICTIC_RX = re.compile(r"\b(?:it|this|that|these|those|the)\b", re.I)
_VISUAL_RX = re.compile(r"\b(?:corner|background|foreground|middle|top|bottom|left|right|caption|logo|label|title|"
                        r"text|written|writing|says|say|shown|show|showing|photo|picture|pic|image|screenshot|chart|"
                        r"graph|diagram|table|document|pdf|scan|page|colou?r|face|sign)\b", re.I)


_MAKE_RX = re.compile(r"\b(?:draw|paint|sketch|make|generate|create|design|render)\b", re.I)


def refers_to_attachment(question: str) -> bool:
    """Whether a follow-up points back at something visual ("the bottom corner of it", "what does that sign say"),
    not just "it" ("Is it going to rain?" doesn't). Only asked when the conversation has an earlier attachment."""
    q = question or ""
    if _MAKE_RX.search(q):
        return False   # "draw the picture…", "make an image…": a new one, not the attachment
    return bool(_DEICTIC_RX.search(q) and _VISUAL_RX.search(q))


def attachment_reader(hint: str) -> str | None:
    """The reader _earlier_attachment's hint names (view_image on the phone, describe_image or file_read on titan)."""
    for r in ("view_image", "describe_image", "file_read"):
        if f"with {r} at" in (hint or ""):
            return r
    return None
