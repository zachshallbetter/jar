#!/usr/bin/env python3
"""Topic vectors for the linker (titan-node#951 (e)): measured, normalised against this house's own items, and
candidate-only.

An item's topic is its original's title plus the start of its body (#954: never a summary), embedded by the fleet's
local nomic model (:8015, the same URL, model and input cap as agent_memory; nothing leaves titan). Only the vector is
kept (links.db `topics`), never the text. Two items are topic neighbours when each is in the other's TOP_K and their
similarity stands out from each item's own similarities to everything else by Z_MIN deviations: normalised against
this house's corpus, not a fixed cosine (a global percentile alone is swamped by templated receipts). The why shows the
pair's percentile among all the house's pairs.

Phase 1 (this): topic adds a measured score to edges, and topic-only pairs become candidates ("possibly related"),
never threads. Phase 2 (LINK_TOPIC_ACCEPT=1, off) waits for topic_eval's precision on titan and the owner's call.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
from typing import Any, Callable, Iterable

EMBED_URL = os.environ.get("AGENT_EMBED_URL", "http://127.0.0.1:8015/v1/embeddings")
EMBED_MODEL = os.environ.get("AGENT_EMBED_MODEL", "nomic-embed-text-v1.5")
MAX_CHARS = 1500
TOP_K = 3
PCT = 0.99           # kept for the why: a pair's percentile among all the house's pairs
Z_MIN = 3.0          # the bar: a pair stands out from each item's own similarities by at least this many deviations

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (item_id TEXT PRIMARY KEY, vec BLOB NOT NULL, model TEXT NOT NULL, made_at REAL NOT NULL);
"""


_HEADER_LINE = re.compile(r"(?i)^(?:from|to|cc|bcc|date|subject|message-id|in-reply-to|references|reply-to|sender):")


def topic_text(title: str, text: str) -> str:
    """The title and the start of the body, without the mail's header lines: two mails from one sender shouldn't look
    alike for their From and Date (JEV, 10-05). Hooks still read the headers; only the topic skips them."""
    lines = str(text or "").splitlines()
    k = 0
    in_head = False
    while k < len(lines):
        ln = lines[k]
        if _HEADER_LINE.match(ln.strip()):
            in_head = True
        elif in_head and ln[:1] in (" ", "\t") and ln.strip():
            pass                                   # a folded header line (a long References)
        elif in_head and not ln.strip():
            k += 1
            break                                  # the blank line that ends the headers
        else:
            break
        k += 1
    body = "\n".join(lines[k:]).strip()
    return (f"{title}\n{body}" if title else body)[:MAX_CHARS].strip()


def enabled() -> bool:
    """LINK_TOPICS (on by default on titan; the deploy gate pins it off, so tests never call the live model)."""
    return os.environ.get("LINK_TOPICS", "1") != "0"


def embed(texts: list[str]) -> list[list[float]] | None:
    """The local embeddings model, batched. None when it can't be reached or LINK_TOPICS=0: no topic, nothing fails."""
    if not texts:
        return []
    if not enabled():
        return None
    try:
        body = json.dumps({"input": [t[:MAX_CHARS] or " " for t in texts], "model": EMBED_MODEL}).encode()
        req = urllib.request.Request(EMBED_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            rows = json.loads(r.read())["data"]
        vecs = [row["embedding"] for row in rows]
        return vecs if len(vecs) == len(texts) else None
    except Exception:  # noqa: BLE001
        return None


def put(con: sqlite3.Connection, item_id: str, vec: list[float], model: str = EMBED_MODEL) -> None:
    import numpy as np
    con.executescript(SCHEMA)
    con.execute("INSERT OR REPLACE INTO topics VALUES (?,?,?,?)",
                (item_id, np.asarray(vec, dtype=np.float32).tobytes(), model, time.time()))
    con.commit()


def index_text(con: sqlite3.Connection, item_id: str, title: str, text: str,
               embedder: Callable[[list[str]], list[list[float]] | None] | None = None) -> bool:
    """One item's topic vector, from its original. → stored or not."""
    t = topic_text(title, text)
    if not t:
        return False
    vecs = (embedder or embed)([t])
    if not vecs:
        return False
    put(con, item_id, vecs[0])
    return True


def load(con: sqlite3.Connection) -> dict[str, Any]:
    import numpy as np
    con.executescript(SCHEMA)
    return {r[0]: np.frombuffer(r[1], dtype=np.float32) for r in con.execute("SELECT item_id, vec FROM topics")}


def neighbours(vecs: dict[str, Any], ids: Iterable[str] | None = None, top_k: int = TOP_K,
               pct: float = PCT) -> dict[tuple[str, str], dict[str, float]]:
    """Mutual top-k pairs that stand out from both items' own similarities (Z_MIN). → {(a, b): {sim, pctl}} with
    a < b; pctl is the pair's place among every pair of `ids` (the house's own distribution)."""
    import numpy as np
    keys = sorted(i for i in (ids if ids is not None else vecs) if i in vecs)
    if len(keys) < 3:
        return {}
    m = np.stack([vecs[k] for k in keys]).astype(np.float32)
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    sim = m @ m.T
    n = len(keys)
    iu = np.triu_indices(n, 1)
    dist = np.sort(sim[iu])
    np.fill_diagonal(sim, -1.0)
    k = min(top_k, n - 1)
    top = np.argsort(-sim, axis=1)[:, :k]
    tops = [set(row.tolist()) for row in top]
    # each item's own spread (without itself): a pair stands out when it's Z_MIN deviations above both items' means.
    # A global percentile alone is swamped by templated mail (receipts that are near-identical to each other).
    s0 = sim.copy()
    np.fill_diagonal(s0, np.nan)
    mu, sd = np.nanmean(s0, axis=1), np.maximum(np.nanstd(s0, axis=1), 1e-6)
    out = {}
    for i in range(n):
        for j in tops[i]:
            if i < j and i in tops[j] and (sim[i, j] - mu[i]) / sd[i] >= Z_MIN and (sim[i, j] - mu[j]) / sd[j] >= Z_MIN:
                p = float(np.searchsorted(dist, dist.dtype.type(sim[i, j]), side="right")) / len(dist)
                out[(keys[i], keys[j])] = {"sim": round(float(sim[i, j]), 3), "pctl": round(p, 4), "near": True}
    return out


def pair_score(vecs: dict[str, Any], a: str, b: str, dist: Any) -> dict[str, float] | None:
    """The topic score of one pair (for an edge that exists for other reasons), as a corpus percentile."""
    import numpy as np
    if a not in vecs or b not in vecs or dist is None or not len(dist):
        return None
    va, vb = vecs[a], vecs[b]
    s = float(va @ vb / max(float(np.linalg.norm(va) * np.linalg.norm(vb)), 1e-9))
    # search with the array's own dtype: a Python float makes numpy convert the whole distribution to float64 on
    # every call (125 ms at 7.5k items, once per scored pair: threads took 30-90 min on titan, 10-05)
    at = np.searchsorted(dist, dist.dtype.type(s), side="right")
    return {"sim": round(s, 3), "pctl": round(float(at) / len(dist), 4)}


def distribution(vecs: dict[str, Any], ids: Iterable[str] | None = None) -> Any:
    import numpy as np
    keys = sorted(i for i in (ids if ids is not None else vecs) if i in vecs)
    if len(keys) < 2:
        return None
    m = np.stack([vecs[k] for k in keys]).astype(np.float32)
    m /= np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)
    s = m @ m.T
    return np.sort(s[np.triu_indices(len(keys), 1)])
