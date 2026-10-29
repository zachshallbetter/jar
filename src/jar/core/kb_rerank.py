#!/usr/bin/env python3
"""A local cross-encoder over knowledge-base hits, for an explicit search (titan-node#369; measured in
docs/evals/reranker-2026-10-02.md). Off unless KB_RERANK=1.

kb_retrieve's top 20 hybrid hits are scored by bge-reranker-v2-m3 (titan-reranker.service, GPU2,
127.0.0.1:18030) reading the question and each passage together; one floor (KB_RERANK_FLOOR, -2.8) replaces
the retrieval score floor and the topic-word filter for that path. If the reranker can't be reached, the
search falls back to today's path: a search never fails because of it. The per-turn context is unchanged.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

RERANK_URL = os.environ.get("KB_RERANK_URL", "http://127.0.0.1:18030/v1/rerank")
FETCH = 20
PASSAGE_CHARS = 1500


def enabled() -> bool:
    return os.environ.get("KB_RERANK", "0").strip().lower() in ("1", "true", "yes", "on")


def floor() -> float:
    try:
        return float(os.environ.get("KB_RERANK_FLOOR", "-2.8"))
    except ValueError:
        return -2.8


def _text(h: dict[str, Any]) -> str:
    return f"{h.get('title') or ''}\n{h.get('chunk_text') or h.get('text') or ''}"[:PASSAGE_CHARS]


def rerank(query: str, hits: list[dict[str, Any]], timeout: float = 8) -> list[dict[str, Any]]:
    """The hits, each with its rerank_score, best first. Raises when the reranker can't be reached."""
    if not hits:
        return []
    body = json.dumps({"query": query, "documents": [_text(h) for h in hits]}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(RERANK_URL, data=body,
                                                                headers={"Content-Type": "application/json"}),
                                         timeout=timeout))
    scores = {x["index"]: float(x["relevance_score"]) for x in r.get("results") or []}
    if len(scores) != len(hits):
        raise RuntimeError("reranker returned a partial result")
    out = [{**h, "rerank_score": round(scores[i], 3)} for i, h in enumerate(hits)]
    return sorted(out, key=lambda h: -h["rerank_score"])
