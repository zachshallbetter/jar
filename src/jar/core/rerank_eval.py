#!/usr/bin/env python3
"""A local cross-encoder reranker over knowledge-base hits, measured (titan-node#369). Results only: nothing in
titan's search changes.

Retrieval scores don't separate relevant from irrelevant (an unrelated query still scores ~0.45), so the code
carries word-overlap workarounds (_tidy_hits, the topic-word filter, cited_sources). A cross-encoder reads the
question and each passage together. Two things are measured on titan's real KB, as the admin, read-only:
  ranking      for retrieval_eval's "found" questions, the top 20 hybrid hits reranked: hit@1 and hit@3 before
               and after (a hit: the expected words in the passage);
  calibration  can one reranker score floor separate a real answer from nothing? The right hit's score for
               found questions vs the best score for retrieval_eval's "not stored" questions.

Needs a reranking server (llama-server --reranking --pooling rank) at RERANK_URL, e.g. bge-reranker-v2-m3 Q8_0
on a spare GPU (#369: CPU, or <= 2.5 GB on GPU0/GPU2, outside 03:30-04:30 UTC).

  python3 scripts/rerank_eval.py [--k 20]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RERANK_URL = os.environ.get("RERANK_URL", "http://127.0.0.1:18030/v1/rerank")


# Questions certainly not in a household's KB, for calibration (retrieval_eval's own "not stored" set is small,
# and some of it gets stored over time).
OFF_TOPIC = [("off-capital", "What is the capital of Australia?", ["canberra"]),
             ("off-everest", "How tall is Mount Everest?", ["everest"]),
             ("off-worldcup", "Who won the 1998 World Cup?", ["world cup"]),
             ("off-recipe", "How do I make sourdough starter?", ["sourdough"]),
             ("off-moon", "How far away is the moon?", ["moon"]),
             ("off-python", "How do I reverse a list in Python?", ["reverse"]),
             ("off-tides", "What causes ocean tides?", ["tides"])]


def rerank(query: str, docs: list[str]) -> list[float]:
    body = json.dumps({"query": query, "documents": docs}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(RERANK_URL, data=body,
                                                                headers={"Content-Type": "application/json"}),
                                         timeout=120))
    out = [0.0] * len(docs)
    for x in r.get("results") or []:
        out[x["index"]] = float(x["relevance_score"])
    return out


def _text(h: dict) -> str:
    return f"{h.get('title') or ''}\n{h.get('chunk_text') or h.get('text') or ''}"


def run(k: int = 20) -> dict:
    import agent_runtime_lib as lib
    import kb_privacy
    import retrieval_eval as R
    found, empty = [], []
    t0 = time.time()
    for qid, q, words in R.FOUND:
        hits = kb_privacy.query(lib._runtime_request, q, k, "zach", "admin")
        docs = [_text(h)[:1500] for h in hits]
        ok = [any(w in d.lower() for w in words) for d in docs]
        s = rerank(q, docs) if docs else []
        order = sorted(range(len(docs)), key=lambda i: -s[i])
        found.append({"id": qid, "n": len(docs),
                      "before": next((i + 1 for i, x in enumerate(ok) if x), None),
                      "after": next((r + 1 for r, i in enumerate(order) if ok[i]), None),
                      "right_score": max((s[i] for i in range(len(docs)) if ok[i]), default=None),
                      "best_wrong": max((s[i] for i in range(len(docs)) if not ok[i]), default=None)})
    for qid, q, words in list(R.NOT_STORED) + OFF_TOPIC:
        hits = kb_privacy.query(lib._runtime_request, q, k, "zach", "admin")
        docs = [_text(h)[:1500] for h in hits]
        s = rerank(q, docs) if docs else []
        answers = [any(w in d.lower() for w in words) for d in docs]
        empty.append({"id": qid, "n": len(docs), "best": max(s, default=None),
                      "best_retrieval": max((float(h.get("score") or 0) for h in hits), default=None),
                      "something_answers_it": any(answers)})

    def hit(rows, key, n):
        return sum(1 for r in rows if r[key] is not None and r[key] <= n)
    rights = [r["right_score"] for r in found if r["right_score"] is not None]
    nothing = [e["best"] for e in empty if e["best"] is not None and not e["something_answers_it"]]
    sep = None
    if rights and nothing and min(rights) > max(nothing):
        sep = round((min(rights) + max(nothing)) / 2, 2)
    return {"k": k, "seconds": round(time.time() - t0, 1),
            "hit@1": {"before": hit(found, "before", 1), "after": hit(found, "after", 1), "of": len(found)},
            "hit@3": {"before": hit(found, "before", 3), "after": hit(found, "after", 3), "of": len(found)},
            "calibration": {"lowest_right_answer": min(rights) if rights else None,
                            "highest_not_stored": max(nothing) if nothing else None,
                            "one_floor_separates": sep is not None, "floor": sep},
            "found": found, "not_stored": empty}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--k", type=int, default=20)
    a = ap.parse_args()
    print(json.dumps(run(a.k), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
