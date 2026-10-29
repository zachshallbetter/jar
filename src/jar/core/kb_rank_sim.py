#!/usr/bin/env python3
"""An exact offline model of retrieval-runtime's hybrid query (AcmeProductCo/state 3713440c, titan-node#1225): the same vector pool
(max(top_k*5, 50) nearest chunks, extras included), an extra standing in for its source's first body chunk, the
same lexical scorer, the 0.2 floor, the same order and chunk-id dedupe. A `variant` swaps one piece (the text an
extra's lexical part reads, the lexical weight, or a reorder of the scored list) to screen a ranking change before
it's built. `rerank_reorder` is the reranker (#1622) as such a reorder; `kb_rerank_ab.py` scores it on the bank. Reads the retrieval DB through psql with the connection in the environment (never argv); prints nothing.

Validated 10-07 on titan: for all 115 KB bank questions the model's results equal the runtime's (same chunks, same
order, same total, scores identical), and replaying extra_lexical="note" predicted the live A/B's +cur-fb041200 /
-gen-0ed46405 (the old screen re-ranked only the returned top 60 and missed the loss: the runtime scores a larger pool
and keeps one hit per chunk id, so a scoring change can swap which duplicate survives).

When the runtime's scorer changes, change this with it; `python3 scripts/kb_rank_sim.py validate` re-checks the match
(titan only: it reads the retrieval DB and calls the runtime).
"""
import json, math, os, re, subprocess, urllib.parse, urllib.request

_ENV_FILE = "/ai/projects/runtimes/retrieval-runtime/service.env"
_TOK = re.compile(r"[A-Za-z0-9_]+")


def _service_env():
    out = {}
    for line in open(_ENV_FILE).read().splitlines():
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


_SVC: dict = {}


def _svc():
    if not _SVC:
        _SVC.update(_service_env())
    return _SVC


def _pg_env():
    u = urllib.parse.urlparse(_svc()["DATABASE_URL"])
    env = dict(os.environ, PGHOST=u.hostname or "127.0.0.1", PGPORT=str(u.port or 5432),
               PGUSER=urllib.parse.unquote(u.username or ""), PGPASSWORD=urllib.parse.unquote(u.password or ""),
               PGDATABASE=(u.path or "/").lstrip("/"))
    q = urllib.parse.parse_qs(u.query)
    if q.get("sslmode"):
        env["PGSSLMODE"] = q["sslmode"][0]
    return env


def _sql(stmt):
    r = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-f", "-"], input=stmt, env=_pg_env(),
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("psql failed")          # its stderr may hold the query: never shown
    out = r.stdout.strip()
    return json.loads(out) if out else None


def _lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def embed(text):
    url = _svc().get("EMBEDDINGS_URL") or "http://127.0.0.1:8015/v1/embeddings"
    body = json.dumps({"input": [text], "model": "turboquant"}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}),
                                         timeout=30))
    return [float(x) for x in r["data"][0]["embedding"]]


def pool(vec, n, include_extras=True):
    v = "'[" + ",".join(repr(x) for x in vec) + "]'::vector"
    return _sql(f"""SELECT coalesce(json_agg(t ORDER BY t.rk), '[]') FROM (
        SELECT c.chunk_id, c.source_id, c.parent_record_id, c.chunk_index, c.citation_ref, c.chunk_text, c.chunk_hash,
               c.section_path, c.token_count, c.metadata_json, c.security_classification, s.title AS source_title,
               c.is_extra, (1 - (c.embedding <=> {v}))::float8 AS vector_score,
               row_number() OVER (ORDER BY c.embedding <=> {v}) AS rk
        FROM retrieval_chunks c JOIN retrieval_sources s ON c.source_id = s.source_id
        WHERE ({'TRUE' if include_extras else 'FALSE'} OR NOT c.is_extra)
        ORDER BY c.embedding <=> {v} LIMIT {int(n)}) t;""") or []


_BODY = {}


def bodies(source_ids):
    """First body chunk and the whole body text, per source (cached for the run)."""
    need = [s for s in set(source_ids) if s not in _BODY]
    if need:
        rows = _sql(f"""SELECT coalesce(json_agg(t), '[]') FROM (
            SELECT f.source_id, f.chunk_id, f.chunk_index, f.citation_ref, f.chunk_text, f.chunk_hash, f.token_count,
                   (SELECT string_agg(b.chunk_text, ' ' ORDER BY b.chunk_index) FROM retrieval_chunks b
                    WHERE b.source_id = f.source_id AND NOT b.is_extra) AS whole
            FROM (SELECT DISTINCT ON (source_id) * FROM retrieval_chunks
                  WHERE source_id = ANY(ARRAY[{','.join(_lit(s) for s in need)}]::text[]) AND NOT is_extra
                  ORDER BY source_id, chunk_index) f) t;""") or []
        for r in rows:
            _BODY[r["source_id"]] = r
        for s in need:
            _BODY.setdefault(s, None)
    return _BODY


def normalize_text(text):
    """retrieval-runtime ingestion.rs normalize_text: what an extra chunk is embedded and stored as."""
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _cos(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return 0.0 if not na or not nb else sum(x * y for x, y in zip(a, b)) / (na * nb)


def templates(source_ids):
    """Per source, the fields an extra chunk of it carries (its parent, metadata, classification, title)."""
    return _sql(f"""SELECT coalesce(json_agg(t), '[]') FROM (
        SELECT DISTINCT ON (c.source_id) c.source_id, c.parent_record_id, c.metadata_json,
               c.security_classification, c.section_path, s.title AS source_title
        FROM retrieval_chunks c JOIN retrieval_sources s ON c.source_id = s.source_id
        WHERE c.source_id = ANY(ARRAY[{','.join(_lit(x) for x in source_ids)}]::text[])
        ORDER BY c.source_id, c.is_extra DESC, c.chunk_index) t;""") or []


def _extras_pool(vec, n, include_extras, extras, embed_fn, pool_fn):
    """The pool as if `extras` ({source_id: [extra chunk text]}) were those sources' extras instead of their stored
    ones: the n nearest of the body chunks, every other source's stored extras, and these (embedded as the runtime
    embeds an extra: its normalized text). [] removes a source's extras."""
    rows = (pool_fn or pool)(vec, n, False)
    if include_extras:
        stored = (pool_fn or pool)(vec, n + 50 * max(1, len(extras)), True)
        rows += [r for r in stored if r["is_extra"] and r["source_id"] not in extras]
        if extras:
            for t in templates(list(extras)):
                for i, text in enumerate(extras.get(t["source_id"]) or []):
                    norm = normalize_text(text)
                    if not norm:
                        continue
                    rows.append(dict(t, chunk_id=f"{t['source_id']}#x{i}", chunk_index=10000 + i, citation_ref="",
                                     chunk_text=norm, chunk_hash="", token_count=len(tokenize(norm)), is_extra=True,
                                     vector_score=_cos(vec, (embed_fn or embed)(norm))))
    rows.sort(key=lambda r: -r["vector_score"])
    return rows[:n]


def tokenize(text):
    return _TOK.findall(str(text or "").lower())


def score_chunk(query, chunk_text, title, metadata):
    """retrieval-runtime domain/search.rs score_chunk, term for term."""
    qt, ct = set(tokenize(query)), set(tokenize(chunk_text))
    if not qt:
        return 0.0, "empty query"
    overlap = len(qt & ct) / len(qt)
    ql = str(query).lower()
    title_bonus = 0.1 if ql in str(title or "").lower() else 0.0
    exact_bonus = 0.15 if ql in str(chunk_text or "").lower() else 0.0
    metadata_bonus = 0.05 if metadata else 0.0
    s = min(1.0, overlap * 0.75 + title_bonus + exact_bonus + metadata_bonus)
    return math.floor(s * 1000.0 + 0.5) / 1000.0, (
        f"overlap={overlap:.2f}; title_bonus={title_bonus:.2f}; exact_bonus={exact_bonus:.2f}; metadata_bonus={metadata_bonus:.2f}")


def query(text, top_k, opts=None, variant=None, *, pool_fn=None, bodies_fn=None, embed_fn=None):
    """{"results": [...], "total_matches": n} as POST /v1/retrieval/query (hybrid) would answer.
    variant: {"extra_lexical": "note"|"body"|..., "lexical_weight": w, "reorder": fn(text, candidates) -> candidates,
              "extras": {source_id: [extra chunk text]} (those sources' extras as if re-indexed with these)}."""
    opts, variant = dict(opts or {}), dict(variant or {})
    mode = variant.get("extra_lexical", opts.get("extra_lexical"))
    lw = max(0.0, min(1.0, float(variant.get("lexical_weight", opts.get("lexical_weight", 0.4)))))
    vec = (embed_fn or embed)(text)
    n = max(int(top_k) * 5, 50)
    if variant.get("extras") is not None:
        rows = _extras_pool(vec, n, opts.get("include_extras", True), variant["extras"], embed_fn, pool_fn)
    else:
        rows = (pool_fn or pool)(vec, n, opts.get("include_extras", True))
    body = (bodies_fn or bodies)([r["source_id"] for r in rows if r["is_extra"]])
    cands = []
    for r in rows:
        md = json.loads(r.get("metadata_json") or "{}") or {}
        match_text = r["chunk_text"]
        rec = {k: r[k] for k in ("chunk_id", "source_id", "parent_record_id", "chunk_index", "citation_ref", "chunk_text",
                                 "chunk_hash", "section_path", "token_count", "security_classification")}
        if r["is_extra"]:
            b = body.get(r["source_id"])
            if not b:
                continue
            if mode == "body":
                match_text = b["chunk_text"]
            elif mode == "note":
                match_text = b["whole"] or b["chunk_text"]
            rec.update({k: b[k] for k in ("chunk_id", "chunk_index", "citation_ref", "chunk_text", "chunk_hash", "token_count")})
        lex, lex_expl = score_chunk(text, match_text, r["source_title"], md)
        s = r["vector_score"] * (1.0 - lw) + lex * lw
        expl = f"hybrid_score={s:.3f} (vec: {r['vector_score']:.3f}, lex: {lex:.3f}); lex_detail: {lex_expl}"
        if s < 0.2:
            s = 0.0
        if s <= 0.0:
            continue
        if r["is_extra"]:
            expl += "; via an extra chunk"
        cands.append(dict(rec, score=s, explanation=expl, metadata=md, _vec=r["vector_score"], _lex=lex))
    cands.sort(key=lambda c: (-c["score"], c["source_id"], c["chunk_index"], c["chunk_id"]))
    seen, out = set(), []
    for c in cands:
        if c["chunk_id"] not in seen:
            seen.add(c["chunk_id"])
            out.append(c)
    if variant.get("reorder"):
        out = variant["reorder"](text, out)
    return {"results": out[:int(top_k)], "total_matches": len(out)}


def rerank_reorder(text, candidates, *, rerank_fn=None, floor=None, fetch=None):
    """The reranker as a variant (#1622: `query(..., variant={"reorder": rerank_reorder})`): the top `fetch` (kb_rerank's 20)
    scored by bge-reranker with the question, best first, and those under the floor (-2.8) dropped. Nothing past the top
    `fetch` is kept, as in kb_retrieve's reranked path (_kb_query_reranked). `rerank_fn` stands in for the service in a test."""
    import kb_rerank
    fetch = kb_rerank.FETCH if fetch is None else fetch
    floor = kb_rerank.floor() if floor is None else floor
    head = candidates[:fetch]
    ranked = (rerank_fn or kb_rerank.rerank)(text, [dict(c, title=c.get("metadata", {}).get("title", "")) for c in head])
    keep = [dict(c, score=c["rerank_score"]) for c in ranked if c["rerank_score"] >= floor]
    return keep


def validate() -> dict:
    """The model against the live runtime for every KB bank question: same chunks, order and total (counts and ids)."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import agent_runtime_lib as lib
    import kb_bank
    import kb_privacy as P
    fetch = min(max(kb_bank.K * 3 * P.OVERFETCH, kb_bank.K * 3), P.MAX_FETCH)
    same, diffs, worst = 0, [], 0.0
    for c in kb_bank.load():
        live = lib._runtime_request("retrieval", "/v1/retrieval/query", "POST", {"query": c["q"], "top_k": fetch, **P.QUERY_OPTS})
        sim = query(c["q"], fetch, P.QUERY_OPTS)
        a, b = [h["chunk_id"] for h in live.get("results") or []], [h["chunk_id"] for h in sim["results"]]
        if a == b and live.get("total_matches") == sim["total_matches"]:
            same += 1
        else:
            diffs.append(c["id"])
        worst = max([worst] + [abs(float(x["score"]) - y["score"]) for x, y in zip(live.get("results") or [], sim["results"])
                               if x["chunk_id"] == y["chunk_id"]])
    return {"cases": same + len(diffs), "same": same, "differ": diffs, "max_score_delta": round(worst, 6)}


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["validate"]:
        print(json.dumps(validate()))
    else:
        print("usage: kb_rank_sim.py validate")
