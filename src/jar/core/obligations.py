#!/usr/bin/env python3
"""Obligation threads (titan-node#951, slice 1): a bill's life across its mails, settled in code.

10-04: "Is anything waiting on me?" listed September notices as current, and an overdue notice read as "the bill",
with nothing linking a failure to the payment after it. Here a thread is one obligation: an account (the vendor,
normalised) and a cycle (the date its first notice is about), with an optional thread_ref (#955: a mail server's
thread id, slice 2). Its evidence is the records titan already files from the mail itself (mail_bills: bills and
receipts, extracted from the original before its envelope; never a summary, #954) and the person's own statements.

Settled in code, never by a model:
  - a failure or a due bill, then a same-account receipt for the amount (within a cent or 10%, or up to 25% more:
    a late fee) → resolved; a smaller receipt → partly_paid;
  - a person's "paid" / "fixed" outranks the notices on or before it; a notice after it reopens the thread; a person's
    "not paid" against a matching receipt → contested (one question, never a guess);
  - a due date passed with nothing after it → overdue_unconfirmed, said as "no confirmation seen", never "overdue":
    the confirmation may simply not have reached titan (10-04's utility);
  - a failure older than STALE_DAYS with nothing after it → stale: listed as old, not as waiting.

  threads(records, today, statements=()) → [Thread]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable

STALE_DAYS = 14
JOIN_DAYS = 45          # a retry or a receipt belongs to a thread opened within this many days before it
OPEN = ("failed", "due", "overdue_unconfirmed", "partly_paid", "contested")
SAID = {"failed": "payment failed", "due": "due", "overdue_unconfirmed": "no confirmation seen",
        "partly_paid": "partly paid", "contested": "unclear: the records and what you said disagree",
        "not_mine": "not yours to pay, you said",
        "resolved": "paid", "stale": "old, nothing since"}
# Brands and the names their mail comes under, whole normalised names (slice 2 learns more from sender domains).
ALIASES = {"comcast": "xfinity", "amzn": "amazon", "amazon marketplace": "amazon"}
_SUFFIX = re.compile(r"(?i)\b(?:inc|llc|ltd|pbc|corp|co|company|corporation|plc|gmbh)\b\.?|\.com\b")


def account_of(vendor: Any) -> str:
    """The vendor's whole name, lowercased, suffixes dropped ("Vercel Inc." → "vercel", "Amazon.com" → "amazon"),
    with the alias map. Never just its first word: "American Express" and "American Water" are two companies (#960
    review), and with amount tolerances one's receipt would pay the other's bill."""
    words = re.findall(r"[a-z0-9]+", _SUFFIX.sub(" ", str(vendor or "")).lower())
    key = " ".join(w for w in words if w not in ("the", "your"))
    return ALIASES.get(key, key)


def _prefix(a: str, b: str) -> bool:
    """Whether a's words begin b's ("providence" begins "providence washington montana")."""
    wa, wb = a.split(), b.split()
    return 0 < len(wa) <= len(wb) and wb[:len(wa)] == wa


def merge_accounts(names: Iterable[str]) -> dict[str, str]:
    """name → the account it belongs to. A name joins a shorter one whose words begin it, but only when that shorter
    name begins a single family: "Providence" joins "Providence Washington Montana"; "Apple" next to both "Apple Card"
    and "Apple Services" stays apart; a shared first word alone ("City of Spokane" / "City of Tacoma") never merges."""
    names = sorted(set(n for n in names if n), key=lambda n: (len(n.split()), n))
    out: dict[str, str] = {}
    for n in names:
        target = n
        for r in (m for m in names if m != n and _prefix(m, n)):   # shortest first
            nxt = {m.split()[len(r.split())] for m in names if m != r and _prefix(r, m)}
            if len(nxt) == 1:
                target = out.get(r, r)
                break
        out[n] = target
    return out


def same_account(a: str, b: str) -> bool:
    return a == b or _prefix(a, b) or _prefix(b, a)


def _d(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v or "")[:10])
    except ValueError:
        return None


def _money(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


@dataclass
class Piece:
    when: date
    kind: str                 # failed | due | paid | said_paid | said_unpaid
    amount: float | None
    ref: str                  # a record id, or "you said"
    owner: str | None = None
    audience: str | None = None
    due: date | None = None
    adopted: bool = False      # the bill lens took it from the thread's candidate fringe (#951)
    about: str | None = None   # handled_by: the first name of the household member the speaker named (#951 step 3)
    via: str | None = None     # "text": evidence the owner's own device derived from a text (#951 step 4)


@dataclass
class Thread:
    account: str
    vendor: str
    cycle: date
    amount: float | None
    pieces: list[Piece] = field(default_factory=list)
    thread_ref: str | None = None
    state: str = "due"
    due: date | None = None
    owner: str | None = None
    audience: str | None = None
    handled_by: str | None = None   # the first name of who the person said is handling it (it stays open)

    @property
    def key(self) -> tuple[str, str, str | None]:
        return self.account, self.cycle.isoformat(), self.thread_ref

    def chain(self) -> str:
        words = {"failed": "failed", "due": "billed", "paid": "paid", "said_paid": "{who} it's done",
                 "said_unpaid": "{who} it isn't", "said_disputed": "{who} it's disputed",
                 "said_not_mine": "{who} it isn't {whose}", "said_handled": "{who} {about} handling it"}
        return " → ".join(f"{words[p.kind].format(who=p.ref if p.kind.startswith('said') else '', about=_handles(p.about), whose='yours' if p.ref.startswith('you ') else 'theirs')} {p.when:%b %-d}" + (f" ${p.amount:,.2f}" if p.amount and p.kind == "paid"
                                                                    and p.amount != self.amount else "")
                          + ((" (taken as a payment of this bill)" if p.kind == "paid" else " (taken as a retry)")
                             if p.adopted else "") + (" (a text)" if p.via == "text" else "")
                          for p in self.pieces)


def _handles(who: str | None) -> str:
    return "you're" if who == "you" else f"{who or 'someone'} is"


def _pieces(records: Iterable[dict[str, Any]]) -> list[tuple[str, str, Piece]]:
    out = []
    for n in records:
        d = n.get("data") or {}
        kind_t = str(d.get("doc_type") or n.get("node_type") or "")
        prov = d.get("provenance") or {}
        vendor = str(d.get("vendor") or "")
        acct = account_of(vendor)
        if not acct or kind_t not in ("bill", "receipt"):
            continue
        status = str(d.get("payment_status") or "").lower()
        if kind_t == "receipt":
            kind, when = "paid", _d(d.get("paid_date")) or _d(d.get("mail_date"))
        elif status == "failed":
            kind, when = "failed", _d(d.get("paid_date")) or _d(d.get("mail_date")) or _d(d.get("due_date"))
        else:
            kind, when = "due", _d(d.get("mail_date")) or _d(d.get("due_date"))
        if when is None:
            continue
        out.append((acct, vendor, Piece(when, kind, _money(d.get("amount")), str(n.get("node_id") or ""),
                                        prov.get("owner"), prov.get("audience"), _d(d.get("due_date")))))
    return out


_SAID_KIND = {"paid": "said_paid", "not_paid": "said_unpaid", "disputed": "said_disputed", "not_mine": "said_not_mine",
              "handled_by": "said_handled"}


def _said_piece(st: dict[str, Any], when: date) -> Piece:
    """A person's word as a piece of the thread: {kind | paid, who, handled_by (a first name, already resolved)}."""
    kind = st.get("kind") or ("paid" if st.get("paid") else "not_paid")
    return Piece(when, _SAID_KIND.get(kind, "said_unpaid"), None, f"{st.get('who') or 'you'} said",
                 about=st.get("handled_by") if kind == "handled_by" else None)


def _signal_pieces(signals: Iterable[dict[str, Any]]) -> list[tuple[str, Piece]]:
    """The viewer's own text-derived evidence (obligation_signals.of: {account, date, kind paid | failed, amount}) as
    (account, piece): weighed like a receipt or a failure notice, private to the viewer whose device sent it."""
    out = []
    for sg in signals:
        a, when = account_of(sg.get("account")), _d(sg.get("date"))
        if a and when and sg.get("kind") in ("paid", "failed"):
            out.append((a, Piece(when, str(sg["kind"]), _money(sg.get("amount")), "a text", audience="private",
                                 via="text")))
    return out


def _attach_signals(out: list[Thread], signals: Iterable[dict[str, Any]]) -> None:
    """Each text piece joins the latest thread of its account that began on or before it, within JOIN_DAYS (a text
    never opens a thread: a confirmation with no bill isn't an obligation)."""
    for a, p in _signal_pieces(signals):
        t = next((t for t in sorted(out, key=lambda t: t.cycle, reverse=True)
                  if same_account(a, t.account) and 0 <= (p.when - t.cycle).days <= JOIN_DAYS), None)
        if t is not None:
            t.pieces.append(p)
            t.pieces.sort(key=lambda q: (q.when, q.kind == "paid"))


def _matches(bill: float | None, paid: float | None) -> str | None:
    """resolved | partly_paid | None, for a receipt against the thread's amount."""
    if bill is None or paid is None:
        return "resolved" if paid is not None or bill is None else None
    if abs(paid - bill) <= max(0.01, 0.10 * bill) or bill < paid <= bill * 1.25:
        return "resolved"
    if 0 < paid < bill:
        return "partly_paid"
    return None


def from_links(records: Iterable[dict[str, Any]], today: date, groups: dict[str, int],
               adoptable: set[tuple[str, str]], statements: Iterable[dict[str, Any]] = (),
               signals: Iterable[dict[str, Any]] = ()) -> list[Thread]:
    """The bill lens over the linker's threads (#951): records in one thread are one obligation, so "what's waiting
    on me" and `threads` agree. groups: record id → its thread (the viewer's threads); a record in none settles alone,
    unless the lens adopts it from a thread's candidates (adoptable: candidate edges as (a, b)): a same-account
    receipt within JOIN_DAYS after the cycle that pays it in full or in part, or a same-amount retry. Adopted pieces
    say so in the chain; nothing outside a candidate edge is ever taken."""
    pieces = {p.ref: (acct, vendor, p) for acct, vendor, p in _pieces(records)}
    by_group: dict[Any, list[str]] = {}
    for ref in pieces:
        by_group.setdefault(groups.get(ref, f"solo:{ref}"), []).append(ref)
    out: list[Thread] = []
    built: dict[Any, Thread] = {}
    for g, refs in by_group.items():
        refs.sort(key=lambda r: (pieces[r][2].when, pieces[r][2].kind == "paid"))
        acct, vendor, first = next((pieces[r] for r in refs if pieces[r][2].kind != "paid"), pieces[refs[0]])
        t = Thread(acct, vendor, first.when, first.amount, owner=first.owner, audience=first.audience)
        for r in refs:
            t.pieces.append(pieces[r][2])
            t.due = pieces[r][2].due or t.due
        built[g] = t
    adj: dict[str, set[str]] = {}
    for a, b in adoptable:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    group_of = {r: g for g, refs in by_group.items() for r in refs}
    # a later group joins an earlier one of the same account through a candidate edge, when all of it is payments that
    # pay it (in full or part) or all of it is same-amount retries; whole threads can join (a retry's own mail+record)
    for g in sorted(built, key=lambda g: built[g].cycle):
        t = built.get(g)
        if t is None:
            continue
        kinds = {p.kind for p in t.pieces}
        hosts = []
        for p in t.pieces:
            for other in adj.get(p.ref, ()):
                h = group_of.get(other)
                if h is not None and h != g and h in built and built[h].cycle <= t.cycle:
                    hosts.append(h)
        for h in sorted(set(hosts), key=lambda h: built[h].cycle, reverse=True):
            host = built[h]
            if not same_account(t.account, host.account) or not 0 <= (t.cycle - host.cycle).days <= JOIN_DAYS:
                continue
            if kinds == {"paid"}:
                fits = _matches(host.amount, sum(p.amount or 0 for p in t.pieces)) in ("resolved", "partly_paid")
            elif "paid" not in kinds:
                fits = host.amount is not None and all(p.amount is not None and abs(p.amount - host.amount) < 0.01
                                                       for p in t.pieces) and not any(q.kind == "paid" for q in host.pieces)
            else:
                fits = False
            if fits:
                for p in t.pieces:
                    p.adopted = True
                    host.pieces.append(p)
                host.pieces.sort(key=lambda q: (q.when, q.kind == "paid"))
                host.due = next((q.due for q in reversed(host.pieces) if q.due), host.due)
                for r, gg in list(group_of.items()):
                    if gg == g:
                        group_of[r] = h
                del built[g]
                break
    # a receipt with no bill isn't an obligation (it's in `threads`, not in "what's waiting")
    built = {g: t for g, t in built.items() if any(p.kind != "paid" for p in t.pieces)}
    out = list(built.values())
    said: dict[str, list[Piece]] = {}
    for st in statements:
        a = account_of(st.get("account") or st.get("vendor"))
        when = _d(st.get("date"))
        if a and when:
            said.setdefault(a, []).append(_said_piece(st, when))
    for a, ps in said.items():
        for p in ps:
            t = next((t for t in sorted(out, key=lambda t: t.cycle, reverse=True)
                      if same_account(a, t.account) and t.cycle <= p.when), None)
            if t is not None:
                t.pieces.append(p)
                t.pieces.sort(key=lambda q: q.when)
    _attach_signals(out, signals)
    for t in out:
        t.state = _settle(t, today)
    return sorted(out, key=lambda t: (t.state not in OPEN, t.cycle))


def threads(records: Iterable[dict[str, Any]], today: date,
            statements: Iterable[dict[str, Any]] = (), signals: Iterable[dict[str, Any]] = ()) -> list[Thread]:
    """records: bill and receipt nodes ({node_id, node_type, data}); statements: the person's own dated words,
    {account or vendor, date, paid: bool}. → every thread, settled. The vendor-grouped form: open_obligations uses
    from_links (the linker's threads) when the link index is there, and this when it isn't."""
    pieces = _pieces(records)
    merged = merge_accounts(a for a, _, _ in pieces)
    by: dict[str, list[tuple[str, Piece]]] = {}
    for acct, vendor, p in pieces:
        by.setdefault(merged.get(acct, acct), []).append((vendor, p))
    said: dict[str, list[Piece]] = {}
    for s in statements:
        acct = account_of(s.get("account") or s.get("vendor"))
        fits = [a for a in by if same_account(acct, a)]
        acct = fits[0] if len(fits) == 1 else acct   # "Lakeside" said of "Lakeside Clinic"; ambiguous stays apart
        when = _d(s.get("date"))
        if acct and when:
            said.setdefault(acct, []).append(_said_piece(s, when))
    out: list[Thread] = []
    for acct, items in by.items():
        items.sort(key=lambda x: (x[1].when, x[1].kind == "paid"))   # a same-day receipt after the notice it pays
        mine: list[Thread] = []
        for vendor, p in items:
            if p.kind in ("failed", "due"):
                # a retry or a new notice for the same amount within JOIN_DAYS joins the open thread
                t = next((t for t in reversed(mine) if (p.when - t.cycle).days <= JOIN_DAYS
                          and (t.amount is None or p.amount is None or abs((p.amount or 0) - (t.amount or 0)) < 0.01)
                          and not any(q.kind == "paid" for q in t.pieces)), None)
                if t is None:
                    t = Thread(acct, vendor, p.when, p.amount, owner=p.owner, audience=p.audience)
                    mine.append(t)
                t.pieces.append(p)
                t.due = p.due or t.due
            else:   # a receipt: the latest thread before it that it pays
                t = next((t for t in reversed(mine) if 0 <= (p.when - t.cycle).days <= JOIN_DAYS
                          and _matches(t.amount, p.amount)), None)
                if t is not None:
                    t.pieces.append(p)
        for p in said.get(acct, []):
            t = next((t for t in reversed(mine) if t.cycle <= p.when), None)
            if t is not None:
                t.pieces.append(p)
                t.pieces.sort(key=lambda q: q.when)
        _attach_signals(mine, signals)
        for t in mine:
            t.state = _settle(t, today)
        out += mine
    return sorted(out, key=lambda t: (t.state not in OPEN, t.cycle), reverse=False)


def _settle(t: Thread, today: date) -> str:
    said = [p for p in t.pieces if p.kind.startswith("said_")]
    latest = max(said, key=lambda p: p.when) if said else None
    t.handled_by = latest.about if latest and latest.kind == "said_handled" else None
    if latest and latest.kind == "said_not_mine":
        if latest.ref.startswith("you "):
            return "not_mine"    # the viewer's own word: not theirs to deal with
        # another adult's shared word that it isn't THEIRS says nothing about whether it's this viewer's: a note on
        # the chain, and the bill keeps the state the records and the other words give it
        said = [p for p in said if p is not latest]
        latest = max(said, key=lambda p: p.when) if said else None
    if latest and latest.kind == "said_disputed":
        return "contested"       # the person disputes it: one question, never a guess
    paid = [p for p in t.pieces if p.kind == "paid"]
    said_paid = [p for p in t.pieces if p.kind == "said_paid"]
    said_unpaid = [p for p in t.pieces if p.kind == "said_unpaid"]
    notices = [p for p in t.pieces if p.kind in ("failed", "due")]
    last_notice = max(notices, key=lambda p: p.when) if notices else None
    if said_unpaid and paid and max(said_unpaid, key=lambda p: p.when).when >= max(paid, key=lambda p: p.when).when:
        return "contested"   # a receipt says paid, the person says not: ask, don't guess
    if paid:
        got = sum(p.amount or 0 for p in paid)
        state = _matches(t.amount, got if t.amount is not None else (paid[-1].amount))
        if last_notice is None or last_notice.when <= max(p.when for p in paid):
            return state or "resolved"
    if said_paid and (last_notice is None or last_notice.when <= max(p.when for p in said_paid)):
        return "resolved"    # the person's word outranks the notices before it; a notice after it reopens
    if said_unpaid:
        pass                 # the person says it's open: the notices' own state stands
    if last_notice is None:
        return "resolved"
    if last_notice.kind == "failed":
        return "stale" if (today - last_notice.when).days > STALE_DAYS else "failed"
    if t.due and t.due < today:
        return "overdue_unconfirmed"
    return "due"


def line(t: Thread) -> str:
    """One thread as a sentence-sized line: vendor, amount, state in plain words, and its chain."""
    amt = f" ${t.amount:,.2f}" if t.amount is not None else ""
    due = f", due {t.due:%b %-d}" if t.due and t.state in ("due", "overdue_unconfirmed") else ""
    by = f", {_handles(t.handled_by)} handling it" if t.handled_by and t.state in OPEN else ""
    return f"{t.vendor}{amt}: {SAID[t.state]}{due}{by} ({t.chain()})"
