#!/usr/bin/env python3
"""Bulk review of scanned mail: review@1, the server-driven screen where titan's proposed connections are approved by group
(titan-node#1507, epic #1498 slice 8).

With a thousand scans one question a day does not work, so the pipeline's proposals (scan_pipeline, after the privacy stage) are
grouped by kind, and one tap approves a group:

  "37 go to <sender>"              a bill, statement or receipt that sits on that sender's account cycle      files a cycle
  "12 look like junk"              the junk stage's archive plan                                              files an archive plan
  "3 look like appointments: add to calendar?" (and deadlines)   a notice with a day                          files an event plan
  "5 are Tax 2025" (a policy, a warranty)   a kept bundle                                                     files bundle membership
  "4 are one conversation with <sender>"   a claim or case thread                                             files a link

Each viewer sees only their own groups. Everything is read through scan_privacy.view_for, and then only the viewer's own
documents (the scanner is the owner, #1428); a count is a count of those, so no number says what someone else scanned. A shared
screen gets nothing. A kid sees their own proposals with every operation unavailable (`kids.propose_only`).

Approving files in place: a row in review_store naming the scan and the link, cycle, bundle, event plan or archive plan. It never
deletes and never copies a body. A hard-key match (the bill's account AND amount equal its cycle's) files itself for an adult,
with its own undo, like a verified link; medical and legal mail never does. Every change returns a token for the existing
POST /v1/surfaces/undo, which puts back exactly what that change did. An automatic filing that is taken back is not made again.

Nothing here reads a live scan: the batch is the pipeline's output on the benchmark (`SCAN_REVIEW_BENCH=1` for the route; off by
default, so the route serves the empty review until a real source is wired).
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_store as RS  # noqa: E402
import scan_notices as N  # noqa: E402
import scan_pipeline as P  # noqa: E402
import scan_privacy as PV  # noqa: E402
import surfaces as SF  # noqa: E402

RESOLVER = "review@1"
CAN_FILE = ("admin", "adult")            # a kid proposes: a parent files
BENCH_BATCH = "bench-1499"
PENDING_STATES = (None, RS.MANUAL)


@dataclass
class Batch:
    """The pipeline's output for one batch of scans, and the household it ran for."""
    id: str
    scans: list[dict[str, Any]]
    pred: dict[str, Any]
    people: dict[str, dict[str, Any]] = field(default_factory=lambda: N.BENCH_PEOPLE)
    shares: dict[str, list[dict[str, Any]]] | None = None      # {scan id: [{with: [people], item: bool}]}, as the privacy stage takes them


_BATCHES: dict[str, Batch] = {}


def register(batch: Batch) -> None:
    _BATCHES[batch.id] = batch


def get_batch(batch_id: str) -> Batch | None:
    """A registered batch, or the benchmark's (only when SCAN_REVIEW_BENCH is exactly "1"). No live source yet."""
    if batch_id in _BATCHES:
        return _BATCHES[batch_id]
    if batch_id == BENCH_BATCH and os.environ.get("SCAN_REVIEW_BENCH", "") == "1":
        import scans_bench as B
        scans = B.generate(1499)["scans"]
        register(Batch(BENCH_BATCH, scans, P.pipeline(scans)))
        return _BATCHES[batch_id]
    return None


def viewer_in(batch: Batch, name: str) -> str | None:
    """Which of the batch's people a household member is: by first name, the way the benchmark names them. None: nobody."""
    first = str(name or "").strip().split(" ")[0].lower()
    return first if first in batch.people else None


# ── the proposals ────────────────────────────────────────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")


def _money(x: Any) -> str | None:
    return f"${float(x):,.2f}" if isinstance(x, (int, float)) else None


def proposals(batch: Batch, viewer: str) -> list[dict[str, Any]]:
    """Every connection titan proposes for this viewer's own documents, one per document (a dedupe cluster is one document):
    {item, gid, kind, gkey, gname, title, subtitle, meta, trailing, count, hard, sensitive, filing}. Read through view_for,
    then only the viewer's own; a document with any scan the viewer may not see is not here."""
    pred, scans = batch.pred, {s["id"]: s for s in batch.scans}
    mine = PV.view_for(batch.scans, pred, viewer, people=batch.people, shares=batch.shares)
    vdocs = mine.get("docs") or {}
    clusters: dict[str, list[str]] = defaultdict(list)
    for sid, d in (pred.get("docs") or {}).items():
        if d.get("cluster") and not d.get("held"):
            clusters[d["cluster"]].append(sid)
    cy = mine.get("cycles") or {}
    cycles, cycle_of = cy.get("cycles") or {}, cy.get("cycle_of") or {}
    th = mine.get("threads") or {}
    threads, thread_of = {t["id"]: t for t in th.get("threads") or []}, th.get("of") or {}
    bundles = {b["key"]: b for b in mine.get("bundles") or [] if b.get("owner") == viewer and b.get("state", "proposed") == "proposed"}
    notices = {n["scan"]: n for n in mine.get("notices") or [] if n.get("owner") == viewer}
    out: list[dict[str, Any]] = []
    for cl, sids in sorted(clusters.items()):
        sids = sorted(sids)
        if not all(s in vdocs and vdocs[s].get("owner") == viewer for s in sids):
            continue                                   # not wholly the viewer's own: nothing of it is shown, not even a count
        rep = cl if cl in vdocs else sids[0]
        d = vdocs[rep]
        assoc = d.get("assoc") or {}
        kind = assoc.get("kind")
        sender = str(d.get("sender") or "").strip() or None
        p: dict[str, Any] = {"item": cl, "count": len(sids), "title": sender or "Mail", "subtitle": str(d.get("type") or "").replace("_", " ").capitalize() or None,
                             "meta": d.get("due_date"), "trailing": _money(d.get("amount")), "hard": False,
                             "sensitive": bool(PV.classify(scans[rep])), "gkey": "", "gname": sender or "Mail"}
        if kind == "junk":
            p |= {"kind": "junk", "filing": {"kind": "archive", "key": cl, "plan": {"do": "archive", "undo": "unarchive"}}}
        elif kind == "bill_cycle":
            cid = next((cycle_of[s] for s in [rep] + sids if s in cycle_of), None)
            c = cycles.get(cid)
            if not c or c.get("owner") != viewer:
                continue
            vendor = str(c.get("vendor") or sender or "Mail")
            p |= {"kind": "vendor", "gkey": _slug(vendor), "gname": vendor, "filing": {"kind": "cycle", "key": cid, "plan": {"vendor": _slug(vendor)}},
                  "hard": bool(d.get("account") and d.get("amount") is not None and c.get("account") == d.get("account")
                               and c.get("amount") is not None and abs(float(c["amount"]) - float(d["amount"])) < 0.005)}
        elif kind == "event":
            n = next((notices[s] for s in sids if s in notices), None)
            if not n or n.get("event") or n.get("kind") not in N.NOTICE_TYPES:
                continue                               # already on the calendar: nothing to ask
            p |= {"kind": n["kind"], "meta": n["day"], "filing": {"kind": "event", "key": n["key"], "plan": {"day": n["day"], "add": "calendar"}}}
        elif kind == "bundle":
            b = bundles.get(assoc.get("key"))
            if not b:
                continue
            what, _, _ = str(b["key"]).partition(":")
            name = f"{sender} {what}" if what in ("policy", "warranty") and sender else str(b.get("name") or b["key"])
            p |= {"kind": "bundle", "gkey": _slug(b["key"]), "gname": name, "filing": {"kind": "bundle", "key": b["key"], "plan": {"bundle": b["id"]}}}
        elif kind == "thread":
            tid = next((thread_of[s] for s in [rep] + sids if s in thread_of), None)
            t = threads.get(tid)
            if not t or t.get("owner") != viewer:
                continue
            p |= {"kind": "thread", "gkey": tid, "gname": str(t.get("sender") or sender or "Mail"), "meta": d.get("date") or p["meta"],
                  "filing": {"kind": "link", "key": tid, "plan": {"thread": tid}}}
        else:
            continue
        p["gid"] = "g-" + p["kind"] + (f"-{p['gkey']}" if p["gkey"] else "")
        out.append(p)
    return out


def _state(rows: dict[str, dict[str, Any]], item: str) -> str | None:
    return (rows.get(item) or {}).get("state")


# ── words ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _words(kind: str, n: int, name: str) -> tuple[str, str]:
    """(label key, English) for a group of n: the count is part of the heading."""
    one = n == 1
    return {"vendor": ("review.to_vendor", f"{n} {'goes' if one else 'go'} to {name}"),
            "junk": ("review.junk", f"{n} {'looks' if one else 'look'} like junk"),
            "appointment": ("review.appointments", f"{n} {'looks like an appointment' if one else 'look like appointments'}: add to calendar?"),
            "deadline": ("review.deadlines", f"{n} {'looks like a deadline' if one else 'look like deadlines'}: add to calendar?"),
            "bundle": ("review.bundle", f"{n} {'is' if one else 'are'} {name}"),
            "thread": ("review.thread", f"{n} {'is' if one else 'are'} one conversation with {name}")}[kind]


# ── the surface ──────────────────────────────────────────────────────────────────────────────────────────────────

def review(batch: Batch | None, viewer: str | None, role: str, person: str | None, now: float, *,
           db: Any = None) -> dict[str, Any]:
    """review@1 as this viewer sees it. `viewer` is who they are in the batch (None: nobody in it), `person` whose decisions
    these are (their household id). Groups, one per kind of proposal, biggest first; a group the viewer has fully handled is
    gone. Filed for you: what filed itself in the last 7 days, each with its undo. Left for you: the exceptions."""
    if role not in ("admin", "adult", "kid") or not person:
        raise SF.Refused(403, "the review is a person's page")
    may = role in CAN_FILE
    exposure, reason = ("available", None) if may else ("unavailable", "kids.propose_only")
    props = proposals(batch, viewer) if batch and viewer else []
    rows = RS.rows(batch.id, person, db) if batch else {}
    groups: dict[str, dict[str, Any]] = {}
    auto, left, filed = [], [], 0
    for p in props:
        st = _state(rows, p["item"])
        if st in PENDING_STATES:
            groups.setdefault(p["gid"], {"p": p, "items": []})["items"].append(p)
        elif st == RS.EXCLUDED:
            left.append(p)
        elif st == RS.AUTO and float(rows[p["item"]]["at"]) >= now - SF.UNDO_TTL_S:
            auto.append(p)
        else:
            filed += 1
    ops: list[dict[str, Any]] = []
    deps: list[tuple[str, str]] = []

    def member(p: dict[str, Any], oids: list[str], rule: str, state: str) -> dict[str, Any]:
        slots: dict[str, Any] = {"title": p["title"], "actions": oids}
        if p["subtitle"]:
            slots["subtitle"] = p["subtitle"]
        meta = " · ".join(x for x in (p["meta"], f"{p['count']} scans" if p["count"] > 1 else None) if x)
        if meta:
            slots["meta"] = meta
        if p["trailing"]:
            slots["trailing"] = {"value": p["trailing"]}
        deps.append((p["item"], f"{state}:{p['gid']}:{p['count']}"))
        return SF.member("scan", p["item"], rule, ["scan pipeline", "viewer's own scans"], "derived", 2, slots)

    out_groups: list[dict[str, Any]] = []
    for gid, g in sorted(groups.items(), key=lambda kv: (-len(kv[1]["items"]), kv[0])):
        p0, items = g["p"], sorted(g["items"], key=lambda x: x["item"])
        key, text = _words(p0["kind"], len(items), p0["gname"])
        aid = f"op-approve-{gid}"
        ops.append(SF.operation(aid, "file", "group", gid, "jellybean.review.approve@1", "POST", "/v1/surfaces/review/approve",
                                params={"batch": batch.id, "group": gid}, exposure=exposure, reason=reason, recovery=["undo"],
                                variant="primary"))
        mem = []
        for p in items:
            eid = f"op-exclude-{p['item']}"
            ops.append(SF.operation(eid, "dismiss", "scan", p["item"], "jellybean.review.exclude@1", "POST", "/v1/surfaces/review/exclude",
                                    params={"batch": batch.id, "group": gid, "item": p["item"]}, exposure=exposure, reason=reason,
                                    recovery=["undo"]))
            mem.append(member(p, [eid], f"review.{p['kind']}", "pending"))
        out_groups.append({"id": gid, "role": "primary", "label": SF.label(key, text, count=len(items), name=p0["gname"]),
                           "members": mem, "ordering": "item", "operations": [aid]})
    if auto:
        mem = []
        for p in sorted(auto, key=lambda x: x["item"]):
            oid = f"op-undo-{p['item']}"
            ops.append(SF.operation(oid, "undo", "scan", p["item"], "jellybean.surface.undo@1", "POST", "/v1/surfaces/undo",
                                    params={"token": rows[p["item"]]["token"]}, recovery=["undo"]))
            mem.append(member(p, [oid], "review.hard_key", "auto"))
        out_groups.append({"id": "g-filed-for-you", "role": "supporting", "label": SF.label("review.filed_for_you", count=len(mem)),
                           "members": mem, "ordering": "item"})
    if left:
        mem = []
        for p in sorted(left, key=lambda x: x["item"]):
            oid = f"op-file-{p['item']}"
            ops.append(SF.operation(oid, "file", "scan", p["item"], "jellybean.review.approve@1", "POST", "/v1/surfaces/review/approve",
                                    params={"batch": batch.id, "group": p["gid"], "items": [p["item"]]}, exposure=exposure,
                                    reason=reason, recovery=["undo"]))
            mem.append(member(p, [oid], "review.left_out", "excluded"))
        out_groups.append({"id": "g-left-for-you", "role": "supporting", "label": SF.label("review.left_out", count=len(mem)),
                           "members": mem, "ordering": "item"})
    return SF.envelope(resolver=RESOLVER, profile="collection", purpose="edit", viewer=person, role=role,
                       rule="scan_privacy.view_for, then the viewer's own documents", groups=out_groups, operations=ops,
                       deps=deps, inputs=["scan pipeline", "review decisions", "viewer"], now=now,
                       lens={"batch": batch.id if batch else None},
                       excluded=[{"rule": "review.filed", "count": filed}])


# ── the operations ───────────────────────────────────────────────────────────────────────────────────────────────

def _require(batch: Batch | None, viewer: str | None, role: str, person: str | None) -> list[dict[str, Any]]:
    if role not in CAN_FILE or not person:
        raise SF.Refused(403, "a parent files; a kid proposes")
    if batch is None or not viewer:
        raise SF.Refused(404, "no such batch")
    return proposals(batch, viewer)


def approve(batch: Batch | None, viewer: str | None, role: str, person: str | None, group: str, items: list[str] | None,
            now: float, *, db: Any = None, undo_db: Any = None) -> dict[str, Any]:
    """File a group in one tap: every pending proposal in it, or just `items` (also ones left out). Writes the link, cycle,
    bundle membership, event plan or archive plan in place, one undo token. 404 for a group or item that is not this viewer's."""
    props = _require(batch, viewer, role, person)
    rows = RS.rows(batch.id, person, db)
    inside = [p for p in props if p["gid"] == str(group)]
    if not inside:
        raise SF.Refused(404, "no such group")
    if items is None:
        targets = [p for p in inside if _state(rows, p["item"]) in PENDING_STATES]
    else:
        want = {str(x) for x in items}
        targets = [p for p in inside if p["item"] in want and _state(rows, p["item"]) in PENDING_STATES + (RS.EXCLUDED,)]
        if len(targets) != len(want):
            raise SF.Refused(404, "no such item in that group")
    if not targets:
        return {"filed": 0, "token": None}
    before = {p["item"]: rows.get(p["item"]) for p in targets}
    token = SF.save_undo(person, RS.UNDO_KIND, {"batch": batch.id, "before": before}, undo_db)
    RS.put(batch.id, person, [{"item": p["item"], "state": RS.FILED, **p["filing"], "at": now} for p in targets], db)
    return {"filed": len(targets), "token": token, "group": str(group), "kinds": sorted({p["filing"]["kind"] for p in targets})}


def exclude(batch: Batch | None, viewer: str | None, role: str, person: str | None, group: str, item: str, now: float, *,
            db: Any = None, undo_db: Any = None) -> dict[str, Any]:
    """Leave one document out of its group (fix the exception): nothing is filed for it; it waits in "Left for you"."""
    props = _require(batch, viewer, role, person)
    rows = RS.rows(batch.id, person, db)
    p = next((x for x in props if x["gid"] == str(group) and x["item"] == str(item) and _state(rows, x["item"]) in PENDING_STATES), None)
    if p is None:
        raise SF.Refused(404, "no such item in that group")
    token = SF.save_undo(person, RS.UNDO_KIND, {"batch": batch.id, "before": {p["item"]: rows.get(p["item"])}}, undo_db)
    RS.put(batch.id, person, [{"item": p["item"], "state": RS.EXCLUDED, "at": now}], db)
    return {"excluded": 1, "token": token, "group": str(group)}


def auto_file(batch: Batch | None, viewer: str | None, role: str, person: str | None, now: float, *,
              db: Any = None, undo_db: Any = None) -> dict[str, Any]:
    """File what is certain: a bill, statement or receipt whose account AND amount equal its cycle's, for an adult, never
    medical or legal mail, never what the person took back. Idempotent. One undo token each, kept on the row, which the review
    shows for 7 days as "Filed for you". Undoing one leaves a "manual" row so it is not filed again."""
    if role not in CAN_FILE or not person or batch is None or not viewer:
        return {"filed": 0}
    rows = RS.rows(batch.id, person, db)
    todo = [p for p in proposals(batch, viewer) if p["hard"] and not p["sensitive"] and _state(rows, p["item"]) is None]
    for p in todo:
        token = SF.save_undo(person, RS.UNDO_KIND, {"batch": batch.id, "before": {p["item"]: None}, "instead": {p["item"]: {"at": now}}}, undo_db)
        RS.put(batch.id, person, [{"item": p["item"], "state": RS.AUTO, **p["filing"], "token": token, "at": now}], db)
    return {"filed": len(todo)}
