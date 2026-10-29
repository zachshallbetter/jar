#!/usr/bin/env python3
"""Per-person privacy for scanned mail, as a property of the whole path (titan-node#1508, epic #1498 slice 9).

Every stage of scan_pipeline takes care of its own owner and audience. This module is the one check that does not trust them:
it runs after the chain, over everything the chain derived (an envelope, a dedupe cluster and its review items, a bill cycle and
the account rows that list it, a thread, a notice and its edge, a bundle and the project proposed for it, a junk plan, and every
row each viewer would be shown), and holds each one to the same rules:

  owner      a derived item has an owner who is a person of the household, and it is the scanner of every scan it was derived
             from (P1, P8): a scan from the shared household scanner belongs to nobody, so it derives nothing, until an adult claims it
  audience   a derived item has an audience (a list of people), and it is never wider than the narrowest audience of the scans it
             came from: derived details inherit their most private source (P3)
  sensitive  a medical or legal scan stays with its scanner. A rule, an account mapping or a bulk approval never shares it; only a
             per-item share can, never to the shared screen, never to a kid (P4, P7). An unknown sender with medical words is medical:
             errors fall toward private (D2)
  kids       a kid sees only kid-safe derived facts (an event, a deadline, a letter) and only after a parent shared the scan (P6)
  names      no derived item names a person who cannot see it, and a printed name never decides whose mail it is (P2)
  numbers    an account, member, policy or claim number a scan prints is never raw in a derived item (P9)
  text       a scan's own instructions change no audience and cause no effect (P10)
  junk       a junk plan is the owner's, reversible, and never touches a medical or legal scan
  egress     medical and legal scans are never sent to the cloud (P11)
  views      each viewer's rows hold only scans that viewer may see, and a count is the count of those (P7, P8)
  deletion   after a scan is deleted nothing derived refers to it (P12)

`check` reports violations and never changes anything; `stage` is the chain's last stage: it fails closed, withholding what the
check rejects and recording what it did, so a bug in a stage is a withheld row and a report, never a leak. `view_for` is the one
audience filter any surface that shows scans must go through. Nothing here reads the knowledge base, the mail or a live service.

  python3 scripts/scan_privacy.py                      the before / after table
  python3 scripts/scan_privacy.py --write scripts/evalsets/scans/privacy.json
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_notices as N  # noqa: E402

VERSION = 1
HERE = Path(__file__).resolve().parent
CLASS_FILE = HERE / "evalsets" / "scans" / "sensitive_class.json"
OUT_FILE = HERE / "evalsets" / "scans" / "privacy.json"
SCREEN = "screen"
HOUSEHOLD = "household"                       # the shared scanner's name, and the group a "household" share names
# what a kid may be shown derived from a parent's scan: an event, a deadline or a plain letter, never money, a claim or a policy
KID_SAFE_TYPES = ("appointment", "deadline", "letter")
UNCLAIMED = "unclaimed"
_ID_TOKEN = re.compile(r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b")


_MONEY = re.compile(r"[$€£]\s?\d")


def kid_safe(scan: dict[str, Any], env: dict[str, Any] | None, sens: set[str]) -> bool:
    """A scan a kid may be shown derived facts of: not medical or legal, an event, a deadline or a letter, and no money in it
    (an envelope can mistake a youth savings statement for a letter; the dollar amounts in the text still say what it is)."""
    typ = ((env or {}).get("fields") or {}).get("type")
    return not sens and (typ is None or typ in KID_SAFE_TYPES) and not _MONEY.search(str(scan.get("text") or ""))


# ── the sensitive class (D2): a reviewed, versioned file of hard keys, plus the local model's label ────────────────────
_CLASS: dict[str, Any] | None = None


def sensitive_class() -> dict[str, Any]:
    global _CLASS
    if _CLASS is None:
        _CLASS = json.loads(CLASS_FILE.read_text())
    return _CLASS


def _rx(words: list[str]) -> re.Pattern:
    return re.compile(r"(?i)\b(?:" + "|".join(re.escape(w) for w in words) + r")")


def classify(scan: dict[str, Any], label: str | None = None) -> set[str]:
    """The sensitive classes a scan belongs to: {"medical"}, {"legal"}, both, or none. Either layer is enough (the class file's
    sender categories and health or legal terms, or the model's `label`): an error falls toward private. A kid's printed name
    is not a class; whose mail it is stays the scanner's business (see `kid_subject`)."""
    c = sensitive_class()
    out: set[str] = set()
    head = "\n".join(str(scan.get("text") or "").splitlines()[:1] + [str(scan.get("title") or "")])
    text = str(scan.get("text") or "") + "\n" + str(scan.get("title") or "")
    for cls in ("medical", "legal"):
        if label == cls or _rx(c[cls]["senders"]).search(head) or _rx(c[cls]["terms"]).search(text):
            out.add(cls)
    return out


def kid_subject(scan: dict[str, Any], people: dict[str, dict[str, Any]]) -> set[str]:
    """Kids whose printed name is in the scan. Used only to hold the scan back (a name never decides ownership, #1428)."""
    text = str(scan.get("text") or "")
    return {p for p, who in people.items() if who.get("role") == "kid" and who.get("name") and who["name"].lower() in text.lower()}


# ── who may see a scan ───────────────────────────────────────────────────────────────────────────────────────────
def effective_audience(scan: dict[str, Any], people: dict[str, dict[str, Any]], shares: dict[str, list[dict[str, Any]]] | None = None,
                       env: dict[str, Any] | None = None, label: str | None = None) -> set[str]:
    """The people (and "screen") who may see anything derived from this scan: the scanner, plus whoever it was shared with.
    A share of a sensitive scan counts only when it is a per-item share (`item: True`); the screen never gets one, and a kid
    gets a scan only when it is kid-safe (not sensitive, an event, a deadline or a letter)."""
    who = scan.get("scanner")
    aud: set[str] = {who} if who in people else set()
    if not aud:
        return set()                              # nobody's: the shared scanner, or someone the household does not know
    sens = classify(scan, label)
    for sh in (shares or {}).get(scan["id"], []):
        if sens and not sh.get("item"):
            continue                              # a rule or bulk approval never shares sensitive mail
        to = set(sh.get("with") or [])
        if HOUSEHOLD in to:
            to = (to - {HOUSEHOLD}) | set(people) | {SCREEN}
        aud |= to
    if sens:
        aud.discard(SCREEN)
    safe = kid_safe(scan, env, sens)
    return {p for p in aud if safe or (people.get(p) or {}).get("role") != "kid" or p == who} - ({SCREEN} if sens else set())


# ── the derived items of a prediction ───────────────────────────────────────────────────────────────────────────
class Item:
    """One derived thing: what it is, who it names as owner, audience and recipients, the scans it was derived from, the data
    to read its strings from, and how to withhold it."""

    def __init__(self, kind: str, ref: str, owner: Any, audience: Any, members: list[str], data: Any, drop: Callable[[], None],
                 recipients: list[str] | None = None, count: int | None = None, counted: list[str] | None = None) -> None:
        self.kind, self.ref, self.owner, self.audience = kind, ref, owner, audience
        self.members, self.data, self.drop, self.recipients, self.count = members, data, drop, recipients or [], count
        self.counted = members if counted is None else counted      # what `count` counts (a bundle counts documents, not scans)

    def people(self) -> set[str]:
        a = self.audience if isinstance(self.audience, (list, tuple, set)) else ([] if self.audience is None else [self.audience])
        return {str(x) for x in a} | {str(x) for x in self.recipients} | ({str(self.owner)} if self.owner else set())


def _scan_id(x: str) -> str:
    return x.split(":", 1)[1] if x.startswith("scan:") else x


def items(pred: dict[str, Any]) -> Iterator[Item]:
    """Every derived item in a prediction, in a stable order. The shapes are the stages' own."""
    docs = pred.get("docs") or {}
    for sid, d in sorted(docs.items()):
        env = d.get("envelope") or {}
        rec = env.get("record") or {}
        if d.get("held"):
            continue
        yield Item("envelope", sid, d.get("owner"), rec.get("audience", [d.get("owner")] if d.get("owner") else None), [sid], env,
                   lambda d=d: d.update(held=UNCLAIMED, owner=None))
    dd = pred.get("dedupe") or {}
    for cid, c in sorted((dd.get("clusters") or {}).items()):
        members = [c.get("kept")] + [h["scan"] for h in c.get("hidden") or []] + list(c.get("pages") or [])
        yield Item("cluster", cid, c.get("owner"), [c.get("owner")], sorted({m for m in members if m}), c,
                   lambda cid=cid: dd["clusters"].pop(cid, None), count=c.get("count"))
    for i, r in enumerate(dd.get("review") or []):
        yield Item("review", str(i), r.get("owner"), [r.get("owner")], list(r.get("members") or []), r,
                   lambda r=r: dd["review"].remove(r) if r in dd["review"] else None)
    cy = pred.get("cycles") or {}
    for cid, c in sorted((cy.get("cycles") or {}).items()):
        yield Item("cycle", cid, c.get("owner"), c.get("audience"), list(c.get("members") or []), c,
                   lambda cid=cid: cy["cycles"].pop(cid, None))
    for owner, rows in sorted((cy.get("accounts") or {}).items()):
        for j, row in enumerate(rows):
            ids = [m for h in row.get("history") or [] for m in ((cy.get("cycles") or {}).get(h) or {}).get("members", [])]
            yield Item("account", f"{owner}/{j}", row.get("owner"), row.get("audience"), ids, row,
                       lambda rows=rows, row=row: rows.remove(row) if row in rows else None)
    th = pred.get("threads") or {}
    for t in th.get("threads") or []:
        yield Item("thread", t.get("id", "?"), t.get("owner"), t.get("audience"), list(t.get("scans") or t.get("documents") or []), t,
                   lambda t=t: th["threads"].remove(t) if t in th["threads"] else None, count=t.get("count"))
    for n in pred.get("notices") or []:
        yield Item("notice", n.get("scan", "?"), n.get("owner"), None, [n["scan"]] if n.get("scan") else [], n,
                   lambda n=n: pred["notices"].remove(n) if n in pred["notices"] else None, recipients=n.get("recipients"))
    for e in pred.get("edges") or []:
        yield Item("edge", str(e.get("id") or e.get("a")), e.get("owner"), e.get("audience"),
                   [x for x in (e.get("a"), e.get("b")) if x], e, lambda e=e: pred["edges"].remove(e) if e in pred["edges"] else None)
    for b in pred.get("bundles") or []:
        ids = list(b.get("scans") or b.get("documents") or [])
        yield Item("bundle", b.get("id", "?"), b.get("owner"), b.get("audience"), ids, b,
                   lambda b=b: pred["bundles"].remove(b) if b in pred["bundles"] else None, count=b.get("count"),
                   counted=list(b.get("documents") or ids))
        pr = b.get("project")
        if isinstance(pr, dict):               # the project proposed for a bundle holds only what the bundle holds
            yield Item("proposal", pr.get("id", "?"), b.get("owner"), b.get("audience"), [_scan_id(x) for x in pr.get("items") or []], pr,
                       lambda b=b: b.update(project=None), count=pr.get("count"))
    jk = pred.get("junk") or {}
    for j in jk.get("junk") or []:
        yield Item("junk", j.get("scan", "?"), j.get("owner"), [j.get("owner")], [j["scan"]] if j.get("scan") else [], j,
                   lambda j=j: jk["junk"].remove(j) if j in jk["junk"] else None)
    for viewer, rows in sorted((pred.get("views") or {}).items()):
        for k, row in enumerate(list(rows)):
            yield Item("view", f"{viewer}/{k}", None, [viewer], list(row.get("members") or []), row,
                       lambda rows=rows, row=row: rows.remove(row) if row in rows else None, count=row.get("count"))


def _strings(x: Any) -> Iterator[str]:
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)


def _names(people: dict[str, dict[str, Any]]) -> dict[str, re.Pattern]:
    out = {}
    for p, who in people.items():
        full = str(who.get("name") or "").strip()
        first = full.split(" ")[0] if full else p
        out[p] = re.compile(r"(?i)\b(?:" + "|".join(sorted({re.escape(full), re.escape(first)} - {""}, key=len, reverse=True)) + r")\b")
    return out


# ── the check ────────────────────────────────────────────────────────────────────────────────────────────────────
def check(scans: list[dict[str, Any]], pred: dict[str, Any], *, people: dict[str, dict[str, Any]] | None = None,
          shares: dict[str, list[dict[str, Any]]] | None = None, labels: dict[str, str] | None = None,
          deleted: list[str] | None = None) -> list[dict[str, Any]]:
    """Every rule broken by the derived items of `pred`, each as {rule, kind, ref, why}. Empty is a pass. `people` is the
    household ({id: {role, name, parents}}), `shares` the explicit shares ({scan id: [{with, item}]}), `labels` the local
    model's class per scan, `deleted` the scans whose deletion was asked for."""
    people = N.BENCH_PEOPLE if people is None else people
    by_id = {s["id"]: s for s in scans}
    docs = pred.get("docs") or {}
    aud = {sid: effective_audience(s, people, shares, (docs.get(sid) or {}).get("envelope"), (labels or {}).get(sid)) for sid, s in by_id.items()}
    sens = {sid: classify(s, (labels or {}).get(sid)) for sid, s in by_id.items()}
    names = _names(people)
    raw = {sid: set(_ID_TOKEN.findall(str(s.get("text") or ""))) for sid, s in by_id.items()}
    out: list[dict[str, Any]] = []

    def bad(rule: str, it: Item, why: str) -> None:
        out.append({"rule": rule, "kind": it.kind, "ref": it.ref, "why": why})

    for it in items(pred):
        known = [m for m in it.members if m in by_id]
        for m in it.members:
            if m not in by_id:
                bad("unknown_source", it, f"refers to {m}, which is not a scan of this batch")
        who = it.people()
        if it.kind != "view":
            if not it.owner or it.owner not in people:
                bad("unowned", it, "owner is not a person of the household" if it.owner else "no owner")
            explicit = it.kind in ("envelope", "cycle", "account", "thread", "bundle", "proposal", "edge")
            if (explicit and not it.audience) or (it.kind == "notice" and not it.recipients):
                bad("audience", it, "no audience")
            if not who or any(w not in people for w in who):
                bad("audience", it, f"audience is not a list of people: {sorted(who)}")
            if it.owner and who and it.owner not in who and it.kind not in ("notice",):
                bad("audience", it, "the owner is not in the audience")
            for m in known:
                if by_id[m]["scanner"] != it.owner:
                    bad("mixed_owner", it, f"{m} is another person's scan")
        else:
            if next(iter(who), None) not in people and next(iter(who), None) != SCREEN:
                bad("view", it, f"rows are kept for {sorted(who)}, who is not a viewer")
        # an item is for no one who may not see every scan it came from
        if known:
            allowed = set.intersection(*(aud[m] for m in known))
            extra = who - allowed
            if extra and it.kind != "view":
                bad("wider_than_source", it, f"{sorted(extra)} may not see all of {len(known)} source scan(s)")
            if it.kind == "view":
                for v in who:
                    for m in known:
                        if v not in aud[m]:
                            bad("view_leak", it, f"{v} is shown {m}, which they may not see")
                            break
            for p in who & {p for p, w in people.items() if w.get("role") == "kid"}:
                if not all(kid_safe(by_id[m], (docs.get(m) or {}).get("envelope"), sens[m]) for m in known):
                    bad("kid_unsafe", it, f"{p} is shown more than kid-safe facts")
        if it.count is not None and known and it.count != len(set(it.counted)):
            bad("count", it, f"count {it.count} is not the {len(set(it.counted))} things it holds")
        # nobody who cannot see the item is named in it
        if it.kind not in ("envelope", "view"):
            text = " ".join(_strings({k: v for k, v in it.data.items() if k not in ("members", "scans", "documents", "items", "history")}
                                     if isinstance(it.data, dict) else it.data))
            for p, rx in names.items():
                if p not in who and rx.search(text):
                    bad("names", it, f"names {p}, who cannot see it")
            blob = text
            for m in known:
                for tok in raw[m]:
                    if tok in blob:
                        bad("raw_identifier", it, "keeps a printed identifier raw")
                        break
        if it.kind == "junk":
            if it.data.get("do") != "archive" or it.data.get("reversible") is not True:
                bad("junk_irreversible", it, "a junk plan must archive, and be undoable")
            if any(sens.get(m) for m in known):
                bad("junk_sensitive", it, "a junk plan touches a medical or legal scan")
    # what a scan's own text did, and where the chain sent it
    for sid, d in sorted(docs.items()):
        if d.get("effects"):
            out.append({"rule": "effect", "kind": "envelope", "ref": sid, "why": "scanned text caused an effect"})
        if d.get("held") and (d.get("owner") or sid not in by_id):
            out.append({"rule": "unowned", "kind": "envelope", "ref": sid, "why": "a held scan carries an owner"})
        if sid in by_id and by_id[sid]["scanner"] not in people and not d.get("held"):
            out.append({"rule": "unowned", "kind": "envelope", "ref": sid, "why": "a scan nobody claimed was processed"})
    insts = {sid for sid, d in docs.items() if ((d.get("envelope") or {}).get("fields") or {}).get("instruction_like")}
    for it in items(pred):
        if it.kind in ("notice", "edge", "bundle", "proposal") and insts & set(it.members) and it.kind in ("notice", "edge"):
            bad("effect", it, "a scan that instructs caused a proposal")
    for e in pred.get("egress") or []:
        if e.get("destination") not in (None, "local") and sens.get(e.get("scan")):
            out.append({"rule": "egress", "kind": "egress", "ref": e.get("scan", "?"), "why": "a medical or legal scan left the house"})
    for sid in deleted or []:
        hits = references(pred, sid)
        if hits:
            out.append({"rule": "deleted", "kind": hits[0], "ref": sid, "why": f"still referenced by {', '.join(sorted(set(hits)))}"})
    return out


def references(pred: dict[str, Any], scan_id: str) -> list[str]:
    """The kinds of derived item that still mention a scan (by id), for the deletion rule (P12)."""
    hits = [it.kind for it in items(pred) if scan_id in it.members]
    blob = json.dumps({k: v for k, v in pred.items() if k not in ("canon",)}, sort_keys=True, default=str)
    if f'"{scan_id}"' in blob or f"scan:{scan_id}" in blob:
        hits.append("prediction")
    return hits


# ── the chain's last stage: fail closed ────────────────────────────────────────────────────────────────────────
def claimed(scans: list[dict[str, Any]], people: dict[str, dict[str, Any]] | None = None) -> tuple[list[dict], list[dict]]:
    """(scans of a person of the household, scans nobody owns). The chain runs on the first; the second derive nothing."""
    people = N.BENCH_PEOPLE if people is None else people
    return [s for s in scans if s.get("scanner") in people], [s for s in scans if s.get("scanner") not in people]


def stage(scans: list[dict[str, Any]], pred: dict[str, Any], *, people: dict[str, dict[str, Any]] | None = None,
          shares: dict[str, list[dict[str, Any]]] | None = None, labels: dict[str, str] | None = None) -> dict[str, Any]:
    """Check the chain's result; withhold every item the check rejects; check again. `pred["privacy"]` records what was found
    and what was done, so a withheld row is visible to the people who run the system and to nobody else."""
    people = N.BENCH_PEOPLE if people is None else people
    for s in claimed(scans, people)[1]:
        pred.setdefault("docs", {})[s["id"]] = {"owner": None, "held": UNCLAIMED}
    found = check(scans, pred, people=people, shares=shares, labels=labels)
    withheld = []
    if found:
        bad = {(v["kind"], str(v["ref"])) for v in found}
        for it in list(items(pred)):
            if (it.kind, str(it.ref)) in bad:
                it.drop()
                withheld.append({"kind": it.kind, "ref": it.ref})
    left = check(scans, pred, people=people, shares=shares, labels=labels)
    pred["privacy"] = {"version": VERSION, "checked": sum(1 for _ in items(pred)), "found": len(found), "withheld": len(withheld), "violations": len(left),
                       "held": sorted(s["id"] for s in claimed(scans, people)[1])}
    return pred


# ── the one audience filter ───────────────────────────────────────────────────────────────────────────────────
def view_for(scans: list[dict[str, Any]], pred: dict[str, Any], viewer: str, *, people: dict[str, dict[str, Any]] | None = None,
             shares: dict[str, list[dict[str, Any]]] | None = None, labels: dict[str, str] | None = None) -> dict[str, Any]:
    """What `viewer` may be shown of a prediction: only items whose every source scan the viewer may see, each with the viewer's own
    counts. Any surface that shows scans (Now, Unsorted, /v1/items, the calendar, search, `situation()`, the graph) reads through
    this and nothing else. A viewer the household does not know, and the screen unless an adult shared the scan, sees nothing."""
    people = N.BENCH_PEOPLE if people is None else people
    mine = copy.deepcopy(pred)
    by_id = {s["id"]: s for s in scans}
    docs = mine.get("docs") or {}
    aud = {sid: effective_audience(s, people, shares, (docs.get(sid) or {}).get("envelope"), (labels or {}).get(sid)) for sid, s in by_id.items()}
    for it in list(items(mine)):
        if it.kind == "view":
            continue
        ok = it.members and all(m in by_id and viewer in aud[m] for m in it.members)
        if not ok or viewer not in (it.people() | {it.owner}):
            it.drop()
    for sid in list(docs):
        if sid not in by_id or viewer not in aud[sid]:
            docs.pop(sid)
    ok = {sid for sid in by_id if viewer in aud[sid]}
    # the maps keyed by scan id say, by existing, that a scan exists: keep only the viewer's own
    for group, name in (("cycles", "cycle_of"), ("threads", "of"), ("junk", "of"), ("dedupe", "cluster")):
        if isinstance(mine.get(group), dict) and isinstance(mine[group].get(name), dict):
            mine[group][name] = {k: v for k, v in mine[group][name].items() if k in ok}
    if isinstance(mine.get("junk"), dict) and isinstance(mine["junk"].get("kept"), list):
        mine["junk"]["kept"] = [k for k in mine["junk"]["kept"] if (k if isinstance(k, str) else k.get("scan")) in ok]
    if isinstance(mine.get("cycles"), dict) and isinstance(mine["cycles"].get("accounts"), dict):
        mine["cycles"]["accounts"] = {k: v for k, v in mine["cycles"]["accounts"].items() if k == viewer and v}
    mine.pop("canon", None)
    mine["views"] = {viewer: [r for r in (mine.get("views") or {}).get(viewer, [])
                              if r.get("members") and all(m in by_id and viewer in aud[m] for m in r["members"])]}
    mine.pop("privacy", None)
    return mine


# ── scoring the whole path, before and after ───────────────────────────────────────────────────────────────────
def report(seed: int = 1499) -> dict[str, Any]:
    """The #1508 result: leaks per viewer and per trap category on the original 300 scans and on the privacy profile (321),
    before (the chain as it stood) and after (the chain plus this stage), and how well the sensitive class finds what is."""
    import scan_pipeline as P
    import scans_bench as B
    import scans_score as S
    out: dict[str, Any] = {"version": VERSION, "seed": seed, "profiles": {}}
    for name, extra in (("base", False), ("privacy", True)):
        batch = B.generate(seed, extra)
        pre = S.score(batch, P.chain(batch["scans"]), "chain")["tasks"]["privacy"]
        post = S.score(batch, P.pipeline(batch["scans"]), "scan_pipeline:pipeline")["tasks"]["privacy"]
        traps = defaultdict(int)
        for t in batch["traps"]:
            traps[t["category"]] += 1
        pred = P.pipeline(batch["scans"])
        cats = sorted(traps)
        out["profiles"][name] = {
            "scans": len(batch["scans"]), "traps": len(batch["traps"]),
            "before": {"leaks": pre["leaks"], "per_viewer": pre["per_viewer"]},
            "after": {"leaks": post["leaks"], "per_viewer": post["per_viewer"], "check": pred["privacy"]},
            "by_category": {c: {"traps": traps[c], "leaks_before": pre["by_trap"].get(c, 0), "leaks_after": post["by_trap"].get(c, 0)} for c in cats},
            "untrapped": {"before": pre["by_trap"].get("untrapped", 0), "after": post["by_trap"].get("untrapped", 0)},
            "class": class_quality(batch),
        }
    return out


def class_quality(batch: dict[str, Any]) -> dict[str, Any]:
    """The sensitive class against the benchmark's truth (medical, legal): recall must be 1.0; precision is allowed to be lower,
    because the errors fall toward private."""
    res = {}
    for cls in ("medical", "legal"):
        tp = fp = fn = 0
        for s in batch["scans"]:
            want, got = bool(batch["truth"][s["id"]].get(cls)), cls in classify(s)
            tp += want and got
            fp += got and not want
            fn += want and not got
        res[cls] = {"tp": tp, "false_positive": fp, "missed": fn}
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=1499)
    ap.add_argument("--write", help="write the result here (the committed one is scripts/evalsets/scans/privacy.json)")
    ap.add_argument("--check", help="exit 1 if the result differs from this file")
    a = ap.parse_args()
    text = json.dumps(report(a.seed), indent=1, sort_keys=True) + "\n"
    if a.write:
        Path(a.write).parent.mkdir(parents=True, exist_ok=True)
        Path(a.write).write_text(text)
    if a.check and Path(a.check).read_text() != text:
        print(f"scan privacy differs from {a.check}", file=sys.stderr)
        return 1
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
