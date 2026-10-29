#!/usr/bin/env python3
"""Dedupe and join for scans (titan-node#1501, epic #1498 slice 2): one row per thing, never a destructive delete.

Built on the envelope (scan_envelope.py, #1500). Four layers, each a rule over the envelope fields and the text, run
inside ONE OWNER at a time (a scan is its scanner's; two people's identical-looking letters are two rows, always):

  1. exact       the same text (whitespace folded) is the same scan twice.
  2. rescan      the same sender, type and account, every number in the text the same (dates, amounts, codes, reference
                 numbers: an OCR slip changes letters, never digits), and the text close once the slips are folded
                 (0.92 on difflib's ratio). The same-bill rule (mail_bills.same_bill_in_mail) is used as it is for a
                 bill, receipt or statement that read cleanly. Newsletters that differ only in a reference number stay two.
  3. multi-page  "Page k of N" pages of one sender and account are one document with N pages. When the same page number
                 turns up twice for the same sender and account (two sets of one letter, or a page scanned twice), the
                 pages can't be told apart and nothing is joined: they go to `review` as ambiguous, so a page is never
                 hidden under the wrong document.
  4. paper+email the same bill or letter arriving both ways: the linker (linker.py) scores the hooks (hooks.extract:
                 the same account, the same amount) and an accepted edge between one owner's paper and email copies, whose
                 sender, account, amount and due date also agree, joins them. One row, two sources. The linker's edges
                 across owners are discarded before anything is read from them.

Result, per scan: `cluster` (the row it belongs to, the scorer's dedupe group). Per cluster: the `kept` copy (an email over a
scan, then the copy with the fewest OCR slips: the one most like the others, then the earliest) and the `hidden` ones, each
with the layer that linked it. Hidden copies are links, not deletions: `undo(result, scan_id)` splits a copy back out, and
nothing here deletes, edits or re-owns a scan. Pages are kept as an ordered list with the cluster.

  python3 scripts/scans_score.py --pipeline scan_dedupe:pipeline
  python3 scripts/scans_score.py --pipeline scan_dedupe:pipeline --write scripts/evalsets/scans/dedupe.json
"""
from __future__ import annotations

import difflib
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_envelope as E  # noqa: E402

VERSION = 1
SIMILAR = 0.92
_PAGE = re.compile(r"(?im)^\s*page (\d+) of (\d+)\s*$")
_NUMBERS = re.compile(r"\d+")
BILLISH = ("bill", "receipt", "statement")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _digits(text: str) -> tuple[str, ...]:
    """Every number in the text, in order: OCR slips change letters, never these."""
    return tuple(_NUMBERS.findall(text))


def _page(text: str) -> tuple[int, int] | None:
    m = _PAGE.search(text)
    if not m:
        return None
    k, n = int(m.group(1)), int(m.group(2))
    return (k, n) if 1 <= k <= n else None


def _dates(text: str) -> frozenset[str]:
    out = set()
    for m in re.finditer(E._DATE_RX, text):
        d = E._iso(m.group(1))
        if d:
            out.add(d)
    return frozenset(out)


class _Union:
    def __init__(self, ids: list[str]):
        self.p = {i: i for i in ids}
        self.why: dict[str, str] = {}      # a member -> the layer of the first link that touched it

    def find(self, a: str) -> str:
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def join(self, a: str, b: str, layer: str, order: dict[str, int]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.why.setdefault(a, layer)
        self.why.setdefault(b, layer)
        keep, drop = (ra, rb) if order[ra] <= order[rb] else (rb, ra)
        self.p[drop] = keep


def _similar(a: str, b: str) -> bool:
    fa, fb = E._fold(_norm(a)), E._fold(_norm(b))
    if abs(len(fa) - len(fb)) > 0.1 * max(len(fa), len(fb), 1):
        return False
    return difflib.SequenceMatcher(None, fa, fb, autojunk=False).ratio() >= SIMILAR


def linker_edges(scans: list[dict], envs: dict[str, dict]) -> list[Any]:
    """The linker's edges between scans, of every state, from hooks built in memory (no links.db is read or written).
    Candidates only: the caller keeps those inside one owner whose fields agree."""
    import hooks
    import linker
    items: dict[str, Any] = {}
    for s in scans:
        f = envs[s["id"]]["fields"]
        at = f["due_date"] or f["paid_date"] or f["issued"] or str(s["scanned_at"])[:10]
        it = linker.Item(s["id"], "scans", linker._d(at), s["scanner"], s["scanner"])
        for h in hooks.extract({"text": s["text"], "title": s.get("title") or "", "at": at}):
            it.keys[h.kind].add(h.key)
        items[s["id"]] = it
    return linker.score(items)


def _linker_pairs(scans: list[dict], envs: dict[str, dict]) -> list[tuple[str, str, list[str]]]:
    """The accepted or verified edges, as (a, b, why)."""
    return [(e.a, e.b, e.typed) for e in linker_edges(scans, envs) if e.state in ("verified", "accepted")]


def dedupe(scans: list[dict], envs: dict[str, dict]) -> dict[str, Any]:
    by_id = {s["id"]: s for s in scans}
    order = {s["id"]: i for i, s in enumerate(sorted(scans, key=lambda x: (x["scanned_at"], x["id"])))}
    uf = _Union(sorted(by_id, key=order.get))
    owner = {sid: s["scanner"] for sid, s in by_id.items()}
    fl = {sid: envs[sid]["fields"] for sid in by_id}
    paged = {sid: _page(s["text"]) for sid, s in by_id.items()}
    review: list[dict] = []

    def same_owner(a: str, b: str) -> bool:
        return owner[a] == owner[b]

    # 1. exact: the same text, the same owner, not a numbered page
    seen: dict[tuple[str, str], str] = {}
    for sid in sorted(by_id, key=order.get):
        if paged[sid]:
            continue
        key = (owner[sid], hashlib.sha256(_norm(by_id[sid]["text"]).encode()).hexdigest())
        if key in seen:
            uf.join(seen[key], sid, "exact", order)
        else:
            seen[key] = sid

    # 2. rescan: same owner, type, sender, account, every number; close text once OCR slips are folded
    buckets: dict[tuple, list[str]] = defaultdict(list)
    for sid in sorted(by_id, key=order.get):
        if paged[sid]:
            continue
        f = fl[sid]
        buckets[(owner[sid], f["type"], E._fold(f["sender"] or ""), f["account"], _digits(by_id[sid]["text"]))].append(sid)
    for members in buckets.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if uf.find(a) != uf.find(b) and _similar(by_id[a]["text"], by_id[b]["text"]):
                    uf.join(a, b, "rescan", order)
    import mail_bills as mb
    filed: list[dict] = []
    for sid in sorted(by_id, key=order.get):
        f = fl[sid]
        if f["type"] not in BILLISH or paged[sid] or not f["sender"]:
            continue
        data = {"vendor": f["sender"], "amount": f["amount"], "due_date": f["due_date"], "paid_date": f["paid_date"],
                "doc_type": f["type"], "provenance": {"owner": owner[sid]}}
        twin = mb.same_bill_in_mail(data, filed, owner[sid])
        if twin and by_id[twin]["source"] == by_id[sid]["source"] and fl[twin]["account"] == f["account"]:
            uf.join(twin, sid, "rescan", order)
        filed.append({"node_id": sid, "data": data})

    # 3. multi-page: one owner, sender and account; page numbers that do not repeat
    groups: dict[tuple, list[str]] = defaultdict(list)
    for sid, pg in paged.items():
        f = fl[sid]
        if pg and f["account"] and f["sender"]:
            groups[(owner[sid], E._fold(f["sender"]), f["account"], pg[1])].append(sid)
    for key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        parts = [members]
        if len({paged[m][0] for m in members}) < len(members):          # a page number twice: try the dates on the pages
            split: dict[frozenset, list[str]] = defaultdict(list)
            for m in members:
                split[_dates(by_id[m]["text"])].append(m)
            parts = [p for p in split.values() if len(p) > 1] if all(
                len({paged[m][0] for m in p}) == len(p) for p in split.values()) else []
            if not parts:
                review.append({"kind": "ambiguous_pages", "owner": key[0], "members": sorted(members, key=order.get),
                               "why": "the same page number twice for one sender and account"})
                continue
        for p in parts:
            p = sorted(p, key=lambda m: paged[m][0])
            for m in p[1:]:
                uf.join(p[0], m, "multipage", order)

    # 4. paper + email: the linker's edge, inside one owner, with the fields agreeing and the sources differing
    for a, b, typed in _linker_pairs(scans, envs):
        if not same_owner(a, b) or by_id[a]["source"] == by_id[b]["source"]:
            continue
        fa, fb = fl[a], fl[b]
        if (fa["type"] in BILLISH and fa["type"] == fb["type"] and fa["amount"] is not None
                and (fa["sender"], fa["account"], fa["amount"], fa["due_date"]) == (fb["sender"], fb["account"], fb["amount"], fb["due_date"])
                and fa["due_date"] and fa["account"]):
            uf.join(a, b, "paper_email", order)

    clusters: dict[str, list[str]] = defaultdict(list)
    for sid in by_id:
        clusters[uf.find(sid)].append(sid)
    out: dict[str, Any] = {"cluster": {sid: root for root, m in clusters.items() for sid in m}, "clusters": {},
                           "review": review}
    for root, members in sorted(clusters.items()):
        if len(members) < 2:
            continue
        members.sort(key=order.get)
        kept = _best(members, by_id)
        for m in members:                     # a cluster is named by the copy shown for it
            out["cluster"][m] = kept
        root = kept
        pages = sorted(members, key=lambda m: (paged[m] or (0, 0))[0]) if any(paged[m] for m in members) else None
        out["clusters"][root] = {
            "owner": owner[members[0]], "kept": kept, "pages": pages,
            "sources": sorted({by_id[m]["source"] for m in members}),
            "hidden": [{"scan": m, "layer": uf.why.get(m, "linked")} for m in members if m != kept and not (pages and paged[m])],
            "count": len(members)}
    return out


def _best(members: list[str], by_id: dict[str, dict]) -> str:
    """The copy to show: an email (clean text), then the one most like the others (fewest slips), then the earliest."""
    toks = Counter(t for m in members for t in set(_norm(by_id[m]["text"]).split()))

    def rank(m: str):
        words = set(_norm(by_id[m]["text"]).split())
        return (by_id[m]["source"] != "email", -sum(toks[w] for w in words) / (len(words) or 1), members.index(m))
    return min(members, key=rank)


def undo(result: dict[str, Any], scan_id: str) -> dict[str, Any]:
    """Split one hidden copy back out as a row of its own. Returns a new result; the scan itself was never touched."""
    cl = dict(result["cluster"])
    root = cl[scan_id]
    if root == scan_id:
        return result
    cl[scan_id] = scan_id
    clusters = {k: dict(v) for k, v in result["clusters"].items()}
    c = clusters[root]
    c["hidden"] = [h for h in c["hidden"] if h["scan"] != scan_id]
    c["count"] -= 1
    if c.get("pages"):
        c["pages"] = [p for p in c["pages"] if p != scan_id]
    if c["count"] < 2:
        del clusters[root]
    return {**result, "cluster": cl, "clusters": clusters}


def pipeline(scans: list[dict]) -> dict:
    """The scorer's pipeline: the envelope's prediction with its dedupe replaced by the four layers above."""
    return stage(scans, E.pipeline(scans))


def stage(scans: list[dict], pred: dict) -> dict:
    """Dedupe as a stage: takes the envelope's prediction (scan_envelope.pipeline) and returns it with the clusters and
    the views replaced. scan_pipeline chains this after the envelope."""
    envs = {sid: d["envelope"] for sid, d in pred["docs"].items()}
    with E.bench_salt():
        res = dedupe(scans, envs)
    for sid, d in pred["docs"].items():
        d["cluster"] = res["cluster"][sid]
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid, d in pred["docs"].items():
        groups[(d["owner"], d["cluster"])].append(sid)
    pred["views"] = {}
    for (own, _), members in sorted(groups.items()):
        pred["views"].setdefault(own, []).append({"members": sorted(members), "count": len(members)})
    pred["dedupe"] = res
    return pred
