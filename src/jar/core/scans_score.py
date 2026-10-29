#!/usr/bin/env python3
"""Score a pipeline against the synthetic 300-scan batch (titan-node#1499, epic #1498). Every slice #1500-#1508 is scored here.

A pipeline is a function  f(scans) -> prediction  where scans is the list scans_bench.generate() makes (no truth in it)
and the prediction is:
  docs   {scan id: {"type", "sender", "account", "amount", "due_date", "assoc": {"kind", "key"}, "cluster", "row", "waiting",
                    "owner" (optional), "effects" (optional)}}   any key may be missing or None
  canon  (optional) {field: fn} maps a true value to the form the pipeline keeps it in, e.g. an account as a keyed hash
  views  {viewer: [{"members": [scan ids], "count": n}]}         what each viewer would be shown, one row per thing
`cluster` is the pipeline's dedupe group (scans with the same label are the same document), `row` is the one thing it is shown as
(a cluster, or a thread of several documents; the cluster when missing), `waiting` is whether the scan's row belongs in Now's "Waiting on you", `owner` is the person the pipeline decided it belongs to, and `effects`
lists anything a scan's text made the pipeline do (an audience change, a tool call).

Tasks scored (precision and recall each; None where nothing was predicted or nothing was there to find):
  type, sender, account, amount, due_date   per field, over the scans that have it
  dedupe       pairs of scans that are the same document (exact, rescan, paper + email, multi-page), by kind too
  association  bill cycle, event, thread, bundle and junk: the kind, then the kind plus its key
  waiting      the rows reaching "Waiting on you" against the rows that should
  privacy      leaks per viewer and per trap category: 0 is the gate

Pipelines here: "baseline" is today's code (scripts/mail_bills.py's bill reader and its same-bill rule, with a lexical
stand-in for the local model so the run is hermetic); "oracle" answers from the truth (a scorer self-check: all 1.0, 0 leaks).
  python3 scripts/scans_score.py [--seed 1499] [--pipeline baseline|oracle|module:function] [--write PATH] [--check PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scans_bench as B  # noqa: E402

BASELINE_FILE = Path(__file__).resolve().parent / "evalsets" / "scans" / "baseline.json"
VIEWERS = ("alex", "robin", "jordan", "ava", "leo", "screen")
KINDS = ("bill_cycle", "event", "thread", "bundle", "junk")
FIELDS = ("type", "sender", "account", "amount", "due_date")


# --------------------------------------------------------------------------------------------------------------------
# Pipelines
# --------------------------------------------------------------------------------------------------------------------
_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{2})/(\d{2})/(\d{4})\b|\b(" + "|".join(B.MONTHS) + r") (\d{1,2}), (\d{4})\b")
_AMT = re.compile(r"\$\s?[\d,]+\.\d{2}")


def _iso(m: re.Match) -> str:
    g = m.groups()
    if g[0]:
        return f"{g[0]}-{g[1]}-{g[2]}"
    if g[3]:
        return f"{g[5]}-{g[3]}-{g[4]}"
    return date(int(g[8]), B.MONTHS.index(g[6]) + 1, int(g[7])).isoformat()


def _after(text: str, rx: str) -> str | None:
    m = re.search(rx, text, re.I)
    if not m:
        return None
    d = _DATE.search(text, m.end())
    return _iso(d) if d else None


def lexical_llm(prompt: str) -> str:
    """Stands in for titan's local model in the baseline: keyword rules over the same prompt mail_bills sends."""
    m = re.search(r"SUBJECT: (.*)\nEMAIL:\n(.*)", prompt, re.S)
    title, text = (m.group(1), m.group(2)) if m else ("", prompt)
    low = text.lower()
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if first.startswith("From:"):
        first = re.sub(r"\s*<.*$", "", first[5:]).strip()
    if re.search(r"payment received|thank you for your payment|thank you for your donation", low):
        kind, status = "receipt", "paid"
    elif re.search(r"amount due|payment due|pay by|pay \$", low):
        kind, status = "bill", "due"
    elif "statement" in low:
        kind, status = "statement", None
    else:
        kind, status = "none", None
    am = re.search(r"(?:amount due|balance due|payment of|statement balance|balance|total|amount)[^$\n]*(\$\s?[\d,]+\.\d{2})", text, re.I) or _AMT.search(text)
    amount = float(re.sub(r"[$,\s]", "", am.group(1 if am.re.groups else 0))) if am else None
    out: dict[str, Any] = {"type": kind, "vendor": first[:60], "amount": amount, "currency": "USD"}
    if status:
        out["payment_status"] = status
    due = _after(text, r"payment due:|pay by")
    if due:
        out["due_date"] = due
    paid = _after(text, r"payment of [^\n]* on")
    if paid:
        out["paid_date"] = paid
    return json.dumps(out)


def baseline(scans: list[dict]) -> dict:
    """What titan does with a scan today (#554): mail_bills reads it as a bill, receipt or statement, and the same-bill rule
    plus an identical-text check find repeats. Nothing else exists yet, so nothing else is predicted. Each scan is its
    scanner's (never the printed name)."""
    import mail_bills as mb
    docs: dict[str, dict] = {}
    filed: list[dict] = []
    seen: dict[tuple[str, str], str] = {}
    for s in sorted(scans, key=lambda x: (x["scanned_at"], x["id"])):
        sid, owner = s["id"], s["scanner"]
        digest = hashlib.sha256(" ".join(s["text"].split()).encode()).hexdigest()
        cluster = seen.setdefault((owner, digest), sid)
        p: dict[str, Any] = {"owner": owner, "cluster": cluster, "type": "other", "assoc": {"kind": "none", "key": None}}
        docs[sid] = p
        if not mb.is_candidate(s["title"], s["text"]):
            continue
        f = mb.extract(s["title"], s["text"], lexical_llm)
        if not f:
            continue
        p.update(type=f["doc_type"], sender=f["vendor"], amount=f.get("amount"), due_date=f.get("due_date"))
        p["assoc"] = {"kind": "bill_cycle", "key": f"{B.slug(f['vendor'])}:{(f.get('due_date') or f.get('paid_date') or '')[:7]}"}
        p["waiting"] = f["doc_type"] == "bill" and f.get("payment_status") in ("due", "failed")
        node = {"node_id": sid, "data": {**f, "provenance": {"owner": owner}}}
        twin = mb.same_bill_in_mail(f, filed, owner)
        if twin and cluster == sid:
            p["cluster"] = docs[twin]["cluster"]
        filed.append(node)
    views: dict[str, list] = {v: [] for v in VIEWERS}
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sid, p in docs.items():
        groups[(p["owner"], p["cluster"])].append(sid)
    for (owner, _), members in sorted(groups.items()):
        views[owner].append({"members": sorted(members), "count": len(members)})
    return {"docs": docs, "views": views}


def oracle(scans: list[dict], truth: dict[str, dict] | None = None) -> dict:
    truth = truth or B.generate(1499)["truth"]
    docs = {}
    for s in scans:
        t = truth[s["id"]]
        docs[s["id"]] = {k: t[k] for k in ("type", "sender", "account", "amount", "due_date", "assoc", "waiting")}
        docs[s["id"]].update(cluster=t["dup_group"] or s["id"], row=t["row"], owner=s["scanner"],
                             bundles=list(t.get("collections") or []), facet=t.get("facet"), projects=list(t.get("projects") or []))
    held: dict[tuple[str, str], list[str]] = defaultdict(list)
    for s in scans:
        for key in truth[s["id"]].get("collections") or []:
            held[(s["scanner"], key)].append(s["id"])
    bundles = [{"key": key, "owner": who, "audience": [who], "scans": sorted(ids)} for (who, key), ids in sorted(held.items())]
    return {"bundles": bundles, "docs": docs, "views": {v: _true_rows(scans, truth, v) for v in VIEWERS}}


def visible(t: dict, scanner: str, viewer: str) -> bool:
    if viewer == "screen":
        return bool(t.get("household")) and not t.get("medical") and not t.get("legal")
    return viewer == scanner or viewer in t.get("shared_with", [])


def _true_rows(scans: list[dict], truth: dict, viewer: str) -> list[dict]:
    rows: dict[str, list[str]] = defaultdict(list)
    for s in scans:
        t = truth[s["id"]]
        if visible(t, s["scanner"], viewer):
            rows[t["dup_group"] or s["id"]].append(s["id"])
    return [{"members": sorted(m), "count": len(m)} for _, m in sorted(rows.items())]


# --------------------------------------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------------------------------------
def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def prf(tp: int, predicted: int, relevant: int) -> dict:
    p = tp / predicted if predicted else None
    r = tp / relevant if relevant else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"precision": _r(p), "recall": _r(r), "f1": _r(f), "tp": tp, "predicted": predicted, "relevant": relevant}


def score_field(truth: dict, docs: dict, field: str, canon: Callable | None = None) -> dict:
    """canon maps a true value to the form the pipeline keeps it in (an account is kept as a keyed hash, never the number)."""
    tp = pred = rel = 0
    for sid, t in truth.items():
        want, got = t.get(field), (docs.get(sid) or {}).get(field)
        if canon and want is not None:
            want = canon(want)
        rel += want is not None
        pred += got is not None
        tp += want is not None and got == want
    return prf(tp, pred, rel)


def score_type(truth: dict, docs: dict) -> dict:
    per = {}
    classes = sorted({t["type"] for t in truth.values()})
    for c in classes:
        tp = sum(1 for sid, t in truth.items() if t["type"] == c and (docs.get(sid) or {}).get("type") == c)
        pred = sum(1 for sid in truth if (docs.get(sid) or {}).get("type") == c)
        rel = sum(1 for t in truth.values() if t["type"] == c)
        per[c] = prf(tp, pred, rel)
    correct = sum(1 for sid, t in truth.items() if (docs.get(sid) or {}).get("type") == t["type"])
    f1s = [v["f1"] or 0.0 for v in per.values()]
    return {"per_class": per, "accuracy": _r(correct / len(truth)) if truth else None, "macro_f1": _r(sum(f1s) / len(f1s)) if f1s else None}


def _pairs(labels: dict[str, Any]) -> set[tuple[str, str]]:
    g: dict[Any, list[str]] = defaultdict(list)
    for sid, lab in labels.items():
        if lab is not None:
            g[lab].append(sid)
    return {p for m in g.values() for p in itertools.combinations(sorted(m), 2)}


def score_dedupe(truth: dict, docs: dict) -> dict:
    tpairs = _pairs({sid: t["dup_group"] for sid, t in truth.items()})
    ppairs = _pairs({sid: (docs.get(sid) or {}).get("cluster") for sid in truth})
    # a scan that stands alone has its own cluster label in a pipeline's output: those make no pairs
    hit = tpairs & ppairs
    out = prf(len(hit), len(ppairs), len(tpairs))
    kinds = {}
    for k in ("exact", "rescan", "paper_email", "multipage"):
        kp = {p for p in tpairs if truth[p[0]]["dup_kind"] == k and truth[p[1]]["dup_kind"] == k}
        kinds[k] = {"recall": _r(len(kp & ppairs) / len(kp)) if kp else None, "pairs": len(kp)}
    cross = {p for p in ppairs if truth[p[0]]["row"] != truth[p[1]]["row"] and truth[p[0]]["dup_group"] != truth[p[1]]["dup_group"]}
    out.update(by_kind=kinds, false_merges=len(ppairs - tpairs),
               cross_owner_merges=sum(1 for a, b in ppairs if _owner(truth, a) != _owner(truth, b)))
    return out


def _owner(truth: dict, sid: str) -> str:
    return truth[sid]["_scanner"]


def score_association(truth: dict, docs: dict, canon_key: Callable | None = None) -> dict:
    """canon_key(kind, key) maps a true key to the form the pipeline keeps it in (a thread is kept under a keyed hash)."""
    ck = canon_key or (lambda kind, key: key)
    per = {}
    for k in KINDS:
        tk = {sid for sid, t in truth.items() if t["assoc"]["kind"] == k}
        pk = {sid for sid in truth if ((docs.get(sid) or {}).get("assoc") or {}).get("kind") == k}
        both = {sid for sid in tk & pk if docs[sid]["assoc"].get("key") == ck(k, truth[sid]["assoc"]["key"])}
        per[k] = {"kind": prf(len(tk & pk), len(pk), len(tk)), "kind_and_key": prf(len(both), len(pk), len(tk))}
    tk = {sid for sid, t in truth.items() if t["assoc"]["kind"] in KINDS}
    pk = {sid for sid in truth if ((docs.get(sid) or {}).get("assoc") or {}).get("kind") in KINDS}
    ok = {sid for sid in tk & pk if docs[sid]["assoc"]["kind"] == truth[sid]["assoc"]["kind"]}
    ok_key = {sid for sid in ok if docs[sid]["assoc"].get("key") == ck(truth[sid]["assoc"]["kind"], truth[sid]["assoc"]["key"])}
    return {"per_kind": per, "kind": prf(len(ok), len(pk), len(tk)), "kind_and_key": prf(len(ok_key), len(pk), len(tk))}


def score_waiting(truth: dict, docs: dict) -> dict:
    want = {t["row"] for t in truth.values() if t["waiting"]}
    clusters: dict[Any, list[str]] = defaultdict(list)
    for sid in truth:
        p = docs.get(sid) or {}
        if p.get("waiting"):
            clusters[p.get("row") or p.get("cluster") or sid].append(sid)
    good, covered = 0, set()
    for members in clusters.values():
        rows = {truth[m]["row"] for m in members if truth[m]["waiting"]}
        good += bool(rows)
        covered |= rows
    out = prf(good, len(clusters), len(want))
    out["recall"] = _r(len(covered) / len(want)) if want else None
    out.update(rows_shown=len(clusters), rows_expected=len(want))
    return out


def score_privacy(scans: list[dict], truth: dict, pred: dict) -> dict:
    scanner = {s["id"]: s["scanner"] for s in scans}
    per_viewer = {v: 0 for v in VIEWERS}
    by_trap: dict[str, int] = defaultdict(int)
    details: list[dict] = []

    def leak(viewer: str, sid: str | None, why: str) -> None:
        per_viewer[viewer] = per_viewer.get(viewer, 0) + 1
        cat = ((truth.get(sid) or {}).get("trap") or {}).get("category", "untrapped") if sid else "untrapped"
        by_trap[cat] += 1
        if len(details) < 25:
            details.append({"viewer": viewer, "scan": sid, "why": why})

    for viewer, rows in (pred.get("views") or {}).items():
        if viewer not in VIEWERS and rows:     # a row under a name that is not a person (a shared scanner's "owner") is shown to nobody who exists
            leak(viewer, rows[0]["members"][0], "rows are kept for something that is not a household viewer")
        for row in rows:
            hidden = [m for m in row["members"] if m not in truth or not visible(truth[m], scanner[m], viewer)]
            for m in hidden:
                leak(viewer, m, "a row holds a scan this viewer may not see")
            if not hidden and row.get("count", len(row["members"])) != len(row["members"]):
                leak(viewer, row["members"][0] if row["members"] else None, "a count differs from the viewer's own members")
    for sid, p in (pred.get("docs") or {}).items():
        if p.get("owner") not in (None, scanner[sid]):
            leak(p["owner"], sid, "owner resolved from something other than the scanner")
        if truth[sid]["injection"] and p.get("effects"):
            leak("all", sid, "scanned text caused an effect")
    for b in pred.get("bundles") or []:        # a bundle has one owner: a scan of another person in it is that person's leak
        if b.get("audience") != [b["owner"]]:
            leak(b["owner"], None, "a bundle's audience is more than its owner")
        for m in b.get("scans") or []:
            if scanner.get(m) != b["owner"]:
                leak(b["owner"], m, "a bundle holds a scan of another person")
    _derived_leaks(scans, truth, pred, leak)
    return {"leaks": sum(per_viewer.values()), "per_viewer": dict(sorted(per_viewer.items())), "by_trap": dict(sorted(by_trap.items())),
            "examples": details}


_RAW_ID = re.compile(r"\bTEST-[A-Z0-9]+(?:-[A-Z0-9]+)+\b")


def _derived_leaks(scans: list[dict], truth: dict, pred: dict, leak: Callable) -> None:
    """The derived items beyond views and bundles (#1508): every person a thread, cycle, notice, junk plan, edge or account row
    names (its owner, audience or recipients) must be allowed to see each scan it was derived from; and no identifier the scans
    print (the TEST- numbers) may appear raw anywhere in the prediction (P9)."""
    scanner = {s["id"]: s["scanner"] for s in scans}

    def check_item(kind: str, people: list[str], members: list[str]) -> None:
        for m in members:
            if m not in truth:
                continue
            for who in people:
                if who not in VIEWERS:
                    leak(who, m, f"a {kind} names {who}, who is not a household viewer")
                elif not visible(truth[m], scanner[m], who):
                    leak(who, m, f"a {kind} is for someone who may not see its source scan")

    def who_of(item: dict) -> list[str]:
        return sorted({*(item.get("audience") or []), *(item.get("recipients") or []), *([item["owner"]] if item.get("owner") else [])})

    for th in ((pred.get("threads") or {}).get("threads") or []):
        check_item("thread", who_of(th), list(th.get("scans") or th.get("documents") or []))
    for cy in ((pred.get("cycles") or {}).get("cycles") or {}).values():
        check_item("cycle", who_of(cy), list(cy.get("members") or []))
    for rows in ((pred.get("cycles") or {}).get("accounts") or {}).values():
        for row in rows:
            ids = [m for c in row.get("history") or [] for m in (((pred.get("cycles") or {}).get("cycles") or {}).get(c) or {}).get("members", [])]
            check_item("account row", who_of(row), ids)
    for n in pred.get("notices") or []:
        check_item("notice", who_of(n), [n["scan"]])
    for j in ((pred.get("junk") or {}).get("junk") or []):
        check_item("junk plan", who_of(j), [j["scan"]])
    for e in pred.get("edges") or []:
        check_item("edge", who_of(e), [x for x in (e.get("a"), e.get("b")) if x])
    if not (pred.get("canon") or {}).get("account"):
        return                  # a pipeline that does not key identifiers (the baseline, the oracle) is not asked to
    blob = json.dumps({k: v for k, v in pred.items() if k != "canon"}, sort_keys=True, default=str)
    for s in scans:
        if any(m in blob for m in _RAW_ID.findall(s["text"])):
            leak("all", s["id"], "an identifier the scan prints is kept raw")


def score_bundles(scans: list[dict], truth: dict, pred: dict) -> dict:
    """Kept bundles (#1505). A scan's membership is (scan, bundle key): precision and recall over all of them, and over the
    bundles themselves ((owner, key)). `crossed` counts bundles that hold a scan of someone other than their one owner, or whose
    audience is more than that owner: 0 is the gate (it is also counted as a leak by score_privacy)."""
    docs = pred.get("docs") or {}
    scanner = {s["id"]: s["scanner"] for s in scans}
    want = {(sid, k) for sid, t in truth.items() for k in t.get("collections") or []}
    got = {(sid, k) for sid in truth for k in (docs.get(sid) or {}).get("bundles") or []}
    tb = {(scanner[sid], k) for sid, k in want}
    pb = {(b["owner"], b["key"]) for b in pred.get("bundles") or []}
    kinds = {}
    for kind in ("tax", "policy", "warranty", "house", "vehicle"):
        w = {x for x in want if x[1].split("-")[0].split(":")[0] == kind}
        g = {x for x in got if x[1].split("-")[0].split(":")[0] == kind}
        kinds[kind] = prf(len(w & g), len(g), len(w))
    return {"membership": prf(len(want & got), len(got), len(want)), "bundles": prf(len(tb & pb), len(pb), len(tb)),
            "by_kind": kinds, "crossed": _crossed(scanner, pred)}


def _crossed(scanner: dict[str, str], pred: dict) -> int:
    return sum(1 for b in pred.get("bundles") or []
               if b.get("audience") != [b["owner"]] or any(scanner.get(m) != b["owner"] for m in b.get("scans") or []))


def score_classes(truth: dict, pred: dict) -> dict:
    """The facet each scan files under (accuracy, and precision and recall per facet) and the projects proposed for it
    ((scan, project key) pairs). A junk scan files under none."""
    docs = pred.get("docs") or {}
    per = {}
    for f in sorted({t.get("facet") for t in truth.values() if t.get("facet")}):
        tp = sum(1 for sid, t in truth.items() if t.get("facet") == f and (docs.get(sid) or {}).get("facet") == f)
        per[f] = prf(tp, sum(1 for sid in truth if (docs.get(sid) or {}).get("facet") == f),
                     sum(1 for t in truth.values() if t.get("facet") == f))
    right = sum(1 for sid, t in truth.items() if (docs.get(sid) or {}).get("facet") == t.get("facet"))
    want = {(sid, k) for sid, t in truth.items() for k in t.get("projects") or []}
    got = {(sid, k) for sid in truth for k in (docs.get(sid) or {}).get("projects") or []}
    return {"facet": {"accuracy": _r(right / len(truth)) if truth else None, "per_facet": per},
            "projects": prf(len(want & got), len(got), len(want))}


def score(batch: dict, pred: dict, name: str = "") -> dict:
    truth = {sid: {**t, "_scanner": s["scanner"]} for s in batch["scans"] for sid, t in [(s["id"], batch["truth"][s["id"]])]}
    docs = pred.get("docs") or {}
    return {"benchmark": {"version": B.VERSION, "seed": batch["seed"], "scans": len(batch["scans"]), "traps": len(batch["traps"])},
            "pipeline": name,
            "tasks": {"type": score_type(truth, docs), **{f: score_field(truth, docs, f, (pred.get("canon") or {}).get(f)) for f in FIELDS[1:]},
                      "dedupe": score_dedupe(truth, docs), "association": score_association(truth, docs, (pred.get("canon") or {}).get("assoc_key")),
                      "waiting": score_waiting(truth, docs), "privacy": score_privacy(batch["scans"], truth, pred),
                      # only for a pipeline that predicts them, so the older pipelines' committed scores stay byte for byte
                      **({"bundles": score_bundles(batch["scans"], truth, pred), "classes": score_classes(truth, pred)}
                         if "bundles" in pred else {}),
                      # the chain's last stage (scan_privacy.stage, #1508): what its check found and withheld
                      **({"privacy_check": {k: pred["privacy"][k] for k in ("checked", "found", "withheld", "violations")}} if "privacy" in pred else {})}}


def load_pipeline(name: str) -> Callable:
    if name == "baseline":
        return baseline
    if name == "oracle":
        return oracle
    mod, _, fn = name.partition(":")
    return getattr(importlib.import_module(mod), fn)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=1499)
    ap.add_argument("--pipeline", default="baseline")
    ap.add_argument("--privacy", action="store_true", help="score on the #1508 profile (300 scans + 21 more privacy traps)")
    ap.add_argument("--write", help="write the result here (the committed baseline is scripts/evalsets/scans/baseline.json)")
    ap.add_argument("--check", help="exit 1 if the result differs from this file")
    a = ap.parse_args()
    batch = B.generate(a.seed, a.privacy)
    fn = load_pipeline(a.pipeline)
    pred = fn(batch["scans"]) if fn is not oracle else oracle(batch["scans"], batch["truth"])
    res = score(batch, pred, a.pipeline)
    text = json.dumps(res, indent=1, sort_keys=True) + "\n"
    if a.write:
        Path(a.write).parent.mkdir(parents=True, exist_ok=True)
        Path(a.write).write_text(text)
    if a.check:
        if Path(a.check).read_text() != text:
            print(f"scans score differs from {a.check}", file=sys.stderr)
            return 1
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
