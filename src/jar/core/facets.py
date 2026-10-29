#!/usr/bin/env python3
"""Facet chips and held axes for the threads tool (titan-node#951, rules 7 and 9).

Rule 9: free text becomes deterministic chips that narrow the set; no model guesses the filter.
  party  a vendor as filed, a sender's domain, or an organisation the world model knows (through the asker's aliases)
  time   a month ("September", "Sep 2026"), "last month", "this month", "this week", "last week", "today",
         "yesterday", or a date
  kind   bills, receipts, payments → records; mail, email → mail; texts, messages → texts; scans → scans
A question's chips intersect; words that make no chip are left for the title match.

Rule 7: hold one axis of what was found and vary the rest, for discovery without a similarity loop:
  hold=party  the same party, at other times ("same vendor, other months")
  hold=time   the same weeks, other parties
  hold=kind   the same kind of item, other parties and times
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable

_MONTHS = {m: i for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"), 1)}
_MON3 = {k[:3]: v for k, v in _MONTHS.items()} | {"sept": 9}
_KINDS = {"record": r"\b(?:bills?|receipts?|payments?|invoices?|statements?)\b", "mail": r"\b(?:mails?|e-?mails?|inbox)\b",
          "text": r"\b(?:texts?|messages?|sms|imessages?)\b", "scan": r"\bscans?\b"}
AXES = ("party", "time", "kind")


@dataclass
class Chip:
    axis: str            # party | time | kind
    label: str           # what the person reads ("Harbor Gas", "Sep 2026", "texts")
    match: Callable[[Any], bool] = field(repr=False, default=lambda it: True)


def _month_span(y: int, m: int) -> tuple[date, date]:
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return start, end


def time_chip(text: str, today: date) -> Chip | None:
    q = text.lower()
    span = None
    label = ""
    if re.search(r"\blast month\b", q):
        first = today.replace(day=1) - timedelta(days=1)
        span, label = _month_span(first.year, first.month), f"{first:%b %Y}"
    elif re.search(r"\bthis month\b", q):
        span, label = _month_span(today.year, today.month), f"{today:%b %Y}"
    elif re.search(r"\bthis week\b", q):
        s = today - timedelta(days=today.weekday())
        span, label = (s, s + timedelta(days=6)), f"week of {s:%b %-d}"
    elif re.search(r"\blast week\b", q):
        s = today - timedelta(days=today.weekday() + 7)
        span, label = (s, s + timedelta(days=6)), f"week of {s:%b %-d}"
    elif re.search(r"\btoday\b", q):
        span, label = (today, today), "today"
    elif re.search(r"\byesterday\b", q):
        d = today - timedelta(days=1)
        span, label = (d, d), "yesterday"
    else:
        m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", q)
        if m:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            span, label = (d, d), f"{d:%b %-d, %Y}"
        else:
            m = re.search(r"\b(" + "|".join(sorted(list(_MONTHS) + list(_MON3), key=len, reverse=True)) +
                          r")\b\.?(?:\s+(20\d\d))?", q)
            if m:
                mon = _MONTHS.get(m.group(1)) or _MON3[m.group(1)]
                y = int(m.group(2)) if m.group(2) else (today.year if mon <= today.month else today.year - 1)
                span, label = _month_span(y, mon), f"{date(y, mon, 1):%b %Y}"
    if span is None:
        return None
    lo, hi = span
    return Chip("time", label, lambda it, lo=lo, hi=hi: it.at is not None and lo <= it.at <= hi)


def kind_chips(text: str) -> list[Chip]:
    q = text.lower()
    out = []
    for src, rx in _KINDS.items():
        if re.search(rx, q):
            label = {"record": "bills and receipts", "mail": "mail", "text": "texts", "scan": "scans"}[src]
            out.append(Chip("kind", label, lambda it, src=src: it.source == src))
    if len(out) > 1:   # "texts and mail": either
        srcs = {c.label for c in out}
        return [Chip("kind", " or ".join(sorted(srcs)), lambda it, cs=out: any(c.match(it) for c in cs))]
    return out


def party_chips(text: str, vendors: dict[str, str], domains: Iterable[str], entity_ids: Iterable[str],
                entity_names: dict[str, str]) -> list[Chip]:
    """Vendors as filed whose words are all in the question; domains whose name is; the organisations the world model
    found in it (the caller passes world.find's ids). Several parties: any of them."""
    q = " " + re.sub(r"[^a-z0-9]+", " ", text.lower()) + " "
    joined = q.replace(" ", "")
    found: list[tuple[str, Callable[[Any], bool]]] = []
    for key, name in sorted(vendors.items(), key=lambda kv: -len(kv[0])):
        ws = key.split()
        if ws and all(f" {w} " in q for w in ws):
            found.append((name, lambda it, k=key: k in it.keys.get("party:vendor", ())))
    for e in entity_ids:
        name = entity_names.get(e) or e
        if not any(name.lower() == n.lower() for n, _ in found):
            found.append((name, lambda it, e=e: e in it.keys.get("party:entity", ())))
    for d in sorted(set(domains)):   # a domain only when no vendor or organisation already names it
        label = d.split(".")[0]
        if len(label) >= 4 and (f" {label} " in q or label in joined) and not any(label in n.lower().replace(" ", "")
                                                                                  for n, _ in found):
            found.append((d, lambda it, d=d: d in it.keys.get("party:domain", ())))
    if not found:
        return []
    if len(found) == 1:
        return [Chip("party", found[0][0], found[0][1])]
    return [Chip("party", " or ".join(n for n, _ in found), lambda it, fs=found: any(f(it) for _, f in fs))]


def chips(text: str, today: date, vendors: dict[str, str], domains: Iterable[str], entity_ids: Iterable[str],
          entity_names: dict[str, str]) -> list[Chip]:
    out = party_chips(text, vendors, domains, entity_ids, entity_names) + kind_chips(text)
    t = time_chip(text, today)
    return out + ([t] if t else [])


def narrow(items: dict[str, Any], visible_ids: Iterable[str], cs: list[Chip]) -> set[str]:
    return {i for i in visible_ids if all(c.match(items[i]) for c in cs)}


def hold(axis: str, seeds: set[str], items: dict[str, Any], visible_ids: Iterable[str]) -> tuple[str, set[str]]:
    """Keep one axis of the seeds and vary the rest. → (what was held, in words; the other items that share it)."""
    vis = set(visible_ids) - seeds
    if axis == "party":
        keys = {(k, v) for i in seeds for k in ("party:vendor", "party:domain", "party:entity")
                for v in items[i].keys.get(k, ())}
        return "the same party", {i for i in vis if any(v in items[i].keys.get(k, ()) for k, v in keys)}
    if axis == "time":
        days = [items[i].at for i in seeds if items[i].at]
        if not days:
            return "the same time", set()
        lo, hi = min(days) - timedelta(days=3), max(days) + timedelta(days=3)
        return f"{lo:%b %-d}–{hi:%b %-d}", {i for i in vis if items[i].at and lo <= items[i].at <= hi}
    if axis == "kind":
        kinds = {items[i].source for i in seeds}
        return "the same kind", {i for i in vis if items[i].source in kinds}
    raise ValueError(f"hold is one of {', '.join(AXES)}")
