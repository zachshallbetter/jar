#!/usr/bin/env python3
"""Bills, statements and receipts attach to account cycles (titan-node#1502, epic #1498 slice 3).

Built on the envelope (scan_envelope.py, #1500) and dedupe (scan_dedupe.py, #1501), and on the machinery bills already
have: the obligation threads (obligations.from_links: a bill's life, settled in code), the linker's edges (a receipt is
adopted only through an edge the linker offers: the same account, and a candidate or better), the keyed account hash
(hooks.keyed) and the household's day (local_time). No store of its own: the result is a value, and what the app keeps is
the pointer, the cycle's key and the derived fields.

  bill / statement   joins its account's obligation thread (the owner, the keyed account hash and the vendor) and its cycle:
                     the month its payment is due; a statement with no due date, the month it is dated (statement date,
                     date of service, else its first date).
  receipt            attaches to the open cycle it settles: the same owner and account, the same amount to the cent, a
                     linker edge (candidate or better) between the two, and the bill issued before it was paid. The cycle then
                     reads `resolved`; the receipt is part of the cycle and never a bill of its own. A receipt that finds no
                     open cycle is kept as an orphan cycle, settled, and is never waiting.
  waiting            a cycle is "waiting on you" while its thread is open (due, overdue_unconfirmed, partly_paid, failed,
                     contested) as of the HOUSEHOLD'S day (local_time.today(): AGENT_TZ, never titan's UTC day, #1567).
                     A paper + email pair is one cycle (the dedupe's kept copy stands for both).
  display            one row per account ("+N earlier" open cycles), its newest cycle in front; the account's history lists
                     every cycle, settled or open.

Privacy, absolute. A cycle belongs to its account's owner, the scanner (never a printed name, #1428). Threads are built one
owner at a time from that owner's scans alone, so nothing of another person's is in reach: two people's accounts at one
company are two accounts, and a receipt of one never settles the bill of the other, whatever the amount. An account hash
that two owners' scans both carry (a joint account) is two cycles, one per owner, never routed to the other holder (the
cycle's id carries the owner; its key never depends on whether another person holds the account, which would be a hint). Attaching never widens an audience: every cycle, row and member is [owner].

  python3 scripts/scans_score.py --pipeline scan_cycles:pipeline
  python3 scripts/scans_score.py --pipeline scan_cycles:pipeline --write scripts/evalsets/scans/cycles.json
"""
from __future__ import annotations

import hashlib
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_dedupe as D  # noqa: E402
import scan_envelope as E  # noqa: E402

VERSION = 1
OPEN = ("failed", "due", "overdue_unconfirmed", "partly_paid", "contested")
EDGE_STATES = ("verified", "accepted", "candidate")
CYCLE_TYPES = ("bill", "receipt", "statement")


def statement_date(text: str, fields: dict[str, Any]) -> str | None:
    """The date a statement is dated: its statement date, its date of service, else the first date in it."""
    if fields.get("issued"):
        return fields["issued"]
    got = E._date_after(text, r"date of service\s*:")
    if got:
        return got
    m = re.search(E._DATE_RX, text)
    return E._iso(m.group(1)) if m else None


def _month(iso: str | None) -> str:
    return (iso or "")[:7]


def _cycle_id(owner: str, account: str, key: str) -> str:
    return "cy-" + hashlib.sha1(f"{owner}\0{account}\0{key}".encode()).hexdigest()[:14]


def cycles(scans: list[dict], envs: dict[str, dict], dd: dict[str, Any], today: date | None = None,
           edges: list[Any] | None = None) -> dict[str, Any]:
    """Every bill, statement and receipt in `scans` attached to its account's cycle. `dd` is scan_dedupe.dedupe's result.
    `today` is the household's day (default local_time.today()). Needs the house salt the envelopes were made under."""
    import local_time
    import obligations as O
    today = today or local_time.today()
    by_id = {s["id"]: s for s in scans}
    fl = {sid: envs[sid]["fields"] for sid in by_id}
    owner = {sid: s["scanner"] for sid, s in by_id.items()}
    rep_of = dd["cluster"]
    members: dict[str, list[str]] = defaultdict(list)
    for sid in sorted(by_id):
        members[rep_of[sid]].append(sid)
    reps = [r for r in sorted(members) if fl[r]["type"] in CYCLE_TYPES and fl[r]["sender"]]

    # an account hash two owners both carry is a joint account: one cycle per owner (the owner is in the cycle's id). The key
    # is the same whether or not another person holds the account: a key that changed would tell its owner the other holder exists (#1508)

    if edges is None:
        edges = D.linker_edges(scans, envs)
    rec = {r for r in reps if fl[r]["type"] in ("bill", "receipt")}
    adoptable: dict[str, set[tuple[str, str]]] = defaultdict(set)      # owner -> edges inside that owner alone
    for e in edges:
        if e.state in EDGE_STATES and e.a in rec and e.b in rec and owner[e.a] == owner[e.b]:
            fa, fb = fl[e.a], fl[e.b]
            if fa["account"] and fa["account"] == fb["account"] and fa["amount"] is not None and fa["amount"] == fb["amount"] \
                    and {fa["type"], fb["type"]} == {"bill", "receipt"}:
                adoptable[owner[e.a]].add((e.a, e.b))

    found: dict[str, dict[str, Any]] = {}      # cycle id -> cycle

    def put(own: str, r: str, key: str, state: str | None, due: str | None, group: list[str], **extra: Any) -> None:
        f = fl[r]
        acct = f["account"] or ""
        cid = _cycle_id(own, acct or E._slug(f["sender"]), key)
        c = found.setdefault(cid, {"id": cid, "owner": own, "audience": [own], "vendor": f["sender"], "account": f["account"],
                                   "key": key, "due": None, "amount": None, "state": None, "waiting": False, "bills": [],
                                   "receipts": [], "statements": [], "members": [], "orphan": False})
        c["members"] = sorted(set(c["members"]) | {m for g in group for m in members[g]})
        c["due"] = c["due"] or due
        if state is not None:
            c["state"] = state
        for k, v in extra.items():
            if isinstance(v, list):
                c[k] = c[k] + [x for x in v if x not in c[k]]
            elif v is not None:
                c[k] = v

    for own in sorted({owner[r] for r in reps}):
        mine = [r for r in rec if owner[r] == own]
        records = []
        for r in sorted(mine):
            f, s = fl[r], by_id[r]
            records.append({"node_id": r, "node_type": f["type"], "data": {
                "doc_type": f["type"], "vendor": f["sender"], "amount": f["amount"], "due_date": f["due_date"],
                "paid_date": f["paid_date"], "mail_date": f["issued"] or str(s["scanned_at"])[:10],
                "provenance": {"owner": own, "audience": "private"}}})
        # one thread per bill (no linker groups); a receipt joins the cycle it pays through a linker edge, or none
        threads = O.from_links(records, today, {}, adoptable[own])
        seen: set[str] = set()
        for t in threads:
            refs = [p.ref for p in t.pieces if p.ref in by_id]
            bills = [x for x in refs if fl[x]["type"] == "bill"]
            rcpts = [x for x in refs if fl[x]["type"] == "receipt"]
            if not bills:
                continue
            head = bills[0]
            f = fl[head]
            when = f["due_date"] or (t.due.isoformat() if t.due else None) or t.cycle.isoformat()
            put(own, head, f"{E._slug(f['sender'])}:{_month(when)}", t.state, f["due_date"], bills + rcpts,
                bills=bills, receipts=rcpts, amount=t.amount)
            seen.update(refs)
        for r in sorted(mine):
            if r in seen or fl[r]["type"] != "receipt":
                continue
            f = fl[r]                      # a receipt with no open cycle to settle: kept, settled, never waiting
            put(own, r, f"{E._slug(f['sender'])}:{_month(f['paid_date'])}", "resolved", None, [r], receipts=[r],
                amount=f["amount"], orphan=True)
        for r in sorted(x for x in reps if owner[x] == own and fl[x]["type"] == "statement"):
            f = fl[r]
            d = statement_date(by_id[r]["text"], f)
            put(own, r, f"{E._slug(f['sender'])}:{_month(d)}", None, f["due_date"], [r], statements=[r])

    for c in found.values():
        c["waiting"] = c["state"] in OPEN
        c["settled"] = c["state"] == "resolved"
    cycle_of = {m: c["id"] for c in found.values() for m in c["members"]}
    return {"today": today.isoformat(), "cycles": found, "cycle_of": cycle_of, "accounts": accounts(found)}


def accounts(found: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The display: per owner, one row per account with its newest open cycle in front and "+N earlier" for the other open
    ones; the history lists every cycle, newest first. An account with nothing open has no row in Now (history only)."""
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in found.values():
        by[(c["owner"], c["account"] or E._slug(c["vendor"]))].append(c)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (own, acct), cs in sorted(by.items()):
        cs.sort(key=lambda c: (c["key"].split(":", 1)[1], c["id"]), reverse=True)
        opened = [c for c in cs if c["waiting"]]
        out[own].append({"owner": own, "audience": [own], "account": acct, "vendor": cs[0]["vendor"],
                         "now": opened[0]["id"] if opened else None, "earlier": max(len(opened) - 1, 0),
                         "history": [c["id"] for c in cs]})
    return dict(out)


def pipeline(scans: list[dict]) -> dict:
    """The scorer's pipeline: the dedupe's prediction with every bill, statement and receipt on its account's cycle. The
    batch's own day (scans_bench.AS_OF) is the household's day here, so the score is the same on any host."""
    return stage(scans, D.pipeline(scans))


def stage(scans: list[dict], pred: dict) -> dict:
    """Cycles as a stage: takes a prediction that already has the dedupe (pred["dedupe"]) and puts every bill, statement
    and receipt on its account's cycle. Touches only those documents."""
    import scans_bench as B
    envs = {sid: d["envelope"] for sid, d in pred["docs"].items()}
    with E.bench_salt():
        res = cycles(scans, envs, pred["dedupe"], date.fromisoformat(B.AS_OF))
    cyc = res["cycles"]
    for sid, d in pred["docs"].items():
        cid = res["cycle_of"].get(sid)
        if cid is None:
            if d["type"] in CYCLE_TYPES:                  # no sender to file it under: not a cycle, never waiting
                d["assoc"], d["waiting"] = {"kind": "none", "key": None}, False
            continue
        c = cyc[cid]
        d["assoc"] = {"kind": "bill_cycle", "key": c["key"]}
        d["waiting"] = c["waiting"]
    pred["cycles"] = res
    return pred
