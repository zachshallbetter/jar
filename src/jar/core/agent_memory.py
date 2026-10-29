#!/usr/bin/env python3
"""
Titan Agent Memory  —  L0-L3 hierarchical memory pyramid
=========================================================
Port of TencentDB-Agent-Memory's 4-layer architecture to Python + SQLite.

Layers
------
  L0  Raw conversation messages          (SQLite: titan_memory.db / messages)
  L1  Atomic facts extracted by LLM      (SQLite: facts table + BM25 keyword index)
  L2  Scene/scenario blocks              (~/.config/jellybean/memory/refs/scene-<id>.md)
  L3  User persona (long-horizon)        (~/.config/jellybean/memory/persona.md)

Recall API
----------
  result = memory.recall(query, conv_id)
  result.prependContext        → prepend to user message  (L1 facts, per-turn)
  result.appendSystemContext   → append to system prompt  (L3 persona + L2 scene nav)

Context compression
-------------------
  ref_id = memory.offload(tool_output_text)  → writes to refs/ref-<id>.md, returns node_id
  memory.resolve(node_id)                    → reads back full text

Usage in agent_api_server.py
-----------------------------
  from agent_memory import AgentMemory, RecallResult
  mem = AgentMemory()
  recall = mem.recall(question, conv_id=turn_id)
  # inject into messages:
  #   system_msg += recall.appendSystemContext
  #   user_msg = recall.prependContext + user_msg
  # After turn:
  mem.add_message(conv_id, "user", question)
  mem.add_message(conv_id, "assistant", reply)
  mem.maybe_extract_facts(conv_id)   # triggers L1 every N turns
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
import agent_paths  # noqa: E402
from ops_ports import CODE_MODEL_URL  # noqa: E402
_MEMORY_DIR = agent_paths.memory_dir()
_DB_PATH    = _MEMORY_DIR / "titan_memory.db"
EMBED_MAX_CHARS = int(os.environ.get("AGENT_EMBED_MAX_CHARS", "6000"))   # ≈ 1,500 tokens of English
_REFS_DIR   = _MEMORY_DIR / "refs"
_PERSONA_MD = _MEMORY_DIR / "persona.md"

L1_EXTRACT_EVERY_N    = int(os.environ.get("MEMORY_L1_EVERY_N", "5"))
EXTRACTED_CONFIDENCE = 0.7   # a fact a model read out of chat (10-02 audit: wrong ones were stored at 1.0)
# Who reads facts out of chat (#1090, decided 10-06): Qwen3.6 on :8198 (on a fictional bank it echoed nothing and
# stored 2 false facts where the 8B stored 13), falling back to titan's local brain (AGENT_ENDPOINT) when :8198 is
# unreachable or busy past MEMORY_EXTRACT_TIMEOUT. Each stored fact says which one (facts.extractor).
MEMORY_EXTRACT_URL = os.environ.get("MEMORY_EXTRACT_URL", CODE_MODEL_URL).rstrip("/")
MEMORY_EXTRACT_MODEL = os.environ.get("MEMORY_EXTRACT_MODEL", "qwen3.6-35b-a3b")
MEMORY_EXTRACT_TIMEOUT = float(os.environ.get("MEMORY_EXTRACT_TIMEOUT", "25"))
# One JSON line per extraction, counts only (no fact text): how many lines the model wrote, how many echoed a known
# fact, NONE, stored, replaced, and the corrections correction_problem blocked (#1090 follow-up). Not the turn log:
# its readers count every line as a turn.
MEMORY_EXTRACT_LOG = os.environ.get("MEMORY_EXTRACT_LOG", "/ai/logs/memory-extract.jsonl")


def _log_extraction(rec: dict[str, Any]) -> None:
    try:
        with open(MEMORY_EXTRACT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except OSError:
        pass   # best effort: a log never breaks memory
L3_PERSONA_EVERY_N_L1 = int(os.environ.get("MEMORY_L3_EVERY_N", "50"))
MAX_RECALL_FACTS      = int(os.environ.get("MEMORY_MAX_RECALL_FACTS", "12"))
MAX_SCENE_LINES       = int(os.environ.get("MEMORY_MAX_SCENE_LINES", "6"))

# Hybrid recall: lexical BM25 fused with semantic vector search over the same
# titan-embeddings service the fleet already runs (nomic-embed-text-v1.5, 768-d,
# loopback, no auth). Fully optional — any failure degrades to pure BM25.
MEMORY_HYBRID       = os.environ.get("MEMORY_HYBRID", "1") not in ("0", "false", "")
AGENT_EMBED_URL     = os.environ.get("AGENT_EMBED_URL", "http://127.0.0.1:8015/v1/embeddings")
AGENT_EMBED_MODEL   = os.environ.get("AGENT_EMBED_MODEL", "nomic-embed-text-v1.5")
AGENT_EMBED_PROVIDER = os.environ.get("AGENT_EMBED_PROVIDER", "titan-embeddings")
# Facts belong to a person (scripts/household_identity.py). Facts from before people existed are
# Zach's (the migration in _init_schema), and every call that doesn't name a person means him, so
# the CLI, the canary and older callers keep working on his memory.
OWNER = "zach"                  # household_identity.ADMIN_PERSON_ID
_RRF_K            = 60          # reciprocal-rank-fusion constant
_BACKFILL_BUDGET  = 32          # max facts embedded per recall when catching up


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RecallResult:
    """Two prompt-injection points mirroring TencentDB-Agent-Memory's RecallResult."""
    prependContext: str = ""        # L1 facts  → prepend to user message (dynamic, per-turn)
    appendSystemContext: str = ""   # L3 persona + L2 scene nav → append to system prompt (stable)
    used: list = field(default_factory=list)   # the L1 fact texts this recall put in the context (#1072)


@dataclass
class TokenCounts:
    """Per-turn context-size accounting — all values are chars, not tokens."""
    estimated_input_chars: int = 0
    recall_injected_chars: int = 0
    history_avoided_chars: int = 0

    @property
    def estimated_savings_tokens(self) -> int:
        return max(0, self.history_avoided_chars - self.recall_injected_chars) // 4


@dataclass
class _OffloadEntry:
    node_id: str
    result_ref: str   # path relative to _REFS_DIR
    summary: str      # one-line Mermaid label


# ---------------------------------------------------------------------------
# BM25 (pure Python — no external dep)
# ---------------------------------------------------------------------------

class _BM25:
    """Minimal BM25 over a list of string documents. k1=1.5, b=0.75."""

    def __init__(self, docs: list[str]) -> None:
        self._docs = docs
        self._k1 = 1.5
        self._b  = 0.75
        self._tokenize = lambda s: re.findall(r"[a-z0-9]+", s.lower())
        tokenized = [self._tokenize(d) for d in docs]
        self._avgdl = sum(len(t) for t in tokenized) / max(len(tokenized), 1)
        self._tf: list[Counter] = [Counter(t) for t in tokenized]
        df: Counter = Counter()
        for t in tokenized:
            for tok in set(t):
                df[tok] += 1
        N = len(docs)
        self._idf: dict[str, float] = {
            tok: math.log((N - f + 0.5) / (f + 0.5) + 1)
            for tok, f in df.items()
        }

    def scores(self, query: str) -> list[float]:
        q_toks = re.findall(r"[a-z0-9]+", query.lower())
        result = []
        for i, tf in enumerate(self._tf):
            dl = sum(tf.values())
            score = 0.0
            for tok in q_toks:
                if tok not in self._idf:
                    continue
                f = tf[tok]
                score += self._idf[tok] * f * (self._k1 + 1) / (
                    f + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
                )
            result.append(score)
        return result

    def top_k(self, query: str, k: int = 10) -> list[tuple[int, float]]:
        sc = self.scores(query)
        return sorted(enumerate(sc), key=lambda x: -x[1])[:k]


# ---------------------------------------------------------------------------
# AgentMemory
# ---------------------------------------------------------------------------

# A "fact" that is really today's calendar or screen: dated, relative to today, or momentary.
_EPHEMERAL_FACT = re.compile(
    r"\b(today|tomorrow|tonight|yesterday|this (?:week|weekend|morning|afternoon|evening)|next week|"
    r"currently|right now|at the moment|is looking at|window titled|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{4}-\d{2}-\d{2})\b", re.I)


# What the extractor (the local Qwen3-8B) kept writing despite the prompt (09-30 review, 71 of 77
# facts forgotten): what the user asked or said in the conversation, not facts. Code, not the prompt.
_SUBJ = r"^(?:the )?(?:user(?:'s)?(?: and (?:their|the) household)?|household)"
_NON_FACT = re.compile(
    _SUBJ + r"\s+(?:is|was|has been|are|were)\s+(?:asking|looking|interested|concerned|checking|trying|involved|"
    r"talking|discussing|wondering|aware|about to|going|leaving|headed|heading|on (?:the|their) way|maintaining|"
    r"experiencing|planning)\b"
    r"|" + _SUBJ + r"\s+(?:asked|asks|mentioned|mentions|indicated|specified|discussed|expressed|instructed|requested|"
    r"said|stated|noted|corrected|confirmed|found|believes?|thinks?|wants to know|added|set|created|leaves for|left for|"
    r"(?:has|have) (?:instructed|identified|corrected|asked|mentioned|requested|decided to create)|"
    r"(?:has|have) a system|(?:has|have) (?:a|two|\d+) (?:calendar )?events?)\b"
    r"|" + _SUBJ + r"\s+wants? (?:a|an|the) [\w' ]{0,40}?(?:added|set|created|moved|sent|made|scheduled)\b"
    r"|^(?:the )?user and \w+ (?:are|have) (?:going|gone|left|leaving|already)\b"
    r"|\b(?:is|are) experiencing\b|\b(?:file|folder) named\b"
    r"|(?:^|\s)(?:/ai/|/users/|/home/|/etc/|~/)"   # server and Mac paths are infrastructure (09-30: a token's path)
    # A meal is the timeline's (the dinner log). Live 10-01 the local model wrote "There was leftover
    # chili for dinner"; allergies and diets are kept before this rule is reached (_DIET).
    r"|\b(?:had|ate|eaten|was|were)\b[^.]{0,60}\bfor (?:dinner|lunch|breakfast)\b|\bhad leftovers?\b"
    # Statements about the assistant's abilities, not every mention ("prefers Claude for work writing" stays).
    r"|\b(?:the assistant|this assistant|titan|jellybean|claude|the agent|the ai)\s+(?:can|can't|cannot|could|will|"
    r"won't|should|is able|isn't able|is unable|does|doesn't|needs)\b"
    r"|\bcannot initiate\b|\bmust set reminders\b", re.I)
# needs to / has to / must is a task only with a deadline (10-01 review: "must avoid peanuts because of an
# allergy" and "needs to take blood-pressure medication daily" are what memory is for).
_TASK = re.compile(_SUBJ + r"\s+(?:needs to|has to|must|should)\b[^.]*\b(?:by|today|tomorrow|tonight|this (?:week|morning|"
                   r"afternoon|evening|weekend)|before \d|at \d|\d{1,2}(?::\d\d)?\s*(?:am|pm))\b", re.I)
_DIET = re.compile(r"\b(?:allerg|intoleran|can'?t eat|cannot eat|doesn'?t eat|does not eat|vegetarian|vegan|gluten|kosher|halal)", re.I)
# "has completed the integration of Alexa" after the user asked for it (09-30): a want stored as done.
_DONE_CLAIM = re.compile(r"\bhas (?:completed|integrated|set up|installed|connected|enabled|finished|added)\b", re.I)
_ASKING = re.compile(r"\b(?:i'?d like|i would like|i want|we want|can (?:we|you)|could (?:we|you)|let'?s|please add|"
                     r"add (?:that|it|this)|to claude|todo|to-do)\b", re.I)
_STOP = frozenset("the a an and or of to in on for with is are was were has have had their they them user user's "
                  "household who that this it its be been being as at by from during certain specific "
                  "you your yours yourself".split())   # a fact written to the person ("You…") says no more than "The user…"
# Persona sentences that guess rather than restate (10-01: "indicating responsible financial management").
_GUESS = re.compile(r"\b(?:likely|probably|suggest(?:s|ing)?|indicat(?:es|ing)|appears?|seems?|perhaps|may be|might)\b", re.I)


def _content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


# A correction must stand on what the person said (#1090). On a fictional bank the local 8B "corrected" facts nobody
# mentioned ("We adopted a cat on Saturday" → "REPLACES: Groceries are delivered on Saturdays"), invented the new value
# ("Jordan's birthday is in March, right?" → "…in April"), and Qwen3.6 pointed a new fact about Leo at the user's own
# allergy. Code, like the content-word rule below.
_CORRECTION_WORDS = frozenset("instead now anymore longer no not new currently switched changed".split())
_ARTICLES = frozenset("the a an this that these those our my his her their its".split())


_MONTHS = frozenset("january february march april may june july august september october november december".split())


def _keys(text: str) -> set[str]:
    """Content words, matched loosely: a month by its first 3 letters (October ~ Oct), any other word of 5+ letters by
    its first 4 (Thursdays ~ Thursday, swims ~ swimming)."""
    out = set()
    for w in _content_words(text):
        if w in _MONTHS or (len(w) == 3 and any(m.startswith(w) for m in _MONTHS)):
            out.add(w[:3])
        else:
            out.add(w[:4] if len(w) >= 5 else w)
    return out


def _subject(text: str) -> str:
    """Who a fact is about, by its first word: "you" for the person however it's written ("The user…", "You…",
    "Your…": #1605 writes facts in the second person, older ones aren't), "household" for the family or household, a
    first word otherwise, and "" when it starts with an article ("The balloons…": not a person, so never "another
    subject" than "Your balloons…")."""
    t = (text or "").strip()
    if re.match(r"(?i)(?:the user\b|user\b|you\b|your\b|yours\b)", t):
        return "you"
    if re.match(r"(?i)(?:the|our|your) (?:family|household)\b", t):
        return "household"
    m = re.match(r"([A-Za-z]+)", t)
    if not m or m.group(1).lower() in _ARTICLES:
        return ""
    return m.group(1).lower()


def correction_problem(old: str, new: str, user_lines: str) -> str:
    """'' when "REPLACES old: new" may stand. Otherwise why not:
      unmentioned    nothing the old fact is about appears in what the person said: drop the line
      other-subject  the new fact is about someone else: store it as a new fact, forget nothing
      unrelated      same subject, nothing else in common: store it as a new fact, forget nothing
      invented       the new fact brings words the person never said: drop the line"""
    said = _keys(user_lines)
    subj = _subject(old)
    connective = {w[:4] if len(w) >= 5 else w for w in _CORRECTION_WORDS}
    skip = {subj[:4] if len(subj) >= 5 else subj} | connective
    told = _keys(new) - connective
    # What it corrects was mentioned, or the correction itself is mostly the person's own words (09-30: "The total
    # should be 254.45 on Oct 27th" corrected the $400 bill without naming it; "We adopted a cat on Saturday" shares
    # only "Saturday" with "Groceries are delivered on Saturdays").
    if not ((_keys(old) - skip) & said) and not (told and len(told & said) * 2 >= len(told)):
        return "unmentioned"
    if _subject(old) and _subject(new) and _subject(old) != _subject(new):
        return "other-subject"
    if not ((_keys(old) & _keys(new)) - skip):   # same person, different topic ("Jordan is vegetarian" vs her birthday)
        return "unrelated"
    if (_keys(new) - _keys(old) - connective) - said:
        return "invented"
    return ""


_WHEN = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b|\b\d{1,2}(?::\d\d)?\s*(?:am|pm)\b", re.I)


def _specifics(text: str) -> tuple[set[str], set[str], set[str]]:
    """The names (capitalized words), numbers and days or times a fact names."""
    names = {w.lower() for w in re.findall(r"\b[A-Z][a-z]+\b", text) if w.lower() not in _STOP | {"user"}}
    return names, set(re.findall(r"\d+(?:[.,:]\d+)*", text)), {m.lower() for m in _WHEN.findall(text)}


def _near_duplicate(text: str, others: list[str], threshold: float = 0.6) -> bool:
    """Says what another fact says (09-30: seven wordings of one fact were stored). Never when they
    differ in a person, a number, or a day or time (10-01 review: Ava's ballet is not Leo's)."""
    a, sa = _content_words(text), _specifics(text)
    for o in others:
        b = _content_words(o)
        if a and b and len(a & b) / len(a | b) >= threshold and _specifics(o) == sa:
            return True
    return False


# The household roster is the people store's (household_identity), never memory's (10-02 audit: "The user's household
# has a child named Robin", an adult, stored at confidence 1.0, and "has a child named Leo", which the roster
# already says). A fact that says who's in the household, or what someone is to it, is dropped: when it agrees it's
# redundant, and when it doesn't it's wrong.
_KID_WORDS = r"(?:child|children|kid|kids|son|sons|daughter|daughters|baby|toddler|stepson|stepdaughter)"
_ADULT_WORDS = r"(?:wife|husband|partner|spouse|girlfriend|boyfriend|fianc[ée]e?|mother|mom|father|dad|adult|roommate|parent)"
# Only a roster statement: someone named, or a count of members ("a child who spends time with their father during
# certain weeks" says something the roster doesn't, and stays).
# "has a … named X" is a membership statement only when X is a household role (#1092: "The family has a cat named
# Biscuit", "Ava has a teacher named Ms. Ortiz" and "Leo has a friend called Sam" were dropped as roster facts).
_HOUSEHOLD_ROLE = (r"(?:child|children|kid|kids|son|sons|daughter|daughters|baby|toddler|stepson|stepdaughter|"
                   r"wife|husband|partner|spouse|girlfriend|boyfriend|members?|roommates?)")
_MEMBERSHIP = re.compile(r"\b(?:has|have|includes?|consists? of)\b[^.]{0,30}?\b" + _HOUSEHOLD_ROLE + r"\b[^.]{0,15}?"
                         r"\b(?:named|called)\b|"
                         r"\b(?:household|family)\b[^.]{0,20}\b(?:includes?|consists? of)\b[^.]{0,30}\b(?:named|called)\b|"
                         r"\b(?:household|family)\b[^.]{0,20}\b(?:has|have|includes?|consists? of) (?:\d+|one|two|three|four|"
                         r"five|six) (?:members|people|kids|children)\b|\bmembers? of the (?:household|family)\b", re.I)
# One-off events (10-02 audit): the prompt says "nothing dated", the local model writes them anyway. A recurring
# day ("on Thursdays", "every Monday") is kept.
_ONE_OFF = re.compile(
    r"\b(?:today|tonight|tomorrow|yesterday|this (?:morning|afternoon|evening|week|weekend)|last (?:night|week|weekend)|"
    r"next (?:week|weekend|month)|(?<!every )(?<!each )(?:on )?(?:mon|tues|wednes|thurs|fri|satur|sun)day"
    r"(?!s|\s+(?:nights|mornings|afternoons|evenings))|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?\b|\d{4}-\d{2}-\d{2})\b",
    re.I)


def roster_block(people: list[dict[str, Any]]) -> str:
    """The household as the people store has it, for the extraction prompt: who's an adult and who's a kid."""
    def first(p: dict[str, Any]) -> str:
        return str(p.get("name") or "").split(" ")[0]
    adults = [first(p) for p in people if p.get("role") in ("admin", "adult") and first(p)]
    kids = [first(p) for p in people if p.get("role") == "kid" and first(p)]
    if not adults and not kids:
        return ""
    out = "The household (already known; never write a fact about who is in it or what anyone is to it): "
    out += "; ".join(x for x in (f"adults: {', '.join(adults)}" if adults else "",
                                 f"kids: {', '.join(kids)}" if kids else "") if x) + "."
    other = sorted({str(p["other_parent"]) for p in people if p.get("role") == "kid" and p.get("other_parent")})
    if other:
        out += f" The kids' other parent ({', '.join(other)}) lives elsewhere and is none of the adults here."
    return out


def contradicts_roster(line: str, people: list[dict[str, Any]]) -> bool:
    """A fact about who's in the household or what a member is to it (the roster's to say), or one that calls an
    adult a child or a kid a partner. Code, not the prompt."""
    if _MEMBERSHIP.search(line):
        return True
    for p in people:
        name = str(p.get("name") or "").split(" ")[0]
        if len(name) < 2 or not re.search(r"\b" + re.escape(name) + r"\b", line):
            continue
        near = r"(?:\b" + re.escape(name) + r"\b[^.]{0,40}\b{w}\b|\b{w}\b[^.]{0,25}\b" + re.escape(name) + r"\b)"
        if p.get("role") in ("admin", "adult") and re.search(near.replace("{w}", _KID_WORDS), line, re.I):
            return True
        if p.get("role") == "kid" and re.search(near.replace("{w}", r"(?:wife|husband|partner|spouse|girlfriend|boyfriend)"),
                                                 line, re.I):
            return True
    return False


# Where the kids are staying is a state, not an event (custody.py holds it; memory keeps what was said).
_CUSTODY = re.compile(r"\bwith (?:their|his|her) (?:mom|mother|dad|father|other parent)\b|\bat (?:their|his|her) "
                      r"(?:mom|dad)'?s\b|\bcustody\b|\balternat\w+ weeks\b", re.I)


def one_off(line: str) -> bool:
    """A dated, one-time thing (a single day or date): the calendar's or the timeline's, not memory's."""
    return bool(_ONE_OFF.search(line)) and not (_DIET.search(line) or _CUSTODY.search(line))


# Zach 10-10 ("what it knows about me seems weak"): 8 of his 14 were wishes about titan ("The user wants to be able to
# ask anything…"), stored because the prompt asked for wants. A wish about the assistant or a system being built is a
# dev note (owner-only), never a fact about the person. Code, not the prompt.
_PRODUCT_WISH = re.compile(
    r"\b(?:the user|user|you|i)\s+(?:wants?|would like|wish(?:es)?|needs?|hopes?|asked for|is building|are building|"
    r"plans? to build)\b.{0,200}?\b(?:assistant|titan|jellybean|the app|an app|apps?|system|graph|feature|tool|model|"
    r"agent|ui|interface|dashboard|build|to be able to|ask (?:it|anything|me)|context across|integrat\w+|automat\w+)\b",
    re.I)
# Another adult's health or private matters inside someone else's memory (derived-details privacy, 10-10: a household
# member's prescription pickup in Zach's): beyond who they are to the person, never stored.
_PRIVATE_DETAIL = re.compile(
    r"\b(?:prescriptions?|pharmac(?:y|ies|ist)|medications?|meds|diagnos\w*|therap(?:y|ist)|doctor|clinic|surgery|"
    r"biopsy|pregnan\w*|rehab|counsel(?:ing|or)|psychiatr\w*|lab results?|blood (?:work|tests?)|treatment|"
    r"health|medical|hospital|appointments?)\b", re.I)


def product_wish(line: str) -> bool:
    """A wish about the assistant or a system being built: a dev note, never a personal fact."""
    return bool(_PRODUCT_WISH.search(line))


def other_adults_private(line: str, people: list[dict[str, Any]], person_id: str) -> bool:
    """A health or private detail about another adult of the household (never the person's own, never a kid's: a
    parent keeps their child's care, "Robin is Ava's therapist")."""
    if not _PRIVATE_DETAIL.search(line):
        return False
    for p in people:
        name = str(p.get("name") or "").split(" ")[0]
        if (p.get("role") in ("admin", "adult") and str(p.get("id")) != str(person_id) and len(name) >= 2
                and re.search(r"\b" + re.escape(name) + r"(?:'s)?\b", line)):
            return True
    return False


_ES_VERB = re.compile(r"(?:ss|sh|ch|x|z|o)es$")


def second_person(line: str) -> str:
    """A fact written to the person (Zach 10-10): "The user pays for parking with ParkMobile" → "You pay for parking
    with ParkMobile"; "The user's sister" → "Your sister". Anything it can't turn cleanly stays as written."""
    t = " ".join(line.split())
    t = re.sub(r"\b[Tt]he user and their\b", lambda m: "You and your" if m.start() == 0 else "you and your", t)
    t = re.sub(r"\b[Tt]he user's\b", lambda m: "Your" if m.start() == 0 else "your", t)
    for a, b in (("is", "are"), ("was", "were"), ("has", "have"), ("does", "do")):
        t = re.sub(rf"\b[Tt]he user {a}\b", lambda m, b=b: ("You " if m.start() == 0 else "you ") + b, t)

    def verb(m: re.Match) -> str:
        v = m.group(2)
        base = v[:-3] + "y" if v.endswith("ies") else v[:-2] if _ES_VERB.search(v) else v[:-1]
        return ("You " if m.start() == 0 else "you ") + base
    t = re.sub(r"\b([Tt]he user) ([a-z]+s)\b", verb, t)
    t = re.sub(r"\b[Tt]he user\b", lambda m: "You" if m.start() == 0 else "you", t)
    return t


# A standing rule the person gives about how to treat something (#1613: "Kate with Mom is a standing family event,
# don't check against it" was never kept). The model tends to write it as reported speech ("The user instructed that…"),
# which the non-fact rule drops, so the rule is kept as the rule itself. Never a one-off: it stands.
_STANDING = re.compile(
    r"\b(?:(?:a|is|are|as) standing\b|as a rule\b|"
    r"(?:don'?t|do not|never|always) (?:check|flag|count|schedule|book|remind|warn|ask|move|treat|double-book|cancel|"
    r"suggest|plan|list|show|mention)\b)", re.I)
_REPORTED = re.compile(
    r"^(?:the )?user(?: has)? (?:said|says|stated|states|instructed|instructs|noted|notes|clarified|clarifies|"
    r"explained|explains|confirmed|confirms|specified|specifies|mentioned|mentions|told (?:the assistant|titan|you))"
    r"(?: (?:the assistant|titan|you))?(?: that)?[,:]?\s+", re.I)


def standing_rule(line: str) -> str | None:
    """The standing rule a line states, as the rule ("The user said that Ava's time with Mom is a standing family
    event…" → "Ava's time with Mom is a standing family event…"), or None when it isn't one."""
    t = " ".join((line or "").split())
    if not _STANDING.search(t):
        return None
    t = _REPORTED.sub("", t, count=1)
    return t[:1].upper() + t[1:] if len(t) > 12 else None


# A correction in the person's own words (#1613: "No, red and baby blue"). An addition ("too", "also") never is.
_CORRECTING = re.compile(r"\b(?:no|nope|not|actually|instead|anymore|any more|now|changed|switched|wrong|correction|"
                         r"rather|isn't|aren't|wasn't|doesn't|don't)\b|n't\b", re.I)
_ADDING = re.compile(r"\b(?:too|also|as well|another|in addition|additionally)\b", re.I)


def superseded(line: str, have: list[tuple[str, str]], user_lines: str) -> tuple[str, str] | None:
    """The stored fact (id, text) a new line restates with a change, when the model didn't say "REPLACES" (#1613): the
    same subject and mostly the same words (half or more), the new words all said by the person, and either it only adds
    to the old (a fuller wording) or the person was correcting ("no", "actually", "now", "instead"). Never when they
    were adding ("too", "also"), never across subjects (Ava's ballet is not Leo's). None: a new fact or a duplicate."""
    new_w, new_k = _content_words(line), _keys(line)
    if not new_w:
        return None
    correcting = bool(_CORRECTING.search(user_lines)) and not _ADDING.search(user_lines)
    best = None
    for fid, old in have:
        o = second_person(old)
        ow = _content_words(o)
        if not ow or len(new_w & ow) / len(new_w | ow) < 0.5:
            continue
        added, removed = new_k - _keys(o), _keys(o) - new_k
        if not added or (removed and not correcting):
            continue      # says nothing new (a duplicate), or a changed value nobody said was a correction
        if correction_problem(o, line, user_lines):
            continue      # not mentioned, another subject, unrelated, or words the person never said
        score = len(new_w & ow) / len(new_w | ow)
        if best is None or score > best[0]:
            best = (score, fid, old)
    return (best[1], best[2]) if best else None


CARD_PROMPT_MARK = "(from a suggestion card)"   # mail_suggestions' buttons start with this (#533 follow-up)


def _keep_extracted(line: str, user_lines: str) -> bool:
    """Whether an extracted line is a durable fact worth storing (code-enforced, 10-01)."""
    if _DIET.search(line):   # allergies and diets are what memory is for, whatever else the line says
        return not (_EPHEMERAL_FACT.search(line) or len(_content_words(line)) < 2)
    if _NON_FACT.search(line) or _TASK.search(line) or _EPHEMERAL_FACT.search(line) or len(_content_words(line)) < 2:
        return False
    return not (_DONE_CLAIM.search(line) and _ASKING.search(user_lines))


def _asked(reply: str) -> str:
    """The question an assistant reply ends with (for context), or ''."""
    text = " ".join((reply or "").split())
    if "?" not in text:
        return ""
    head = text[: text.rindex("?") + 1]
    starts = [head.rfind(p) for p in (". ", "! ", "\n", ": ")]
    q = head[max(starts) + 2:] if max(starts) >= 0 else head
    return q[-200:]


class AgentMemory:
    """
    Thread-safe (WAL mode) SQLite-backed memory store.
    All LLM calls use the agent_runtime_lib Gemini/local path — import is lazy
    to avoid circular imports when this module is imported early.
    """

    def __init__(self) -> None:
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _REFS_DIR.mkdir(parents=True, exist_ok=True)
        self._db = self._connect()
        self._init_schema()
        self._offload_index: list[_OffloadEntry] = self._load_offload_index()
        self._lock = threading.Lock()          # serializes check-then-insert ops
        self._last_persona_at: int = 0         # fact count at last L3 refresh
        self._last_model: str | None = None    # the model the last _llm_call used (#1090: facts.extractor)
        # Cached BM25 indexes, per person: {person_id: (index, the facts it was built from)}.
        # None = dirty (any fact changed).
        self._bm25: dict[str, tuple[_BM25, list[str]]] | None = None

    # ── DB ───────────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
            id          TEXT PRIMARY KEY,
            conv_id     TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT NOT NULL,
            ts          REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, ts);

        CREATE TABLE IF NOT EXISTS facts (
            id          TEXT PRIMARY KEY,
            conv_id     TEXT NOT NULL,
            text        TEXT NOT NULL,
            confidence  REAL NOT NULL DEFAULT 1.0,
            ts          REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_facts_conv ON facts(conv_id, ts);

        CREATE TABLE IF NOT EXISTS fact_vectors (
            text_hash   TEXT PRIMARY KEY,
            vec         BLOB NOT NULL,
            ts          REAL NOT NULL,
            provider    TEXT,
            model       TEXT,
            dim         INTEGER
        );

        CREATE TABLE IF NOT EXISTS scenes (
            id          TEXT PRIMARY KEY,
            conv_id     TEXT NOT NULL,
            title       TEXT NOT NULL,
            md_path     TEXT NOT NULL,
            ts          REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scenes_conv ON scenes(conv_id, ts);

        CREATE TABLE IF NOT EXISTS offload_index (
            node_id     TEXT PRIMARY KEY,
            result_ref  TEXT NOT NULL,
            summary     TEXT NOT NULL,
            ts          REAL NOT NULL
        );
        """)
        # Migration: fact_vectors predates the provider/model/dim version-tag
        # columns (added to close the Continue-#906-style stale-vector gap —
        # a DB created before this change has the 3-column table above, which
        # CREATE TABLE IF NOT EXISTS leaves untouched). Add them if missing;
        # existing rows get NULL, which never matches a real (provider,
        # model, dim) tuple, so they're correctly treated as stale/absent
        # rather than silently trusted.
        # Pinned facts ride along in every recall; forgotten ones move aside (restorable), not away.
        fact_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(facts)").fetchall()}
        if "pinned" not in fact_cols:
            self._db.execute("ALTER TABLE facts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        # When a real turn's recall last put a fact in front of the model, and how often (#1072). NULL: never seen
        # used since tracking began (memory_meta usage_since), not "never used".
        if "last_used" not in fact_cols:
            self._db.execute("ALTER TABLE facts ADD COLUMN last_used REAL")
        if "use_count" not in fact_cols:
            self._db.execute("ALTER TABLE facts ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0")
        # Each fact's group (#1073): person:<id>, or routine / preference / place / work / health / other
        # (memory_groups.py). NULL: not grouped yet.
        if "grp" not in fact_cols:
            self._db.execute("ALTER TABLE facts ADD COLUMN grp TEXT")
        # Which model read the fact out of chat (#1090), as model:<name>. NULL: said to titan directly, or stored
        # before this was recorded.
        if "extractor" not in fact_cols:
            self._db.execute("ALTER TABLE facts ADD COLUMN extractor TEXT")
        self._db.execute("CREATE TABLE IF NOT EXISTS memory_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self._db.execute("INSERT OR IGNORE INTO memory_meta VALUES ('usage_since', ?)", (str(time.time()),))
        self._db.execute("""CREATE TABLE IF NOT EXISTS forgotten_facts (
            id TEXT PRIMARY KEY, conv_id TEXT, text TEXT NOT NULL, confidence REAL, ts REAL,
            forgotten_at REAL NOT NULL)""")
        # A bulk forget's undo token and the group, so a batch comes back exactly as it was (#1073)
        fcols = {r["name"] for r in self._db.execute("PRAGMA table_info(forgotten_facts)").fetchall()}
        for col, decl in (("grp", "TEXT"), ("batch", "TEXT"), ("pinned", "INTEGER NOT NULL DEFAULT 0"),
                          ("last_used", "REAL"), ("use_count", "INTEGER NOT NULL DEFAULT 0")):
            if col not in fcols:   # a restored fact keeps its pin and its use (#1073 review)
                self._db.execute(f"ALTER TABLE forgotten_facts ADD COLUMN {col} {decl}")
        # Per-person memory (2026-09-30): every fact and forgotten fact names its person; the ones
        # from before are Zach's. Idempotent: runs on every start, touches only unassigned rows.
        # Which app conversation and device a message came from (10-01, jellybean#117: topic cards).
        mcols = {r["name"] for r in self._db.execute("PRAGMA table_info(messages)").fetchall()}
        # client_msg_id: the app's own id (continuity echoes); tools: a reply's tool names, no arguments (#1016 step 5);
        # platform: the paired device's kind, mac/iphone/ipad/watch/tv (#1287; NULL: unknown, the master token, scripts)
        for col in ("thread", "device", "client_msg_id", "tools", "platform"):
            if col not in mcols:
                self._db.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
        for table in ("facts", "forgotten_facts"):
            cols = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
            if "person_id" not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN person_id TEXT")
            self._db.execute(f"UPDATE {table} SET person_id=? WHERE person_id IS NULL", (OWNER,))
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_facts_person ON facts(person_id, ts)")
        # A wish about titan or a system being built (Zach 10-10): its owner's dev notes, never their memory, never
        # recalled. Read only by that person (GET /v1/memory/dev_notes); delete-my-data covers them (me_delete).
        self._db.execute("CREATE TABLE IF NOT EXISTS dev_notes (id TEXT PRIMARY KEY, person_id TEXT NOT NULL, "
                         "conv_id TEXT, text TEXT NOT NULL, ts REAL NOT NULL)")
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_dev_notes_person ON dev_notes(person_id, ts)")
        existing_cols = {r["name"] for r in self._db.execute("PRAGMA table_info(fact_vectors)").fetchall()}
        for col, coltype in (("provider", "TEXT"), ("model", "TEXT"), ("dim", "INTEGER")):
            if col not in existing_cols:
                self._db.execute(f"ALTER TABLE fact_vectors ADD COLUMN {col} {coltype}")
        self._db.commit()

    # ── L0: raw messages ─────────────────────────────────────────────────────

    def add_message(self, conv_id: str, role: str, content: str, *, thread: str | None = None,
                    device: str | None = None, client_msg_id: str | None = None,
                    tools: list[str] | None = None, platform: str | None = None) -> dict[str, Any]:
        """`thread`: the app's own conversation id when it sends one (the topic cards, jellybean#117);
        `device`: which device it was said on ("Zach's iPhone"); `client_msg_id`: the app's id for the
        message, so its own echo in the continuity feed can be skipped; `platform`: that device's kind
        (mac, iphone, ipad, watch, tv; #1287), None when unknown. Returns {id, ts}."""
        mid, ts = str(uuid.uuid4()), time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO messages (id, conv_id, role, content, ts, thread, device, client_msg_id, tools, "
                "platform) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mid, conv_id, role, content, ts, thread, device, client_msg_id,
                 json.dumps([str(t)[:60] for t in tools][:12]) if tools else None, platform)
            )
            self._db.commit()
        return {"id": mid, "ts": ts}

    def get_messages(self, conv_id: str, limit: int = 40) -> list[dict]:
        rows = self._db.execute(
            "SELECT role, content, ts FROM messages WHERE conv_id=? ORDER BY ts DESC LIMIT ?",
            (conv_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def turn_count(self, conv_id: str | None = None) -> int:
        if conv_id:
            return self._db.execute(
                "SELECT COUNT(*) FROM messages WHERE conv_id=? AND role='user'", (conv_id,)
            ).fetchone()[0]
        return self._db.execute(
            "SELECT COUNT(*) FROM messages WHERE role='user'"
        ).fetchone()[0]

    # ── L1: atomic facts ─────────────────────────────────────────────────────

    def add_fact(self, conv_id: str, text: str, confidence: float = 1.0, *, person_id: str,
                 extractor: str | None = None) -> str | None:
        """Store a fact. → the older fact's text when this one replaced it (a changed value), else None."""
        old = None
        with self._lock:
            kind, row_id = self._match_fact(text, person_id=person_id)
            if kind == "duplicate":
                return None
            if kind == "update":
                r = self._db.execute("SELECT text FROM facts WHERE id=?", (row_id,)).fetchone()
                old = r[0] if r else None
                # Same fact, new value (a changed phone number or code): the newer statement
                # replaces the older one so recall can't surface the stale value.
                self._db.execute("UPDATE facts SET conv_id=?, text=?, confidence=?, ts=?, extractor=? WHERE id=?",
                                 (conv_id, text.strip(), confidence, time.time(), extractor, row_id))
            else:
                self._db.execute(
                    "INSERT INTO facts (id, conv_id, text, confidence, ts, person_id, extractor) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), conv_id, text.strip(), confidence, time.time(), person_id, extractor)
                )
            self._db.commit()
            self._bm25 = None   # invalidate cached index
        # Embed outside the lock (network call); safe to fail — recall backfills.
        if MEMORY_HYBRID:
            self._store_vectors([text.strip()])
        return old

    def _fact_exists(self, text: str, *, person_id: str) -> bool:
        # Must be called under self._lock.
        return self._match_fact(text, person_id=person_id)[0] == "duplicate"

    def _match_fact(self, text: str, *, person_id: str) -> tuple[str, str | None]:
        """Classify a new fact against recent ones: ("duplicate", id) when it says the same thing
        with the same values, ("update", id) when it states the same fact with different values
        (tokens containing digits — numbers, codes, dates), else ("new", None).

        Word overlap alone dropped a long fact whose number changed as a duplicate (one token of
        ten differs → 0.9), and kept a short one alongside the stale value (0.67), so recall could
        return either number. Must be called under self._lock."""
        words, values = self._split_tokens(text)
        # Only this person's facts: Robin's phone number must never "update" Zach's.
        rows = self._db.execute("SELECT id, text FROM facts WHERE person_id=? ORDER BY ts DESC LIMIT 200",
                                (person_id,)).fetchall()
        for r in rows:
            r_words, r_values = self._split_tokens(r["text"])
            if values and r_values and values != r_values and self._jaccard(words, r_words) >= 0.85:
                return "update", r["id"]
            if values == r_values and self._similarity(r["text"], text) > 0.85:
                return "duplicate", r["id"]
        return "new", None

    @staticmethod
    def _split_tokens(text: str) -> tuple[set[str], set[str]]:
        toks = set(re.findall(r"[a-z0-9]+", text.lower()))
        values = {t for t in toks if any(c.isdigit() for c in t)}
        return toks - values, values

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        return len(a & b) / len(a | b) if a and b else 0.0

    def _similarity(self, a: str, b: str) -> float:
        ta = set(re.findall(r"[a-z0-9]+", a.lower()))
        tb = set(re.findall(r"[a-z0-9]+", b.lower()))
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    def get_facts(self, conv_id: str | None = None, limit: int = 200, *, person_id: str) -> list[str]:
        """A person's facts, newest first (optionally one conversation's)."""
        q, args = "SELECT text FROM facts WHERE person_id=?", (person_id,)
        if conv_id:
            q, args = q + " AND conv_id=?", args + (conv_id,)
        rows = self._db.execute(q + " ORDER BY ts DESC LIMIT ?", args + (limit,)).fetchall()
        return [r["text"] for r in rows]

    def _noted(self, person_id: str) -> dict[str, str]:
        """fact text → when it was noted, in the household's words ("today", "yesterday", "Sep 29",
        "Mar 3, 2025"), so a recalled fact says how old it is (10-01: time and order right
        everywhere; "Ava's piano moved to Thursdays" read the same a day or a year later)."""
        from datetime import datetime
        import local_time
        now = local_time.now()
        out: dict[str, str] = {}
        for r in self._db.execute("SELECT text, MAX(ts) AS ts FROM facts WHERE person_id=? GROUP BY text", (person_id,)):
            try:
                d = datetime.fromtimestamp(float(r["ts"]), now.tzinfo)
            except (TypeError, ValueError, OSError):
                continue
            days = (now.date() - d.date()).days
            out[r["text"]] = ("today" if days == 0 else "yesterday" if days == 1
                              else f"{d:%b} {d.day}" if d.year == now.year else f"{d:%b} {d.day}, {d.year}")
        return out

    _SORTS = {"pinned": "pinned DESC, ts DESC", "use": "use_count DESC, last_used DESC, ts DESC",
              "new": "ts DESC", "old": "ts ASC"}

    def list_facts(self, limit: int = 200, conv_id: str | None = None, *, person_id: str, sort: str = "pinned",
                   unused_since: float | None = None, group: str | None = None, source: str | None = None) -> list[dict]:
        """A person's facts with their ids: [{id, text, ts, confidence, pinned, last_used, use_count}]. sort: pinned
        (pinned first, then newest; the default), use (most used first), new, old. unused_since: only facts no real
        turn has used since then (never used counts as unused)."""
        q, args = ("SELECT id, text, ts, confidence, pinned, last_used, use_count, grp FROM facts WHERE person_id=?",
                   (person_id,))
        if group:
            q, args = q + " AND COALESCE(grp, 'other')=?", args + (group,)
        if source == "said":      # told to titan (#1073): stored at full confidence
            q += " AND confidence >= 1.0"
        elif source == "chat":    # read out of a chat by a model (EXTRACTED_CONFIDENCE)
            q += " AND confidence < 1.0"
        if conv_id:
            q, args = q + " AND conv_id=?", args + (conv_id,)
        if unused_since is not None:
            q, args = q + " AND (last_used IS NULL OR last_used < ?)", args + (float(unused_since),)
        order = self._SORTS.get(sort, self._SORTS["pinned"])
        rows = self._db.execute(q + f" ORDER BY {order} LIMIT ?", args + (limit,)).fetchall()
        return [{"id": r["id"], "text": r["text"], "ts": r["ts"], "confidence": r["confidence"],
                 "pinned": bool(r["pinned"]), "last_used": r["last_used"], "use_count": int(r["use_count"] or 0),
                 "group": r["grp"] or "other", "source": "said" if (r["confidence"] or 0) >= 1.0 else "chat"}
                for r in rows]

    def stamp_used(self, texts: list[str], *, person_id: str, at: float | None = None) -> int:
        """A real turn's recall put these facts in front of the model: last_used = now, use_count + 1 (#1072). Only
        that person's facts. Never for an eval, canary or pinned turn (the caller decides). → rows stamped."""
        texts = [t for t in dict.fromkeys(texts or []) if t]
        if not texts:
            return 0
        with self._lock:
            cur = self._db.execute(f"UPDATE facts SET last_used=?, use_count=use_count+1 WHERE person_id=? AND text IN "
                                   f"({','.join('?' * len(texts))})", (at or time.time(), person_id, *texts))
            self._db.commit()
            return cur.rowcount

    def usage_since(self) -> float | None:
        r = self._db.execute("SELECT v FROM memory_meta WHERE k='usage_since'").fetchone()
        return float(r[0]) if r else None

    def pinned_facts(self, limit: int = 5, *, person_id: str) -> list[str]:
        rows = self._db.execute("SELECT text FROM facts WHERE pinned=1 AND person_id=? ORDER BY ts DESC LIMIT ?",
                                (person_id, limit)).fetchall()
        return [r["text"] for r in rows]

    def add_dev_note(self, conv_id: str, text: str, *, person_id: str) -> None:
        """A wish about titan or a system being built: its owner's dev notes (never their memory, never recalled)."""
        text = " ".join(str(text or "").split())
        if not text or not person_id:
            return
        with self._lock:
            if not self._db.execute("SELECT 1 FROM dev_notes WHERE person_id=? AND text=?", (person_id, text)).fetchone():
                self._db.execute("INSERT INTO dev_notes (id, person_id, conv_id, text, ts) VALUES (?,?,?,?,?)",
                                 (str(uuid.uuid4()), person_id, conv_id, text, time.time()))
            self._db.commit()

    def dev_notes(self, *, person_id: str, limit: int = 200) -> list[dict]:
        """One person's own dev notes, newest first ({id, text, ts}; never another person's)."""
        rows = self._db.execute("SELECT id, text, ts FROM dev_notes WHERE person_id=? ORDER BY ts DESC LIMIT ?",
                                (person_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def edit_fact(self, fact_id: str, text: str, *, person_id: str) -> dict | None:
        """The person rewrites one of their own facts (Zach 10-10: editable, not only Forget). In place: the same id,
        group, pin and use, so Settings keeps one row per thing; the new wording is theirs (confidence 1.0, extractor
        "person"). The reply carries the previous wording, so Undo is the same edit back. → {id, text, previous} |
        None (no such fact of theirs, or no words)."""
        text = " ".join(str(text or "").split())
        if len(text) < 3 or len(text) > 500:
            return None
        with self._lock:
            row = self._db.execute("SELECT text FROM facts WHERE id=? AND person_id=?", (fact_id, person_id)).fetchone()
            if not row:
                return None
            self._db.execute("UPDATE facts SET text=?, confidence=1.0, ts=?, extractor='person' WHERE id=? AND person_id=?",
                             (text, time.time(), fact_id, person_id))
            self._db.commit()
            self._bm25 = None
        if MEMORY_HYBRID:
            self._store_vectors([text])
        return {"id": fact_id, "text": text, "previous": row["text"]}

    def set_pinned(self, fact_id: str, pinned: bool, *, person_id: str) -> bool:
        """Pin or unpin one of the person's facts (False: no such fact of theirs)."""
        with self._lock:
            cur = self._db.execute("UPDATE facts SET pinned=? WHERE id=? AND person_id=?",
                                   (1 if pinned else 0, fact_id, person_id))
            self._db.commit()
            return cur.rowcount > 0

    # What a forgotten fact keeps, so a restore puts back exactly what was there (its pin and its use, #1073 review)
    _KEEP = "id, conv_id, text, confidence, ts, grp, pinned, last_used, use_count"

    def _move_to_forgotten(self, row, person_id: str, batch: str | None) -> None:
        """Under self._lock."""
        self._db.execute("INSERT OR REPLACE INTO forgotten_facts (id, conv_id, text, confidence, ts, forgotten_at, person_id, "
                         "grp, batch, pinned, last_used, use_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (row["id"], row["conv_id"], row["text"], row["confidence"], row["ts"], time.time(), person_id,
                          row["grp"], batch, row["pinned"] or 0, row["last_used"], row["use_count"] or 0))
        self._db.execute("DELETE FROM facts WHERE id=?", (row["id"],))

    def _restore_row(self, row, person_id: str) -> None:
        """Under self._lock."""
        self._db.execute("INSERT OR REPLACE INTO facts (id, conv_id, text, confidence, ts, person_id, grp, pinned, last_used, "
                         "use_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (row["id"], row["conv_id"], row["text"], row["confidence"], row["ts"], person_id, row["grp"],
                          row["pinned"] or 0, row["last_used"], row["use_count"] or 0))
        self._db.execute("DELETE FROM forgotten_facts WHERE id=?", (row["id"],))

    def forget_fact(self, fact_id: str, *, person_id: str) -> bool:
        """Move one of the person's facts to forgotten_facts (restorable with restore_fact) and out
        of recall."""
        with self._lock:
            row = self._db.execute(f"SELECT {self._KEEP} FROM facts WHERE id=? AND person_id=?",
                                   (fact_id, person_id)).fetchone()
            if not row:
                return False
            self._move_to_forgotten(row, person_id, None)
            self._db.execute("DELETE FROM facts WHERE id=?", (fact_id,))
            self._db.commit()
            self._bm25 = None
            return True

    def restore_fact(self, fact_id: str, *, person_id: str) -> bool:
        with self._lock:
            row = self._db.execute(f"SELECT {self._KEEP} FROM forgotten_facts WHERE id=? AND person_id=?",
                                   (fact_id, person_id)).fetchone()
            if not row:
                return False
            self._restore_row(row, person_id)
            self._db.execute("DELETE FROM forgotten_facts WHERE id=?", (fact_id,))
            self._db.commit()
            self._bm25 = None
            return True

    def set_group(self, fact_id: str, group: str, *, person_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE facts SET grp=? WHERE id=? AND person_id=?", (group, fact_id, person_id))
            self._db.commit()
            return cur.rowcount > 0

    def groups(self, *, person_id: str) -> list[dict]:
        """The person's facts by group (#1073): [{group, count, examples (2), last_used}], the most recently used group
        first, then the biggest. Ungrouped facts are "other" until memory_groups assigns them."""
        rows = self._db.execute("SELECT COALESCE(grp, 'other') AS g, COUNT(*) AS n, MAX(last_used) AS lu FROM facts "
                                "WHERE person_id=? GROUP BY g", (person_id,)).fetchall()
        out = []
        for r in rows:
            ex = [x[0] for x in self._db.execute(
                "SELECT text FROM facts WHERE person_id=? AND COALESCE(grp, 'other')=? "
                "ORDER BY use_count DESC, ts DESC LIMIT 2", (person_id, r["g"]))]
            out.append({"group": r["g"], "count": r["n"], "examples": ex, "last_used": r["lu"]})
        out.sort(key=lambda g: (-(g["last_used"] or 0), -g["count"], g["group"]))
        return out

    def forget_batch(self, *, person_id: str, group: str | None = None, conv_id: str | None = None) -> dict:
        """Forget a whole group, or everything learned from one chat, as one batch (#1073): each fact moves to
        forgotten_facts like forget_fact, under one undo token. → {token, count}."""
        if bool(group) == bool(conv_id):
            raise ValueError("forget a group or a chat, one of them")
        token = "fb-" + uuid.uuid4().hex[:16]
        with self._lock:
            q, arg = (("COALESCE(grp, 'other')=?", group) if group else ("conv_id=?", conv_id))
            rows = self._db.execute(f"SELECT {self._KEEP} FROM facts WHERE person_id=? AND {q}",
                                    (person_id, arg)).fetchall()
            for row in rows:
                self._move_to_forgotten(row, person_id, token)
            self._db.commit()
            self._bm25 = None
        return {"token": token if rows else None, "count": len(rows)}

    def restore_batch(self, token: str, *, person_id: str) -> int:
        """Undo one bulk forget: every fact of that batch back, exactly as it was. Only the person's own batch."""
        with self._lock:
            rows = self._db.execute(f"SELECT {self._KEEP} FROM forgotten_facts WHERE batch=? AND person_id=?",
                                    (token, person_id)).fetchall()
            for row in rows:
                self._restore_row(row, person_id)
            self._db.commit()
            self._bm25 = None
            return len(rows)

    def total_fact_count(self, person_id: str | None = None) -> int:
        if person_id:
            return self._db.execute("SELECT COUNT(*) FROM facts WHERE person_id=?", (person_id,)).fetchone()[0]
        return self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]

    def maybe_extract_facts(self, conv_id: str, *, person_id: str, on_facts=None) -> None:
        """Call after each assistant turn. Triggers L1 extraction every N user turns; the facts
        belong to person_id. on_facts(added, corrected, user_text, evaluator), when given, hears what
        was stored (the world model turns facts with values into claims, #358)."""
        n = self.turn_count(conv_id)
        if n > 0 and n % L1_EXTRACT_EVERY_N == 0:
            self._extract_facts_l1(conv_id, person_id=person_id, on_facts=on_facts)
        # The L3 persona is Zach's (it's written from his facts only); refresh it exactly once per
        # milestone, not once per fact added.
        if person_id != OWNER:
            return
        total = self.total_fact_count(OWNER)
        if total >= self._last_persona_at + L3_PERSONA_EVERY_N_L1:
            self._refresh_persona()
            self._last_persona_at = total

    def _extract_facts_l1(self, conv_id: str, *, person_id: str, on_facts=None) -> None:
        """Ask the LLM to extract atomic facts from the last N messages.

        Only what the USER said is material. The assistant's replies restate tool results
        (tomorrow's calendar, the front window, a routine it just saved) and the local model
        turned those into "facts" that went stale by the next day, then outranked the tools
        ("you have a Wind down routine" after it was deleted). An assistant line is kept only
        as the question it asked, so a bare answer ("Spokane") still has its context."""
        msgs = self.get_messages(conv_id, limit=L1_EXTRACT_EVERY_N * 2)
        if not msgs:
            return
        lines = []
        for m in msgs:
            if m["role"] == "user":
                if str(m["content"]).startswith(CARD_PROMPT_MARK):
                    continue      # a card's button, built partly from someone else's words: never the person's facts
                lines.append(f"USER: {m['content']}")
            else:
                q = _asked(m["content"])
                if q:
                    lines.append(f"ASSISTANT (asked): {q}")
        if not any(l.startswith("USER:") for l in lines):
            return
        transcript = "\n".join(lines)
        user_lines = "\n".join(l for l in lines if l.startswith("USER:"))
        known = [(r["id"], r["text"]) for r in self._db.execute(
            "SELECT id, text FROM facts WHERE person_id=? ORDER BY ts DESC LIMIT 40", (person_id,))]
        known_block = "\n".join(f"[{i + 1}] {t}" for i, (_, t) in enumerate(known)) or "(none)"
        try:
            import household_identity as hh
            people = hh.get_store().people()
        except Exception:  # noqa: BLE001 - no store: no roster, the code checks still run on nothing
            people = []
        roster = roster_block(people)
        import local_time
        prompt = (
            "From what the USER said below, extract only facts about the user or their household that "
            "will still be true weeks from now: people and relationships, preferences, routines, places, how "
            "the household does things, standing decisions, and corrections the user made. One standalone "
            "sentence per line, written to the user in the second person (\"You pay for downtown parking with "
            "ParkMobile\", \"Your sister Hale lives in Boise\"), no numbering or bullets. Do NOT include: what "
            "the user asked for, questions, wishes or plans for this assistant, an app or any system being built, "
            "a single trip, errand, pickup or happening, calendar events, appointments or anything dated, what "
            "is on their screen, test or debugging messages, facts about software, servers or this assistant, or "
            "another adult's health, medication or private matters (who they are to the user is enough). "
            "A standing rule the user gives about how to treat something (\"Ava's time with her mom is a standing "
            "family event: never flag it as a conflict\") is kept, written as the rule itself. "
            "If the user corrected one of the known facts below, even briefly (\"No, red and baby blue\"), or it is "
            "no longer true, write \"REPLACES <number>: <the whole corrected fact>\" instead. "
            "If there is nothing like that, output exactly NONE.\n\n"
            # #1090: the local 8B restated the known facts block instead (70% of its lines on a fictional bank)
            "Never repeat, restate or paraphrase a known fact: the known facts are already stored. Write only what "
            "is new, or a correction. If nothing is new and nothing was corrected, output exactly NONE and nothing "
            "else.\n\n"
            f"{local_time.now_line()}\n" + (f"\n{roster}\n" if roster else "")
            + f"\nKnown facts:\n{known_block}\n\nConversation:\n{transcript}"
        )
        counts = {"raw_lines": 0, "echoes": 0, "none": 0, "filtered": 0, "dev_note": 0, "other_adult": 0,
                  "near_duplicate": 0, "same_words": 0,
                  "stored": 0, "replaced": 0, "blocked": {}, "kept_as_new": 0}
        known_texts = [t for _, t in known]
        t0 = time.time()
        try:
            raw = self._llm_call(prompt, max_tokens=512)
            have_rows = [(r["id"], r["text"]) for r in self._db.execute(
                "SELECT id, text FROM facts WHERE person_id=? ORDER BY ts DESC LIMIT 1000", (person_id,))]
            added: list[str] = []
            corrected: list[tuple[str, str]] = []
            for line in raw.splitlines():
                line = line.strip(" -•*")
                if not line:
                    continue
                counts["raw_lines"] += 1
                if line.upper() == "NONE":
                    counts["none"] += 1
                elif not line.upper().startswith("REPLACES") and (line in known_texts or _near_duplicate(line, known_texts)):
                    counts["echoes"] += 1
                replaces = None
                m = re.match(r"(?i)^REPLACES\s*\[?(\d+)\]?\s*:\s*(.+)$", line)
                if m:
                    n = int(m.group(1))
                    replaces = known[n - 1][0] if 1 <= n <= len(known) else None
                    line = m.group(2).strip()
                # A standing rule the person gave is the rule itself, not "the user said…" (#1613: "Kate with Mom is a
                # standing family event, don't check against it" was never kept). A wish about titan is never one.
                rule = None if product_wish(line) else standing_rule(line)
                if rule:
                    line, counts["rule"] = rule, counts.get("rule", 0) + 1
                if len(line) <= 12 or line.upper() == "NONE" or not _keep_extracted(line, user_lines):
                    counts["filtered"] += line.upper() != "NONE"
                    continue
                if contradicts_roster(line, people):
                    counts["filtered"] += 1
                    continue          # the roster's to say (10-02 audit)
                if product_wish(line):
                    if one_off(line):
                        counts["filtered"] += 1
                        continue
                    counts["dev_note"] += 1
                    self.add_dev_note(conv_id, line, person_id=person_id)
                    continue          # a wish about titan: the owner's dev notes, never their memory (Zach 10-10)
                if other_adults_private(line, people, person_id):
                    counts["other_adult"] += 1
                    continue          # another adult's health or private matter: never in someone else's memory
                line = second_person(line)
                known_old = dict(known).get(replaces) if replaces else None
                if replaces and not (_content_words(second_person(known_old)) & _content_words(line)):
                    replaces = None   # a REPLACES with nothing in common is a slip: store the new fact, forget nothing
                if replaces:
                    why = correction_problem(second_person(known_old), line, user_lines)
                    if why:
                        counts["blocked"][why] = counts["blocked"].get(why, 0) + 1
                    if why in ("unmentioned", "invented"):
                        continue      # a correction of something not said, or to a value not said: neither stored nor forgotten
                    if why in ("other-subject", "unrelated"):
                        counts["kept_as_new"] += 1
                        replaces = None   # about someone or something else: a new fact, the old one stays
                if not replaces:
                    # The model wrote the corrected fact without "REPLACES" (#1613: "No, red and baby blue" left the
                    # old balloon colours stored, the new line dropped as a near-duplicate of them)
                    sup = superseded(line, have_rows, user_lines)
                    if sup:
                        replaces, known_old = sup
                        counts["superseded"] = counts.get("superseded", 0) + 1
                if one_off(line) and not replaces and not rule:
                    counts["filtered"] += 1
                    continue          # a one-time thing (a person's correction of a stored fact stands; 10-02 audit)
                if replaces and " ".join(second_person(known_old).split()).casefold().strip(" .") == \
                        " ".join(line.split()).casefold().strip(" ."):
                    counts["same_words"] += 1
                    continue          # "replaced" by the same words: nothing changed (10-02: one Maple fact churned 6 times)
                carry = None
                if replaces:   # a correction: the old fact leaves recall (restorable), the new one is stored
                    carry = self._db.execute("SELECT grp, pinned FROM facts WHERE id=? AND person_id=?",
                                             (replaces, person_id)).fetchone()
                    self.forget_fact(replaces, person_id=person_id)
                    have_rows = [(i, t) for i, t in have_rows if i != replaces]
                    corrected.append((known_old, line))
                # Compared as the person reads them: an older "The user pays…" is the same fact as a new "You pay…"
                elif _near_duplicate(line, [second_person(t) for _, t in have_rows]):
                    counts["near_duplicate"] += 1
                    continue
                # Read out of chat by a model, not said to titan: memory's prior (world PRIORS "memory"), not 1.0
                old = self.add_fact(conv_id, line, confidence=EXTRACTED_CONFIDENCE, person_id=person_id,
                                    extractor=self.extractor())
                row = self._db.execute("SELECT id FROM facts WHERE person_id=? AND text=? ORDER BY ts DESC LIMIT 1",
                                       (person_id, line.strip())).fetchone()
                if row:
                    have_rows = [(row["id"], line.strip())] + [(i, t) for i, t in have_rows if i != row["id"]]
                    grp = "decision" if rule else (carry["grp"] if carry else None)
                    with self._lock:   # a correction keeps its row's place in Settings: its group and pin (one row per thing)
                        if grp:
                            self._db.execute("UPDATE facts SET grp=? WHERE id=?", (grp, row["id"]))
                        if carry and carry["pinned"]:
                            self._db.execute("UPDATE facts SET pinned=1 WHERE id=?", (row["id"],))
                        self._db.commit()
                if old and not replaces:
                    corrected.append((old, line))      # stored as an update of an older fact: a correction too
                elif not replaces:
                    added.append(line)
            counts["stored"], counts["replaced"] = len(added), len(corrected)
            _log_extraction({"ts": round(time.time(), 3), "person": person_id, "extractor": self.extractor(),
                             "ms": int((time.time() - t0) * 1000), "known": len(known),
                             "user_lines": user_lines.count("USER:"), **counts})
        except Exception as exc:
            print(f"[memory] L1 fact extraction failed: {exc}", file=sys.stderr)
            _log_extraction({"ts": round(time.time(), 3), "person": person_id, "error": type(exc).__name__,
                             "ms": int((time.time() - t0) * 1000)})
        try:   # #1073: every fact gets a group (roster first, then the local model); best effort, the rest wait
            import memory_groups
            if os.environ.get("MEMORY_GROUP_FACTS", "1").strip().lower() not in ("0", "off", "false", "no"):
                memory_groups.assign(self, person_id, people)
        except Exception as exc:  # noqa: BLE001
            print(f"[memory] grouping skipped: {type(exc).__name__}", file=sys.stderr)
            return
        if on_facts and (added or corrected):
            try:
                on_facts(added, corrected, user_lines, self.extractor())
            except Exception as exc:  # noqa: BLE001 - claims never break memory
                print(f"[memory] on_facts failed: {exc}", file=sys.stderr)

    def extractor(self) -> str:
        """Who reads facts out of chat, as evidence names its evaluator (#392): model:<name>. The model the last
        extraction actually used, once there's been one (#1090: Qwen3.6 or the fallback)."""
        if self._last_model:
            return "model:" + self._last_model
        if os.environ.get("GEMINI_API_KEY", "").strip() and \
                os.environ.get("MEMORY_EXTRACT_BRAIN", "local").strip().lower() == "gemini":
            return "model:" + os.environ.get("AGENT_GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
        return "model:" + os.environ.get("AGENT_LOCAL_MODEL_NAME", "qwen3-8b")

    # ── L2: scene blocks ─────────────────────────────────────────────────────

    def _write_scene(self, conv_id: str, facts: list[str]) -> None:
        if not facts:
            return
        scene_id = uuid.uuid4().hex[:8]
        title = facts[0][:60] if facts else "Unnamed scene"
        md_path = _REFS_DIR / f"scene-{scene_id}.md"
        import durable   # a power cut never leaves half a file (#407)
        durable.atomic_write(md_path, f"# Scene: {title}\n\n" + "\n".join(f"- {f}" for f in facts))
        with self._lock:
            self._db.execute(
                "INSERT INTO scenes (id, conv_id, title, md_path, ts) VALUES (?,?,?,?,?)",
                (scene_id, conv_id, title, str(md_path), time.time())
            )
            self._db.commit()

    def get_recent_scenes(self, limit: int = 3) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, title, md_path FROM scenes ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── L3: persona ──────────────────────────────────────────────────────────

    def _refresh_persona(self) -> None:
        facts = self.get_facts(limit=100, person_id=OWNER)
        if len(facts) < 5:
            return
        prompt = (
            "Restate these facts about the user as a short third-person profile (3-6 sentences). "
            "Say only what the facts say: no guesses, inferences, praise or advice. Never write "
            "\"likely\", \"suggests\", \"indicating\" or \"appears\".\n\n"
            "Facts:\n" + "\n".join(f"- {f}" for f in facts)
        )
        try:
            persona = self._llm_call(prompt, max_tokens=400)
            # Code, not the prompt: a sentence that guesses is dropped (10-01: "likely a parent").
            kept = [x for x in re.split(r"(?<=[.!?])\s+", persona.strip()) if x and not _GUESS.search(x)]
            import durable   # rewritten in place, a power cut would leave it empty (#407)
            durable.atomic_write(_PERSONA_MD, "# User Persona\n\n" + " ".join(kept) + "\n")
        except Exception as exc:
            print(f"[memory] L3 persona refresh failed: {exc}", file=sys.stderr)

    def get_persona(self) -> str:
        if _PERSONA_MD.exists():
            return _PERSONA_MD.read_text(encoding="utf-8").strip()
        return ""

    # ── Context compression (offload) ────────────────────────────────────────

    def offload(self, content: str, summary: str | None = None) -> str:
        """
        Store a large tool output to disk. Returns a node_id like '001-N1'.
        In the agent context, replace the full output with:
            [OFFLOADED node_id: <summary> — resolve with memory.resolve(node_id)]
        """
        if not summary:
            summary = content[:80].replace("\n", " ")
        fname = f"ref-{uuid.uuid4().hex[:8]}.md"
        fpath = _REFS_DIR / fname
        import durable
        durable.atomic_write(fpath, content)
        with self._lock:
            idx = len(self._offload_index) + 1
            node_id = f"{idx:03d}-N{idx}"
            entry = _OffloadEntry(node_id=node_id, result_ref=fname, summary=summary)
            self._offload_index.append(entry)
            self._db.execute(
                "INSERT OR IGNORE INTO offload_index (node_id, result_ref, summary, ts) VALUES (?,?,?,?)",
                (node_id, fname, summary, time.time())
            )
            self._db.commit()
        return node_id

    def resolve(self, node_id: str) -> str | None:
        """Retrieve full content of an offloaded tool result."""
        row = self._db.execute(
            "SELECT result_ref FROM offload_index WHERE node_id=?", (node_id,)
        ).fetchone()
        if not row:
            return None
        fpath = _REFS_DIR / row["result_ref"]
        return fpath.read_text(encoding="utf-8") if fpath.exists() else None

    def _load_offload_index(self) -> list[_OffloadEntry]:
        rows = self._db.execute(
            "SELECT node_id, result_ref, summary FROM offload_index ORDER BY ts"
        ).fetchall()
        return [_OffloadEntry(**dict(r)) for r in rows]

    # ── Semantic vectors (hybrid recall) ─────────────────────────────────────

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        """Batch-embed via the fleet's titan-embeddings service. None on any failure."""
        if not texts:
            return []
        import json as _json
        import urllib.error as _ue
        import urllib.request as _ur

        def post(batch: list[str]) -> list[list[float]] | None:
            body = _json.dumps({"input": batch, "model": AGENT_EMBED_MODEL}).encode()
            req = _ur.Request(AGENT_EMBED_URL, data=body, headers={"Content-Type": "application/json"})
            with _ur.urlopen(req, timeout=10) as resp:
                data = _json.loads(resp.read())
            vecs = [row["embedding"] for row in data["data"]]
            return vecs if len(vecs) == len(batch) else None

        # titan-embeddings (nomic-embed-text-v1.5) takes at most 2048 tokens per input and answers a
        # longer one with HTTP 500 (09-30: four "embed failed" fallbacks to BM25). Trim each input;
        # on a 500 anyway (dense text runs more tokens per character), trim harder once.
        try:
            return post([t[:EMBED_MAX_CHARS] for t in texts])
        except _ue.HTTPError as exc:
            if exc.code == 500:
                try:
                    return post([t[:EMBED_MAX_CHARS // 3] for t in texts])
                except Exception as exc2:  # noqa: BLE001
                    exc = exc2
            print(f"[memory] embed failed (degrading to BM25): {exc}", file=sys.stderr)
            return None
        except Exception as exc:  # network down, service down, shape drift
            print(f"[memory] embed failed (degrading to BM25): {exc}", file=sys.stderr)
            return None

    def _store_vectors(self, texts: list[str]) -> None:
        """Embed and persist vectors for texts that don't have a *current-version*
        one yet. A row tagged with a different (provider, model, dim) — e.g. left
        over from a prior embedding-model config — is treated the same as no row
        at all and gets re-embedded, rather than being silently trusted as if it
        were compatible (the Continue #906 failure: a stale index queried as
        valid after an embedding-provider swap)."""
        import array as _array
        missing = [t for t in texts
                   if not self._db.execute(
                       "SELECT 1 FROM fact_vectors WHERE text_hash=? AND provider=? AND model=?",
                       (self._hash(t), AGENT_EMBED_PROVIDER, AGENT_EMBED_MODEL)
                   ).fetchone()]
        if not missing:
            return
        vecs = self._embed(missing)
        if not vecs:
            return
        with self._lock:
            for t, v in zip(missing, vecs):
                blob = _array.array("f", v).tobytes()
                self._db.execute(
                    "INSERT OR REPLACE INTO fact_vectors (text_hash, vec, ts, provider, model, dim) "
                    "VALUES (?,?,?,?,?,?)",
                    (self._hash(t), blob, time.time(), AGENT_EMBED_PROVIDER, AGENT_EMBED_MODEL, len(v))
                )
            self._db.commit()

    def _vector_rank(self, query: str, facts: list[str]) -> dict[int, int] | None:
        """Return {fact_index: rank} by cosine similarity to the query, or None."""
        import array as _array
        qv = self._embed([query])
        if not qv:
            return None
        q = qv[0]
        qnorm = math.sqrt(sum(x * x for x in q)) or 1.0
        # Only rank against vectors from the currently-active embedding config —
        # a row left over from a different provider/model is never comparable
        # to a query embedded with the current one, regardless of raw dimension.
        rows = {r["text_hash"]: r["vec"] for r in self._db.execute(
            "SELECT text_hash, vec FROM fact_vectors WHERE provider=? AND model=? AND dim=?",
            (AGENT_EMBED_PROVIDER, AGENT_EMBED_MODEL, len(q))
        ).fetchall()}
        sims: list[tuple[int, float]] = []
        for i, f in enumerate(facts):
            blob = rows.get(self._hash(f))
            if blob is None:
                continue
            v = _array.array("f"); v.frombytes(blob)
            dot = sum(a * b for a, b in zip(q, v))
            vnorm = math.sqrt(sum(b * b for b in v)) or 1.0
            sims.append((i, dot / (qnorm * vnorm)))
        if not sims:
            return None
        sims.sort(key=lambda x: -x[1])
        return {idx: rank for rank, (idx, _s) in enumerate(sims)}

    # ── Recall ───────────────────────────────────────────────────────────────

    def recall(self, query: str, conv_id: str | None = None, *, person_id: str,
               include_health: bool = True) -> RecallResult:
        """
        Hybrid BM25 recall over the person's L1 facts → prependContext.
        L3 persona + L2 scene navigation (Zach's) → appendSystemContext.
        Nobody's recall ever includes another person's facts.
        """
        result = RecallResult()
        relevant: list[str] = []

        # L1: BM25 over the person's facts — rebuild the index only when invalidated by add_fact
        if query:
            # A local reference: a writer may set self._bm25 = None at any moment (add_fact, under
            # its lock). That orphans this dict instead of breaking it, and the next recall rebuilds.
            # The dict is published before the facts are read, so a build from facts older than a
            # write always lands in a dict that write has already orphaned.
            cache = self._bm25
            if cache is None:
                cache = {}
                self._bm25 = cache
            if person_id not in cache:
                facts = self.get_facts(limit=500, person_id=person_id)
                if facts:
                    cache[person_id] = (_BM25(facts), facts)
            index, facts_n = cache.get(person_id, (None, []))
            if index and facts_n:
                # Lexical ranking (BM25).
                bm_sorted = index.top_k(query, k=len(facts_n))
                bm_rank = {i: r for r, (i, score) in enumerate(bm_sorted) if score > 0.01}

                # Semantic ranking (vector cosine), fused via reciprocal rank fusion.
                vec_rank = None
                if MEMORY_HYBRID:
                    # Lazily catch up embeddings for facts missing a *current-version*
                    # vector — a row from a different (provider, model) counts as
                    # missing, so a config change re-embeds rather than trusting stale.
                    have = {r["text_hash"] for r in self._db.execute(
                        "SELECT text_hash FROM fact_vectors WHERE provider=? AND model=?",
                        (AGENT_EMBED_PROVIDER, AGENT_EMBED_MODEL)
                    ).fetchall()}
                    need = [f for f in facts_n if self._hash(f) not in have][:_BACKFILL_BUDGET]
                    if need:
                        self._store_vectors(need)
                    vec_rank = self._vector_rank(query, facts_n)

                if vec_rank is not None:
                    fused: dict[int, float] = {}
                    for i, r in bm_rank.items():
                        fused[i] = fused.get(i, 0.0) + 1.0 / (_RRF_K + r)
                    for i, r in vec_rank.items():
                        fused[i] = fused.get(i, 0.0) + 1.0 / (_RRF_K + r)
                    hit_idxs = set(sorted(fused, key=lambda i: -fused[i])[:MAX_RECALL_FACTS])
                else:
                    hit_idxs = set(list(bm_rank)[:MAX_RECALL_FACTS])
                # N-1/N+1: include the fact immediately before and after each
                # hit by insertion order — preserves narrative coherence that
                # pure relevance scoring would otherwise discard.
                n = len(facts_n)
                expanded: set[int] = set()
                for i in hit_idxs:
                    expanded.update(j for j in (i - 1, i, i + 1) if 0 <= j < n)
                relevant_raw = [facts_n[i] for i in sorted(expanded)]
                capped, total = [], 0
                for fact in relevant_raw:
                    if total + len(fact) > 2000:
                        break
                    capped.append(fact)
                    total += len(fact)
                relevant = capped
        # Pinned facts always ride along (the user pinned them in the mind view), ahead of the rest.
        pinned = [f for f in self.pinned_facts(person_id=person_id) if f not in relevant]
        relevant = pinned + relevant
        if not include_health:   # #1073: health facts only in the person's own chats, never a shared screen or the TV
            health = {r[0] for r in self._db.execute("SELECT text FROM facts WHERE person_id=? AND grp='health'",
                                                     (person_id,))}
            relevant = [f for f in relevant if f not in health]
        result.used = list(relevant)
        if relevant:
            noted = self._noted(person_id)
            result.prependContext = (
                "[MEMORY — relevant facts about this user, remembered from earlier conversations, written to them "
                "(\"you\" in a fact means the user, never you); they can be out of date: a tool result this turn "
                "(calendar, reminders, routines) is current and wins]\n"
                + "\n".join(f"• {f}" + (f" (noted {noted[f]})" if f in noted else "") for f in relevant)
                + "\n"
            )

        # L3 persona and L2 scenes are Zach's (written from his facts and conversations).
        if person_id != OWNER:
            return result
        persona = self.get_persona()
        system_parts: list[str] = []
        if persona:
            system_parts.append("[USER PERSONA]\n" + persona)

        # L2 scene navigation
        scenes = self.get_recent_scenes(limit=MAX_SCENE_LINES)
        if scenes:
            scene_lines = "\n".join(f"• {s['title']}" for s in scenes[:MAX_SCENE_LINES])
            system_parts.append("[RECENT CONTEXT SCENES]\n" + scene_lines)

        if system_parts:
            result.appendSystemContext = "\n\n".join(system_parts)

        return result

    # ── LLM shim (lazy import to avoid circular) ─────────────────────────────

    def _llm_call(self, prompt: str, max_tokens: int = 512) -> str:
        """The model for memory extraction: the LOCAL model by default — these are the most personal
        facts titan keeps, and a turn answered locally to keep household data home mustn't then be
        sent to a hosted model for extraction. MEMORY_EXTRACT_BRAIN=gemini opts in to Gemini."""
        import os as _os
        import json as _json
        import re as _re
        import urllib.request as _ur
        gemini_key = _os.environ.get("GEMINI_API_KEY", "").strip()
        if gemini_key and _os.environ.get("MEMORY_EXTRACT_BRAIN", "local").strip().lower() == "gemini":
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                + _os.environ.get("AGENT_GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
                + ":generateContent"
            )
            body = _json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
            }).encode()
            # The key in a header, never the URL (URLs land in proxy and error logs; 10-02 code audit).
            req = _ur.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key})
            with _ur.urlopen(req, timeout=20) as resp:
                data = _json.loads(resp.read())
            self._last_model = _os.environ.get("AGENT_GEMINI_LITE_MODEL", "gemini-3.5-flash-lite")
            return data["candidates"][0]["content"]["parts"][0]["text"]
        # Local: MEMORY_EXTRACT_URL (Qwen3.6 on :8198, #1090) first; titan's local brain when that's unreachable or
        # busy past MEMORY_EXTRACT_TIMEOUT. Both stay on titan.
        body = _json.dumps({
            "messages": [{"role": "user", "content": prompt + " /no_think"}],
            "max_tokens": max_tokens, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode()
        fallback = (_os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010").rstrip("/")
        tries = [(MEMORY_EXTRACT_URL, MEMORY_EXTRACT_TIMEOUT, MEMORY_EXTRACT_MODEL)] if MEMORY_EXTRACT_URL else []
        if fallback != MEMORY_EXTRACT_URL:
            tries.append((fallback, 60.0, _os.environ.get("AGENT_LOCAL_MODEL_NAME", "qwen3-8b")))
        last: Exception | None = None
        for url, timeout, model in tries:
            try:
                req = _ur.Request(url + "/v1/chat/completions", data=body, headers={"Content-Type": "application/json"})
                with _ur.urlopen(req, timeout=timeout) as resp:
                    data = _json.loads(resp.read())
                text = data["choices"][0]["message"].get("content") or ""
                self._last_model = model
                return _re.sub(r"<think>.*?</think>", "", text, flags=_re.S).strip()
            except Exception as exc:  # noqa: BLE001 - unreachable, busy past the timeout or a bad reply: the next one
                last = exc
        raise last or RuntimeError("no extraction model")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_instance: AgentMemory | None = None
_instance_lock = threading.Lock()


def get_memory() -> AgentMemory:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = AgentMemory()
    return _instance
