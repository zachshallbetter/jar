#!/usr/bin/env python3
"""Embeddings on real household data (titan-node#374 step 1): nomic-embed-text-v1.5, as deployed, against
Qwen3-Embedding-0.6B, on questions whose answers are in titan's own knowledge base. Report only: switching is
Zach's decision, with these numbers in front of him (the 09-29 run, docs/evals/embeddings-2026-09-29.md, used 28
doc notes and 24 hand-written questions).

Questions, built on titan and kept in memory (the eval bank's stated facts, #593, wait for a content-free path
agreed with its owner session, 10-02):
  likely   for a sample of household items (not titan's own notes), a local model (--writer-url, default
           Qwen3.6-35B-A3B on :8198) writes the question
           a household member would ask that the item answers (doc2query); its right items are it and any
           item with the same title (a calendar series)
Every KB item (eval items aside) is a candidate, embedded as its title plus its first 1,500 characters, four
at a time (the embedding server has 2048 tokens per slot, #374's comment). Ranked by cosine similarity:
hit@1, hit@3 and MRR per model and set. Counts only: no question, answer or title is printed or stored.

A candidate model needs its own llama-server on a free GPU, e.g.
  CUDA_VISIBLE_DEVICES=0 /ai/tools/llama.cpp-b11259/llama-server --port 18016 \
    --model /ai/models/qwen3-embedding-0.6b/Qwen3-Embedding-0.6B-Q8_0.gguf \
    --embedding --pooling last --ctx-size 8192 --parallel 4 --batch-size 2048 --ubatch-size 2048

  python3 scripts/embed_eval_household.py [--likely 80] [--qwen-url http://127.0.0.1:18016]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ops_ports import CODE_MODEL_URL  # noqa: E402

DOC_CHARS = 1500
BATCH = 4
WRITER_URL = CODE_MODEL_URL   # titan-code-model (Qwen3.6-35B-A3B, GPU0+GPU2): writes the likely questions
QWEN_INSTRUCT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"


def norm(t: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(t).lower()))


def embed(url: str, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        body = json.dumps({"input": texts[i:i + BATCH]}).encode()
        req = urllib.request.Request(url.rstrip("/") + "/v1/embeddings", data=body,
                                     headers={"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req, timeout=180))
        out += [d["embedding"] for d in sorted(r["data"], key=lambda d: d["index"])]
    return out


def _cos(a: list[float], b: list[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def score(docs: list[list[float]], queries: list[list[float]], targets: list[set[int]],
          owner: list[int] | None = None) -> dict[str, Any]:
    """hit@1, hit@3, MRR: where the first right item lands when every item is ranked by cosine. With `owner`
    (the item each vector belongs to: doc2query adds vectors per item), an item scores its best vector."""
    h1 = h3 = 0
    mrr = 0.0
    for qe, right in zip(queries, targets):
        if owner is None:
            order = sorted(range(len(docs)), key=lambda i: -_cos(qe, docs[i]))
        else:
            best: dict[int, float] = {}
            for v, i in zip(docs, owner):
                c = _cos(qe, v)
                if c > best.get(i, -2.0):
                    best[i] = c
            order = sorted(best, key=lambda i: -best[i])
        r = next((k + 1 for k, i in enumerate(order) if i in right), None)
        if r is None:
            continue
        h1 += r == 1
        h3 += r <= 3
        mrr += 1 / r
    n = len(targets)
    return {"n": n, "hit@1": h1, "hit@3": h3, "mrr": round(mrr / n, 3) if n else None}



LIKELY = """/no_think
This is an item in a household's knowledge base:

{item}

Write the one short question a member of the household would ask that this item answers, in their own everyday
words (not copying its title). Return JSON only: {{"q": "..."}}"""


INDEXED = """/no_think
Here is an item from a household's knowledge base:

{item}

List three different short questions this item answers, worded the way different people might search for it
(a name, a date, a place, a plain description). Return JSON only: {{"qs": ["...", "...", "..."]}}"""


def _chat(prompt: str, url: str, max_tokens: int = 400) -> dict[str, Any] | None:
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens,
                       "temperature": 0, "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=120))
    raw = re.sub(r"<think>.*?</think>", "", r["choices"][0]["message"]["content"], flags=re.S)
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else None


def index_questions(text: str, url: str = WRITER_URL) -> list[str]:
    """doc2query (#371): the questions indexed with an item. A different prompt from the test questions' (LIKELY),
    so what's indexed never is what's asked."""
    d = _chat(INDEXED.format(item=text[:DOC_CHARS]), url) or {}
    qs = d.get("qs") if isinstance(d.get("qs"), list) else []
    return [str(q).strip() for q in qs if str(q).strip()][:3]


def local_question(text: str, url: str = WRITER_URL) -> str | None:
    body = json.dumps({"messages": [{"role": "user", "content": LIKELY.format(item=text[:DOC_CHARS])}],
                       "max_tokens": 400, "temperature": 0, "response_format": {"type": "json_object"}}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=120))
    raw = re.sub(r"<think>.*?</think>", "", r["choices"][0]["message"]["content"], flags=re.S)
    m = re.search(r"\{.*\}", raw, re.S)
    q = str(json.loads(m.group(0)).get("q") or "").strip() if m else ""
    return q or None


def likely_targets(items: list[dict[str, Any]], n: int, ask: Callable[[str], str | None],
                   seed: int = 0, shown: Callable[[dict[str, Any]], str] = lambda it: it["text"]) -> list[tuple[str, set[int]]]:
    """(question, right items) for a sample of household items: it, and any item with the same title. `shown`:
    what the writer reads of each item (its text; with --headers, its context line too)."""
    pool = [i for i, it in enumerate(items) if not it["system"]]
    random.Random(seed).shuffle(pool)
    by_title: dict[str, set[int]] = {}
    for i, it in enumerate(items):
        by_title.setdefault(norm(it["title"]), set()).add(i)
    out = []
    for i in pool[:n]:
        try:
            q = ask(shown(items[i]))
        except Exception:  # noqa: BLE001
            q = None
        if q:
            out.append((q, by_title.get(norm(items[i]["title"]), set()) | {i}))
    return out


def kb_corpus() -> list[dict[str, Any]]:
    """Every KB item but eval items: title, text (title + first DOC_CHARS), whether it's titan's own note, and
    its context header (kb_enrich, #371; empty for titan's own records)."""
    import kb_enrich
    import kb_hygiene
    import kb_privacy
    import kb_query
    names, cals = kb_enrich._people()
    out = []
    for s in kb_hygiene.list_sources():
        text, md = kb_query._chunks(s.get("source_id") or "")
        if not text or md.get("eval_run"):
            continue
        title = str(s.get("title") or "")
        out.append({"title": title, "text": f"{title}\n{text}"[:DOC_CHARS + len(title) + 1],
                    "system": kb_privacy.system_meta(md),
                    "head": kb_enrich.header(str(s.get("source_type") or ""), title, text, md, names, cals)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--likely", type=int, default=80)
    ap.add_argument("--nomic-url", default="http://127.0.0.1:8015")
    ap.add_argument("--qwen-url", default="http://127.0.0.1:18016")
    ap.add_argument("--writer-url", default=WRITER_URL)
    ap.add_argument("--doc2query", action="store_true",
                    help="#371: nomic as today against items plus their indexed questions (own vectors, and appended)")
    ap.add_argument("--headers", action="store_true",
                    help="#371: nomic as today against nomic with kb_enrich's context header prepended, in memory")
    a = ap.parse_args()
    items = kb_corpus()
    if a.headers:
        return headers_report(items, a)
    if a.doc2query:
        return doc2query_report(items, a)
    sets = {"likely": likely_targets(items, a.likely, lambda t: local_question(t, a.writer_url))}
    models = {"nomic (as deployed)": (a.nomic_url, lambda d: d, lambda q: q),
              "qwen3-0.6b (official instruction)": (a.qwen_url, lambda d: d, lambda q: QWEN_INSTRUCT + q)}
    report: dict[str, Any] = {"items": len(items), "household_items": sum(not it["system"] for it in items),
                              "questions": {k: len(v) for k, v in sets.items()}, "writer": a.writer_url, "results": {}}
    for name, (url, fd, fq) in models.items():
        try:
            D = embed(url, [fd(it["text"]) for it in items])
        except Exception as exc:  # noqa: BLE001
            report["results"][name] = {"error": type(exc).__name__}
            continue
        res = {}
        for sname, qs in sets.items():
            if qs:
                res[sname] = score(D, embed(url, [fq(q) for q, _ in qs]), [t for _, t in qs])
        report["results"][name] = res
    print(json.dumps(report, indent=1))
    return 0


def headers_report(items: list[dict[str, Any]], a: argparse.Namespace) -> int:
    """#371: the same corpus embedded as today and with each item's context header first (never the live index).
    Two question sets: "likely" (the writer reads the item, as #374's run) and "likely-whose" (it reads the
    header too, so it can ask the way a household member who knows whose it is would)."""
    ask = lambda t: local_question(t, a.writer_url)   # noqa: E731
    sets = {"likely": likely_targets(items, a.likely, ask),
            "likely-whose": likely_targets(items, a.likely, ask,
                                           shown=lambda it: (it["head"] + "\n" if it["head"] else "") + it["text"])}
    variants = {"as today": [it["text"] for it in items],
                "with headers": [(it["head"] + "\n" if it["head"] else "") + it["text"] for it in items]}
    report: dict[str, Any] = {"items": len(items), "with_header": sum(bool(it["head"]) for it in items),
                              "questions": {k: len(v) for k, v in sets.items()}, "writer": a.writer_url,
                              "embedder": a.nomic_url, "results": {}}
    Q = {k: embed(a.nomic_url, [q for q, _ in v]) for k, v in sets.items() if v}
    for name, docs in variants.items():
        D = embed(a.nomic_url, docs)
        report["results"][name] = {k: score(D, Q[k], [t for _, t in sets[k]]) for k in Q}
    print(json.dumps(report, indent=1))
    return 0


def doc2query_report(items: list[dict[str, Any]], a: argparse.Namespace) -> int:
    """#371's second item, in memory only (never the live index). Every item gets up to three indexed questions
    (INDEXED prompt); the test questions are the LIKELY sets, written apart and never indexed. Three variants:
    as today; each question its own vector (an item scores its best: the index grows); the questions appended to
    the item's text (one vector each, still under a slot). Plus the index's growth and the time to build it."""
    import time as _t
    ask = lambda t: local_question(t, a.writer_url)   # noqa: E731
    sets = {"likely": likely_targets(items, a.likely, ask),
            "likely-whose": likely_targets(items, a.likely, ask,
                                           shown=lambda it: (it["head"] + "\n" if it["head"] else "") + it["text"])}
    t0 = _t.monotonic()
    idx: list[list[str]] = []
    for it in items:
        try:
            idx.append(index_questions(it["text"], a.writer_url))
        except Exception:  # noqa: BLE001
            idx.append([])
    gen_s = _t.monotonic() - t0
    tested = {norm(q) for v in sets.values() for q, _ in v}
    overlap = sum(norm(q) in tested for qs in idx for q in qs)
    Q = {k: embed(a.nomic_url, [q for q, _ in v]) for k, v in sets.items() if v}
    t0 = _t.monotonic()
    D = embed(a.nomic_url, [it["text"] for it in items])
    plain_s = _t.monotonic() - t0
    flat = [(i, q) for i, qs in enumerate(idx) for q in qs]
    t0 = _t.monotonic()
    QV = embed(a.nomic_url, [q for _, q in flat])
    qvec_s = _t.monotonic() - t0
    appended = [it["text"][:DOC_CHARS] + ("\nQuestions it answers: " + " ".join(qs) if qs else "")
                for it, qs in zip(items, idx)]
    t0 = _t.monotonic()
    DA = embed(a.nomic_url, appended)
    app_s = _t.monotonic() - t0
    owner = list(range(len(items))) + [i for i, _ in flat]
    report: dict[str, Any] = {
        "items": len(items), "indexed_questions": len(flat), "items_without": sum(not qs for qs in idx),
        "overlap_with_tests": overlap, "questions": {k: len(v) for k, v in sets.items()}, "writer": a.writer_url,
        "embedder": a.nomic_url,
        "cost": {"vectors_today": len(D), "vectors_own": len(D) + len(QV), "generate_s": round(gen_s),
                 "embed_today_s": round(plain_s, 1), "embed_questions_s": round(qvec_s, 1),
                 "embed_appended_s": round(app_s, 1),
                 "chars_today": sum(len(it["text"]) for it in items), "chars_appended": sum(len(t) for t in appended)},
        "results": {"as today": {k: score(D, Q[k], [t for _, t in sets[k]]) for k in Q},
                    "questions as own vectors": {k: score(D + QV, Q[k], [t for _, t in sets[k]], owner) for k in Q},
                    "questions appended": {k: score(DA, Q[k], [t for _, t in sets[k]]) for k in Q}}}
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
