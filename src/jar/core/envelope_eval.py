#!/usr/bin/env python3
"""Item envelopes against the full text they'd replace (titan-node#390): report only, nothing in the KB changes.

For a sample of real synced items (mail, downloads, browsing, notes; not calendar, which is structured and read
live), titan's own model writes each one's envelope (envelopes.build) into a scratch store. Then, the KB as it is
against the same KB with those items' text replaced by their envelopes:
  likely       one question per sampled item, written from its FULL text by a local model (embed_eval_household's
               LIKELY prompt), so a summary that drops what people ask for loses: hit@1, hit@3, MRR (nomic, as
               deployed);
  found        retrieval_eval's ten "found" questions, ranked the same way: whether a top-3 item holds the words;
  size         characters per sampled item, before and after (and the share of the KB they are);
  checks       summaries refused by the number check (fell back to the title), and how many sampled items touch
               a fence their summary alone wouldn't (the reason envelopes carry the original's fences).
Counts only: no question, title, summary or text is printed. The scratch store (mode 600) holds the envelopes for
review; delete it after.

Local only: the summariser and the question writer are titan's own models (:8010 by default, or :8198); the
embeddings are nomic (:8015). Run on titan with agent.env loaded:
  python3 scripts/envelope_eval.py [--sample 60] [--url http://127.0.0.1:8010] [--writer-url …] [--types …] [--store PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

SAMPLED_TYPES = ("connector:mail", "connector:downloads", "connector:browsing", "connector:messages", "note")


def corpus() -> list[dict[str, Any]]:
    """Every KB item but eval items, as embed_eval_household reads them, plus id, type and metadata."""
    import embed_eval_household as eh
    import kb_hygiene
    import kb_privacy
    import kb_query
    out = []
    for s in kb_hygiene.list_sources():
        sid = s.get("source_id") or ""
        text, md = kb_query._chunks(sid)
        if not text or md.get("eval_run"):
            continue
        title = str(s.get("title") or "")
        out.append({"source_id": sid, "source_type": str(s.get("source_type") or md.get("source_type") or ""),
                    "title": title, "raw": text, "metadata": md, "system": kb_privacy.system_meta(md),
                    "text": f"{title}\n{text}"[:eh.DOC_CHARS + len(title) + 1]})
    return out


def found_check(items: list[dict[str, Any]], docs: list[list[float]], url: str) -> dict[str, int]:
    """retrieval_eval's found questions: pass when a top-3 item (by cosine) holds one of the question's words."""
    import embed_eval_household as eh
    import retrieval_eval as re_
    qs = eh.embed(url, [q for _, q, _ in re_.FOUND])
    ok = 0
    for qe, (_, _, words) in zip(qs, re_.FOUND):
        top = sorted(range(len(docs)), key=lambda i: -eh._cos(qe, docs[i]))[:3]
        ok += any(any(w in items[i]["text"].lower() for w in words) for i in top)
    return {"n": len(re_.FOUND), "pass": ok}


def main() -> int:
    import embed_eval_household as eh
    import envelopes
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--url", default=envelopes.SUMMARISER_URL, help="the summariser (local)")
    ap.add_argument("--writer-url", default=None, help="the question writer (local; default: --url)")
    ap.add_argument("--nomic-url", default="http://127.0.0.1:8015")
    ap.add_argument("--store", type=Path, default=Path(f"/ai/tmp/envelopes-{time.strftime('%Y%m%d-%H%MZ', time.gmtime())}.jsonl"))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--types", default=",".join(SAMPLED_TYPES),
                    help="which synced item types to sample, comma-separated (e.g. connector:mail for a mail-heavy run)")
    a = ap.parse_args()
    items = corpus()
    types = tuple(t.strip() for t in a.types.split(",") if t.strip())
    pool = [i for i, it in enumerate(items) if not it["system"] and it["source_type"] in types]
    random.Random(a.seed).shuffle(pool)
    sample = pool[:a.sample]

    a.store.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(a.store, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    envs: dict[int, dict[str, Any]] = {}
    t0 = time.time()
    with os.fdopen(fd, "w") as fh:
        for i in sample:
            it = items[i]
            env = envelopes.build({"source_id": it["source_id"], "source_type": it["source_type"], "title": it["title"],
                                   "text": it["raw"], "metadata": it["metadata"]}, url=a.url)
            envs[i] = env
            fh.write(json.dumps(env) + "\n")
    secs = time.time() - t0

    after = [dict(it) for it in items]
    for i, env in envs.items():
        after[i]["text"] = envelopes.index_text(env)

    # Questions from the full text, for the sampled items only (what a summary has to keep).
    by_title: dict[str, set[int]] = {}
    for i, it in enumerate(items):
        by_title.setdefault(eh.norm(it["title"]), set()).add(i)
    qs = []
    for i in sample:
        try:
            q = eh.local_question(items[i]["text"], a.writer_url or a.url)
        except Exception:  # noqa: BLE001
            q = None
        if q:
            qs.append((q, by_title.get(eh.norm(items[i]["title"]), set()) | {i}))

    report: dict[str, Any] = {"items": len(items), "sampled": len(sample),
                              "by_type": {t: sum(items[i]["source_type"] == t for i in sample) for t in types},
                              "writer": a.writer_url or a.url,
                              "summariser": a.url, "summarise_s_per_item": round(secs / max(1, len(sample)), 1),
                              "questions": len(qs), "store": str(a.store)}
    before_chars = [len(items[i]["raw"]) for i in sample]
    after_chars = [len(after[i]["text"]) for i in sample]
    total = sum(len(it["raw"]) for it in items) or 1
    report["size"] = {"chars_before_median": int(statistics.median(before_chars)) if before_chars else 0,
                      "chars_after_median": int(statistics.median(after_chars)) if after_chars else 0,
                      "chars_before_total": sum(before_chars), "chars_after_total": sum(after_chars),
                      "sampled_share_of_kb": round(sum(before_chars) / total, 3)}
    report["checks"] = {
        "summaries_from_model": sum(e["interpretation"]["how"] == "model" for e in envs.values()),
        "fell_back_to_title": sum(e["interpretation"]["how"] == "title" for e in envs.values()),
        "touch_a_fence": sum(bool(e["fences"]["touched"]) for e in envs.values()),
    }
    try:
        import fences
        fs = fences.get()
        report["checks"]["fence_missed_by_summary_alone"] = sum(
            1 for e in envs.values() if e["fences"]["touched"]
            and sorted(int(f["id"]) for f in fs.touches(envelopes.index_text(e))) != e["fences"]["touched"])
    except Exception as exc:  # noqa: BLE001
        report["checks"]["fence_missed_by_summary_alone"] = f"n/a ({type(exc).__name__})"

    try:
        D0 = eh.embed(a.nomic_url, [it["text"] for it in items])
        D1 = list(D0)
        changed = sorted(envs)
        for i, v in zip(changed, eh.embed(a.nomic_url, [after[i]["text"] for i in changed])):
            D1[i] = v
        Q = eh.embed(a.nomic_url, [q for q, _ in qs]) if qs else []
        report["likely"] = {"full_text": eh.score(D0, Q, [t for _, t in qs]),
                            "envelopes": eh.score(D1, Q, [t for _, t in qs])}
        report["found"] = {"full_text": found_check(items, D0, a.nomic_url),
                           "envelopes": found_check(after, D1, a.nomic_url)}
    except Exception as exc:  # noqa: BLE001
        report["retrieval_error"] = type(exc).__name__
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
