#!/usr/bin/env python3
"""Per-type indexing, measured before it's built (#371, Zach's KB improvements #4, 10-04): the KB's items chunked
four ways, embedded in memory on titan's embedding server, and scored on the real-question bank (kb_bank.py, #862).
Nothing is written to the KB.

  V0  today: every item through the runtime's own chunker (paragraphs, ≤1200 chars)
  V1  by type: mail whole; saved pages and notes by section; the runbook by sub-heading; calendar as today
  V2  V1 + a context header on every chunk (kb_enrich.header: what it is, whose)
  V3  V2 + likely questions (local model, doc2query) as one more chunk per item
  V0Q V0 + the item's title over its likely questions, one more chunk (no headers; #371 after they were withdrawn)
  V1Q V1 + the same
  V0Qsys V0 + the questions chunk for system items only (the runbook gained, mail lost)

A case is a hit when an item it expects is in the top 3 (an item scores its best chunk). Vector similarity only (the
runtime blends in a lexical score), so the variants compare with each other, not with the nightly numbers.

  python3 scripts/kb_index_ab.py [--questions-cache /ai/tmp/kb-likely-questions.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

_PARA = re.compile(r"\n\s*\n")
_HEADING = re.compile(r"(?m)^(?:#{1,6}\s+.+|\*\*[^*\n]{2,80}\*\*:?\s*|[A-Z][^\n.!?]{2,60}:)\s*$")


def chunk_text(text: str, max_chars: int = 1200) -> list[str]:
    """retrieval-runtime's chunker (src/domain/ingestion.rs): paragraphs merged up to max_chars, long ones cut."""
    out, buf = [], []
    for p in (x.strip() for x in _PARA.split(str(text or "")) if x.strip()):
        if len(p) > max_chars:
            if buf:
                out.append("\n\n".join(buf))
                buf = []
            out += [p[i:i + max_chars].strip() for i in range(0, len(p), max_chars)]
            continue
        if len("\n\n".join(buf + [p])) <= max_chars:
            buf.append(p)
        else:
            if buf:
                out.append("\n\n".join(buf))
            buf = [p]
    if buf:
        out.append("\n\n".join(buf))
    return out


def sections(text: str) -> list[str]:
    """Split at heading lines (markdown, bold, "Title:" lines), each section keeping its heading."""
    t = str(text or "")
    cuts = [m.start() for m in _HEADING.finditer(t)]
    if not cuts:
        return [t]
    parts = ([t[:cuts[0]]] if cuts[0] > 0 else []) + [t[a:b] for a, b in zip(cuts, cuts[1:] + [len(t)])]
    return [p.strip() for p in parts if p.strip()]


def by_type(kind: str, text: str) -> list[str]:
    if kind == "mail":
        return [str(text or "")[:6000]]
    if kind in ("web", "facts"):
        return [c for s in sections(text) for c in chunk_text(s, 800)]
    if kind == "system":
        return [c for s in sections(text) for c in chunk_text(s, 600)]
    return chunk_text(text, 1200)


def likely_questions(text: str, url: str) -> list[str]:
    prompt = ("Write three short questions a person in this household might ask that this text answers. One per line, "
              "no numbering.\n\n" + str(text or "")[:1500] + "\n/no_think")
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 120, "temperature": 0}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(url + "/v1/chat/completions", data=body,
                                                                headers={"Content-Type": "application/json"}), timeout=120))
    out = re.sub(r"(?s)<think>.*?</think>", "", r["choices"][0]["message"]["content"])
    return [q.strip(" -•\t") for q in out.splitlines() if q.strip().endswith("?")][:3]


def run(items: list[dict[str, Any]], cases: list[dict[str, Any]], embed: Callable[[list[str]], list[list[float]]],
        questions: dict[str, list[str]]) -> dict[str, Any]:
    import kb_enrich
    variants: dict[str, list[tuple[str, str]]] = {"V0": [], "V1": [], "V2": [], "V3": [], "V0Q": [], "V1Q": [], "V0Qsys": [],
                                                  "V0Qsys-app": []}
    qchunks: set[str] = set()   # the likely-questions chunks, so an explained case can say which chunk won
    for it in items:
        sid, kind, text, head = it["source_id"], it["kind"], it["text"], it["header"] or it["title"]
        variants["V0"] += [(sid, c) for c in chunk_text(text)]
        typed = by_type(kind, text)
        variants["V1"] += [(sid, c) for c in typed]
        variants["V2"] += [(sid, kb_enrich.enrich(c, head)) for c in typed]
        variants["V3"] += [(sid, kb_enrich.enrich(c, head)) for c in typed]
        variants["V0Q"] += [(sid, c) for c in chunk_text(text)]
        variants["V0Qsys"] += [(sid, c) for c in chunk_text(text)]
        if not (questions.get(sid) and kind == "system"):
            variants["V0Qsys-app"] += [(sid, c) for c in chunk_text(text)]
        variants["V1Q"] += [(sid, c) for c in typed]
        if questions.get(sid):
            variants["V3"].append((sid, f"{head}\n" + "\n".join(questions[sid])))
            # #371 without headers (withdrawn 10-05): the item's own title over its likely questions, one more chunk
            qchunk = (f"{it['title']}\n" if it["title"] else "") + "\n".join(questions[sid])
            variants["V0Q"].append((sid, qchunk))
            variants["V1Q"].append((sid, qchunk))
            if kind == "system":   # only the runbook and system notes, where the questions helped
                variants["V0Qsys"].append((sid, qchunk))
                # what production can index (the runtime merges paragraphs up to 1200 chars): the questions appended
                # to the note's own text, chunked as one text
                variants["V0Qsys-app"] += [(sid, c) for c in chunk_text(text + "\n\n" + qchunk)]
            qchunks.add(qchunk)
    Q = embed([c["q"] for c in cases])
    out: dict[str, Any] = {}
    hits_by: dict[str, dict[Any, tuple]] = {}
    for name, chunks in variants.items():
        E = embed([c for _, c in chunks])
        by: dict[str, list[int]] = {}
        r5 = rr = 0.0
        for case, qe in zip(cases, Q):
            best: dict[str, float] = {}
            via: dict[str, bool] = {}
            for (sid, ch), ce in zip(chunks, E):
                s = kb_enrich._cos(qe, ce)
                if s > best.get(sid, -9):
                    best[sid] = s
                    via[sid] = ch in qchunks
            if os.environ.get("KB_AB_EXPLAIN") == case.get("id"):   # one case: ids, scores, and whether a question
                print(f"  {name} {case.get('id')}: expect {case['expect']} top5 " + ", ".join(   # chunk won the source
                    f"{k}{'(Q)' if via.get(k) else ''} {best[k]:.3f}" for k in sorted(best, key=lambda k: -best[k])[:5]),
                    file=sys.stderr)
            ranked = sorted(best, key=lambda k: -best[k])
            top = ranked[:3]
            hit = bool(set(top) & set(case["expect"]))
            first = next((i + 1 for i, k in enumerate(ranked[:10]) if k in case["expect"]), None)
            r5 += bool(first and first <= 5)
            rr += 1.0 / first if first else 0.0
            hits_by.setdefault(name, {})[case.get("id")] = (hit, case["kind"], case.get("origin"))
            for key in (case["kind"], f"origin:{case.get('origin', '?')}"):
                b = by.setdefault(key, [0, 0])
                b[1] += 1
                b[0] += hit
        kinds = {k: v for k, v in by.items() if not k.startswith("origin:")}
        out[name] = {"chunks": len(chunks), "by_kind": by, "found": sum(v[0] for v in kinds.values()),
                     "cases": sum(v[1] for v in kinds.values()), "recall5": int(r5),
                     "mrr": round(rr / max(1, len(cases)), 3)}
    if os.environ.get("KB_AB_DIFF"):   # which cases a variant flips against V0: ids, kind and origin only
        base = hits_by.get("V0", {})
        for name, hs in hits_by.items():
            flips = [(cid, k, o, "won" if h else "lost") for cid, (h, k, o) in hs.items() if h != base.get(cid, (h,))[0]]
            if flips and name != "V0":
                print(f"  {name} vs V0: " + "; ".join(f"{cid} {k}/{o} {w}" for cid, k, o, w in flips), file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--questions-cache", default="/ai/tmp/kb-likely-questions.json")
    ap.add_argument("--body-questions", action="store_true", help="generated cases asked in body words, not title words")
    ap.add_argument("--llm", default=os.environ.get("KB_DOC2QUERY_URL", "http://127.0.0.1:8010"))
    a = ap.parse_args()
    import agent_runtime_lib as lib
    import kb_bank
    import kb_enrich
    import kb_privacy as P
    import zones
    names, cals = kb_enrich._people()
    items = []
    for s in lib.kb_sources():
        sid = str(s["source_id"])
        md = P.source_meta(lib._runtime_request, sid)
        text = P.source_text(lib._runtime_request, sid) or ""
        z = zones.zone_of(md, sid, s.get("source_type"))
        items.append({"source_id": sid, "kind": kb_bank.kind_of(z, md, sid), "text": text, "title": s.get("title") or "",
                      "header": kb_enrich.header(str(s.get("source_type") or ""), s.get("title") or "", text, md, names, cals)})
    cache = Path(a.questions_cache)
    qs = json.loads(cache.read_text()) if cache.exists() else {}
    for it in items:
        if it["source_id"] not in qs and it["text"].strip():
            try:
                qs[it["source_id"]] = likely_questions(it["text"], a.llm)
            except Exception as exc:  # noqa: BLE001
                print(f"(doc2query skipped for one item: {type(exc).__name__})", file=sys.stderr)
    cache.write_text(json.dumps(qs))
    os.chmod(cache, 0o600)
    cases = kb_bank.load()
    have = {c.get("id") for c in cases}   # the real-shaped runbook cases (#1001), resolved against today's KB
    cases += [c for c in kb_bank.curated([{"source_id": it["source_id"], "title": it["title"]} for it in items])
              if c.get("id") not in have]
    if a.body_questions:
        # the bank's generated questions use title words, which a header repeats: rebuild them from the item's body
        # (its words that aren't in its title), so a header can't win by echoing the question
        text = {it["source_id"]: it for it in items}
        for c in cases:
            it = text.get(c["expect"][0])
            if c.get("origin") != "generated" or not it:
                continue
            tw = {w.lower() for w in re.findall(r"[A-Za-z']+", it["title"])}
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", it["text"][:800])
                     if w.lower() not in tw and w.lower() not in kb_bank._STOP and w.lower() not in ("from", "subject", "date")]
            if len(words) >= 2:
                c["q"] = c["q"].replace(kb_bank.salient(it["title"]), " ".join(dict.fromkeys(words[2:5])))
    r = run(items, cases, kb_enrich._embed, qs)
    for name, v in r.items():
        print(f"{name}: hit@3 {v['found']}/{v['cases']} recall@5 {v.get('recall5')} MRR {v.get('mrr')} ({v['chunks']} chunks) · " +
              " · ".join(f"{k} {f}/{n}" for k, (f, n) in sorted(v["by_kind"].items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
