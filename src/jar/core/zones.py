#!/usr/bin/env python3
"""The knowledge base's zones (docs/kb-partitions.md, #833/#834; Zach's decisions 10-04): what an item is for, which
sets how long it's kept, who may ever see it, and when a question looks at it. Decided here, in code, from what the
item is; the model never assigns one.

  facts    durable household knowledge: explicit saves, household.yaml, shared notes, ingested documents
  sync     transient synced items: mail, calendar, scans (connector items)
  web      pages a person saved (ingest_url)
  system   titan's own knowledge: the seeded runbook and decisions (knowledge records)

`allowed(zone, role)` is checked before the audience check (kb_privacy.kb_visible): kids and shared household
devices see household facts only, never sync, web or system items, whatever an item's audience says (Zach, 10-04).
"""
from __future__ import annotations

from typing import Any

ZONES = ("facts", "sync", "web", "system")
_CONNECTOR_SOURCES = frozenset({"mail", "calendar", "scans", "downloads", "browsing", "daily", "contacts", "notes",
                                "reminders", "photos", "files"})

# role → the zones it may ever see. Titan itself (no person, no role) sees everything.
_ALLOWED: dict[str, frozenset[str]] = {
    "admin": frozenset(ZONES),
    # an adult: the audience decides; system items are the admins' unless the admin shares one (test_kb_privacy)
    "adult": frozenset(ZONES),
    "kid": frozenset({"facts"}),
    "household": frozenset({"facts"}),
    "engineering": frozenset({"system"}),
}


def zone_of(metadata: dict[str, Any] | None, source_id: str | None = None, source_type: str | None = None) -> str:
    """An item's zone from its type, metadata and id (query hits carry no source_type, so the id and metadata
    decide too). Never from a stamped `zone`: that's this function's own output, kept for the runtime's filter."""
    md = metadata or {}
    st = str(source_type or md.get("source_type") or "").lower()
    sid = str(source_id or "")
    if st == "knowledge_record" or str(md.get("origin") or "").startswith("kb_seed") \
            or str(md.get("domain") or "") in ("titan", "jellybean", "decisions"):
        return "system"
    if st.startswith("connector:") or sid.startswith("cx-") or str(md.get("source") or "").lower() in _CONNECTOR_SOURCES:
        return "sync"
    if st == "web" or sid.startswith("web-") or md.get("url"):
        return "web"
    return "facts"


def allowed(zone: str, role: str | None, person: str | None = None) -> bool:
    """Whether this role may ever see items of this zone (before audience and fences)."""
    if person is None and role is None:
        return True
    return zone in _ALLOWED.get(str(role or ""), frozenset({"facts"}))


# ── routing (§6): which zones a question searches, decided in code ────────────────────────────────

import os as _os
import re as _re

_SYSTEM_RX = _re.compile(r"\b(titan|jellybean|runtimes?|deploy\w*|servers?|gpus?|fleet|runbook|systemd|llama|gemini|qwen|"
                         r"decider|canary|evals?|coordinator|pgvector|postgres|world model)\b", _re.I)
_MAIL_RX = _re.compile(r"\b(e-?mails?|mail|inbox|mailbox|newsletters?|receipts?|invoices?|orders?|ordered|shipped|shipping|"
                       r"deliver(?:ed|y)|packages?|tracking|(?:payment|card) (?:failed|declined)|declined|wrote (?:me|to me)|"
                       r"(?:failed|bounced|missed) [^.?!]{0,40}?(?:payment|charge|autopay|bill)|"
                       r"sent me|reply|replied|message from|statement)\b", _re.I)
_WEB_RX = _re.compile(r"\b((?:threads?|posts?|pages?|links?|articles?|videos?|sites?) (?:that )?(?:i|we) (?:saved|bookmarked)|articles?|blog|(?:web ?)?pages? (?:i|we) saved|saved (?:page|link|article|post)|that (?:post|page|link)|"
                      r"links? (?:i|we) saved|bookmark\w*|web ?site i saved)\b", _re.I)
_CAL_RX = _re.compile(r"\b(when(?:'s| is| are| was| do| does| did)?|what time|schedules?|calendar|appointments?|meetings?|"
                      r"this (?:week|weekend|month)|next (?:week|month)|today|tonight|tomorrow|yesterday|"
                      r"(?:mon|tues|wednes|thurs|fri|satur|sun)days?|practice|lessons?|recital|games?|class(?:es)?|"
                      r"pick ?up|drop ?off|birthday|events?|holiday|conference)\b", _re.I)
_URL_RX = _re.compile(r"https?://|\bwww\.", _re.I)
_RULES = (# a pasted link first: the page may be one the person saved (10-04: a Reddit link they'd saved, dropped by the
          # plan; a GitHub link mentioning "runtime" went to titan's runbook)
          ("link", _URL_RX, (("web", 3), ("facts", 1))),
          ("system", _SYSTEM_RX, (("system", 3), ("facts", 1))),
          ("mail", _MAIL_RX, (("sync", 4), ("facts", 1))),
          ("web", _WEB_RX, (("web", 3), ("facts", 1))),
          ("calendar", _CAL_RX, (("sync", 3), ("facts", 2))))
_DEFAULT = (("facts", 3), ("sync", 2), ("web", 1))
# A search (not the per-turn context) also keeps one runbook passage in reach for whoever may see the system zone:
# ops questions without titan's words ("How do scheduled actions work?", "What runs on port 8198?") never reached the
# runbook (10-05: 14 curated runbook questions routed away; with this slot real-shaped cases 22 → 27 of 36, the system
# zone 30 → 35 of 45, retrieval_eval unchanged at 20/20). Budgets are caps: it shows only when it ranks.
_DEFAULT_SEARCH = _DEFAULT + (("system", 1),)
# A search whose question has titan's words and mail words both is about mail titan sent or got (#1225, 10-08:
# miss-cdb3d09f, a mail answer the system rule routed to the runbook alone). It searches both zones, runbook first;
# the per-turn context keeps the first match. Only for whoever may see the system zone (the system rule's own gate).
_SYSTEM_MAIL_SEARCH = (("system", 2), ("sync", 2), ("facts", 1))


def enabled() -> bool:
    return _os.environ.get("KB_ZONES", "") == "1"


def plan(question: str, role: str | None, person: str | None = "p", hint: str | None = None,
         about_titan: bool | None = None, search: bool = False) -> list[tuple[str, int]]:
    """Ordered (zone, passages) for a question, by rule (first match): titan itself (admins only), mail words, a
    saved page or a pasted link, when/schedule words, else facts, sync, then one saved page. Zones the role may never see are dropped. The model's
    `hint` can only narrow the plan to one of its zones, never add one."""
    q = str(question or "")
    steps: tuple[tuple[str, int], ...] = _DEFAULT_SEARCH if search else _DEFAULT
    for name, rx, st in _RULES:
        if name == "system":
            if (about_titan if about_titan is not None else bool(rx.search(q))) and allowed("system", role, person):
                steps = _SYSTEM_MAIL_SEARCH if search and _MAIL_RX.search(q) else st
                break
            continue
        if rx.search(q):
            steps = st
            break
    total = sum(n for _, n in steps)
    out = [(z, n) for z, n in steps if allowed(z, role, person)]
    if not out:
        out = [(z, total) for z in ZONES if allowed(z, role, person)][:1] or [("facts", total)]
    elif len(out) < len(steps):   # a zone the role can't see: its passages go to the first one left
        out[0] = (out[0][0], out[0][1] + total - sum(n for _, n in out))
    if hint and hint in {z for z, _ in out}:
        return [(hint, sum(n for _, n in out))]
    return out


def arrange(hits: list[dict], steps: list[tuple[str, int]], k: int, fallback: bool = True) -> list[dict]:
    """Ranked, already-visible hits kept to the plan's zones, in the runtime's rank order, each zone at most its
    budget, up to k: the plan says where to look, the ranking which passage answers (10-04: ordering by zone put a
    calendar event above the household note that answered "When is the kids' late start?"). With fallback (a search),
    budget a zone leaves unspent goes to the next-ranked hits of the planned zones, after the budgeted ones and up to
    the plan's total (10-07: 9 of 15 runbook misses were the answer ranked 2nd–8th behind other runbook passages while
    facts/sync/web held nothing, so the one system slot dropped it, #1225). Nothing in the planned zones: with fallback, the top k of everything
    visible (every allowed zone, once); without (the per-turn context), nothing."""
    cap = dict(steps)
    used = {z: 0 for z in cap}
    picked: list[int] = []
    zs = [zone_of(h.get("metadata"), str(h.get("source_id") or "")) for h in hits]
    for i, z in enumerate(zs):
        if z in cap and used[z] < cap[z]:
            picked.append(i)
            used[z] += 1
    if not picked and fallback:
        return hits[:k]
    if fallback:   # after the budgeted picks, never ahead of them: an extra note ranked above a calendar event
        spare = sum(cap.values()) - len(picked)   # pushed the answer out of the top 3 (calendar 13 → 12, 10-07)
        have = set(picked)
        for i, z in enumerate(zs):
            if spare <= 0:
                break
            if z in cap and i not in have:
                picked.append(i)
                spare -= 1
    return [hits[i] for i in picked][:k]
