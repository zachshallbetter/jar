#!/usr/bin/env python3
"""Index-time enrichment for the knowledge base (titan-node#371), measured before it's applied.

A one-line context header per item, built in code from what titan already knows, says what the item is and
whose it is in the household's own words, so a question asked that way finds it:
    Ava's calendar event: Ballet + Modern, Thu Oct 8
    Email to Zach from Maple Health, Oct 2: Your prescription is ready
    Zach's note: Vet info
It's indexed with the item (prepended to its text). Likely questions (doc2query, local model) and chunking
by type are later steps; calendar items are already one event each.

Nothing changes until it's measured and agreed:
  python3 scripts/kb_enrich.py compare      embeds every KB item with and without its header on titan's
                                            embedding server (in memory only) and scores retrieval_eval's
                                            questions both ways: hit@1, hit@3
  python3 scripts/kb_enrich.py reindex      dry run: how many items would be re-indexed, per type
  python3 scripts/kb_enrich.py reindex --apply   (a fleet change: only after the owner session says so)
New items aren't headed at ingest: KB_HEADERS was removed after the household re-test came out mixed
(#634: +9 hit@3 on one set, -10 hit@1 on the "whose" set it was meant to help; #945). The tools above
stay for a re-measure.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

EMBED_URL = os.environ.get("KB_EMBED_URL", "http://127.0.0.1:8015/v1/embeddings")


def _first(name: str | None) -> str:
    return str(name or "").split()[0] if name else ""


def _when(md: dict[str, Any]) -> str:
    try:
        d = datetime.fromisoformat(str(md.get("observed_at") or "").replace("Z", "+00:00"))
        return d.strftime("%b ") + str(d.day)
    except ValueError:
        return ""


def header(stype: str, title: str, text: str, md: dict[str, Any], names: dict[str, str],
           calendars: dict[str, str]) -> str:
    """The context line for one item. names: person id → name; calendars: calendar title (lower) → whose
    name (Settings › Household). Empty when there's nothing to add (titan's own records)."""
    owner = _first(names.get(str(md.get("owner") or ""), "")) or ""
    t = " ".join(str(title or "").split())[:100]
    if stype == "connector:calendar":
        m = re.search(r"(?m)^Calendar:\s*(.+)$", text or "")
        cal = (m.group(1).strip() if m else "")
        whose = calendars.get(cal.lower()) or (owner if cal.lower() in ("", owner.lower()) else "")
        dm = re.search(r"(?m)^\w+, (\w{3})\w* (\d{1,2}), \d{4}", text or "")
        when = f", {dm.group(1)} {dm.group(2)}" if dm else ""
        return (f"{whose}'s calendar event: {t}{when}" if whose else
                f"Calendar event on the {cal} calendar: {t}{when}" if cal else f"Calendar event: {t}{when}")
    if stype == "connector:mail":
        fm = re.search(r"(?m)^From:\s*([^<\n]+)", text or "")
        sender = fm.group(1).strip().strip('"')[:40] if fm else ""
        when = _when(md)
        return (f"Email to {owner or 'the household'}" + (f" from {sender}" if sender else "")
                + (f", {when}" if when else "") + f": {t}")
    if stype == "connector:downloads":
        return f"A file {owner + ' ' if owner else ''}downloaded: {t}".replace("  ", " ")
    if stype in ("note", "document", "upload") or "scan" in stype:
        kind = "scan" if "scan" in stype else ("document" if stype == "document" else "note")
        return f"{owner}'s {kind}: {t}" if owner else f"A {kind}: {t}"
    return ""


def enrich(text: str, head: str) -> str:
    return f"{head}\n{text}" if head and not str(text).startswith(head) else text


def _people() -> tuple[dict[str, str], dict[str, str]]:
    import household_identity as hh
    ps = hh.get_store().people()
    names = {str(p["id"]): str(p.get("name") or "") for p in ps}
    cals = {str(c).lower(): _first(p.get("name")) for p in ps for c in (p.get("calendars") or [])}
    return names, cals


def _embed(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 4):    # small batches of short texts: the server's batch limit (10-02: 500)
        body = json.dumps({"input": [t[:1500] for t in texts[i:i + 4]]}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(EMBED_URL, data=body,
                                                                    headers={"Content-Type": "application/json"}),
                                             timeout=120))
        out += [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]
    return out


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b) or 1)


def score(docs: list[str], plain: list[str], questions: list[tuple[str, str, list[str]]],
          embed=_embed) -> dict[str, Any]:
    """hit@1 and hit@3 for each question: a hit is a top item whose own text holds one of the expected words."""
    D = embed(docs)
    Q = embed([q for _, q, _ in questions])
    h1 = h3 = 0
    per = {}
    for (qid, _, words), qe in zip(questions, Q):
        ranked = sorted(range(len(D)), key=lambda i: -_cos(qe, D[i]))[:3]
        ok = [any(w in plain[i].lower() for w in words) for i in ranked]
        h1 += ok[0] if ok else 0
        h3 += any(ok)
        per[qid] = next((r + 1 for r, k in enumerate(ok) if k), None)
    n = len(questions)
    return {"hit@1": f"{h1}/{n}", "hit@3": f"{h3}/{n}", "rank": per}


def _items() -> list[dict[str, Any]]:
    import kb_hygiene
    import kb_query
    out = []
    for s in kb_hygiene.list_sources():
        sid = s.get("source_id") or ""
        if not sid:
            continue
        text, md = kb_query._chunks(sid)
        out.append({"id": sid, "type": str(s.get("source_type") or ""), "title": str(s.get("title") or ""),
                    "text": text, "md": md})
    return out


def compare() -> dict[str, Any]:
    import retrieval_eval
    names, cals = _people()
    items = _items()
    plain = [f"{x['title']}\n{x['text'][:4000]}" for x in items]
    heads = [header(x["type"], x["title"], x["text"], x["md"], names, cals) for x in items]
    rich = [enrich(p, h) for p, h in zip(plain, heads)]
    qs = list(retrieval_eval.FOUND)
    return {"items": len(items), "with_header": sum(bool(h) for h in heads),
            "as_today": score(plain, plain, qs), "with_headers": score(rich, plain, qs)}


def reindex(apply: bool) -> dict[str, Any]:
    """Connector items and notes whose text doesn't start with its header yet. Dry run: counts per type."""
    import collections
    names, cals = _people()
    todo = collections.Counter()
    plan = []
    for x in _items():
        h = header(x["type"], x["title"], x["text"], x["md"], names, cals)
        if h and not x["text"].startswith(h):
            todo[x["type"]] += 1
            plan.append((x, h))
    out = {"would_reindex": dict(todo), "total": sum(todo.values()), "applied": 0}
    if apply:
        import agent_runtime_lib as lib
        for x, h in plan:
            lib._runtime_request("retrieval", "/v1/retrieval/index", "POST", {
                "source_id": x["id"], "parent_record_id": x["id"], "source_type": x["type"], "title": x["title"],
                "text": enrich(x["text"], h), "metadata": x["md"]})
            out["applied"] += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compare")
    r = sub.add_parser("reindex")
    r.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    out = compare() if a.cmd == "compare" else reindex(a.apply)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
