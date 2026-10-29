#!/usr/bin/env python3
"""Filler trimming for saved pages (#868; Zach 10-04: the idea of Osso's sentence scoring, reimplemented here, no code
taken). A page someone saves (ingest_url, POST /v1/capture) arrives with its menus, cookie and subscribe banners, share
rows, footers and "read more" links; they go into the KB with the article and crowd its chunks. This scores each line
and keeps the ones that read like the page's content. Deterministic, no model.

  kept       prose: sentences with ordinary words, a heading followed by kept text, list items with substance
  dropped    boilerplate phrases (cookies, subscribe, sign in, share, follow us, all rights reserved, related posts…),
             navigation (runs of 1-3 word lines without sentence punctuation), lines repeated on the page,
             lone links and bare dates
  safety     never below MIN_KEEP of the text (a page that's mostly short lines stays whole) and never an empty result

`trim(text, title)` → (kept text, stats). Always on (KB_TRIM, on since 10-04, was removed: #945).
"""
from __future__ import annotations

import re
from collections import Counter

MIN_KEEP = 0.35
_BOILER = re.compile(
    r"(?i)\b(cookies?|accept all|privacy (policy|settings|choices)|terms (of (use|service)|and conditions)|"
    r"subscribe|newsletter|sign (up|in)|log ?in|create (an|your) account|follow us|share (this|on)|tweet|pin it|"
    r"all rights reserved|copyright|©|skip to (main )?content|read more|continue reading|related (posts|articles|stories)|"
    r"you (may|might) also like|advertisement|sponsored|powered by|back to top|load more|show more|comments? \(\d+\)|"
    r"open in app|download the app|get the app|join (now|free)|upvote|downvote|reply|permalink|report)\b")
_URL_ONLY = re.compile(r"^\s*(https?://\S+|www\.\S+)\s*$")
_DATE_ONLY = re.compile(r"(?i)^\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.? \d{1,2},? \d{4}|"
                        r"\d+ (min|minute|hour|day)s? (read|ago))\s*$")
_HEADING = re.compile(r"^\s*(#{1,6}\s+\S|[A-Z][^.!?]{2,80}$)")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]+")
_COMMON = frozenset("the a an and or but of to in on at for with is are was were be been it this that these those "
                    "as by from not you we they he she i our your their its can will would should could has have had "
                    "do does did so if than then there here when what which who how why".split())


def _score(line: str, repeats: int) -> float:
    s = line.strip()
    words = _WORD.findall(s)
    if not words:
        return -3.0
    if _URL_ONLY.match(s) or _DATE_ONLY.match(s):
        return -3.0
    score = 0.0
    if _BOILER.search(s) and len(words) < 25:
        score -= 3.0
    if repeats > 1:
        score -= 2.0
    if len(words) <= 3 and not re.search(r"[.!?:]$", s):
        score -= 1.5                      # a nav item, a button, a tag
    common = sum(w.lower() in _COMMON for w in words) / len(words)
    score += 2.0 * common                 # prose has function words; menus don't
    score += min(len(words), 30) / 15.0   # longer lines are likelier content
    if re.search(r"[.!?]\s*$", s):
        score += 0.5
    return score


def trim(text: str, title: str = "") -> tuple[str, dict]:
    lines = [l.rstrip() for l in str(text or "").splitlines()]
    counts = Counter(l.strip().lower() for l in lines if l.strip())
    scored = [(l, _score(l, counts[l.strip().lower()]) if l.strip() else None) for l in lines]
    keep = [sc is not None and sc >= 1.0 for _, sc in scored]
    # a heading stays when the next non-blank line is kept
    for i, (l, sc) in enumerate(scored):
        # (a repeated heading too: the table of contents repeats the section's own heading; only the one that leads
        # into kept prose stays)
        if sc is not None and not keep[i] and _HEADING.match(l) and not _BOILER.search(l):
            nxt = next((j for j in range(i + 1, len(scored)) if scored[j][1] is not None), None)
            keep[i] = nxt is not None and keep[nxt]
    out_lines, blank = [], False
    for (l, sc), k in zip(scored, keep):
        if sc is None:
            if out_lines and not blank:
                out_lines.append("")
                blank = True
            continue
        if k:
            out_lines.append(l)
            blank = False
    out = "\n".join(out_lines).strip()
    total = len(str(text or "").strip())
    stats = {"chars_in": total, "chars_out": len(out), "lines_in": sum(1 for _, sc in scored if sc is not None),
             "lines_out": sum(keep)}
    if not out or (total and len(out) / total < MIN_KEEP):
        stats.update(chars_out=total, lines_out=stats["lines_in"], kept_whole=True)
        return str(text or "").strip(), stats
    return out, stats
