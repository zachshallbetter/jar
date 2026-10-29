#!/usr/bin/env python3
# gate: hermetic
"""kb_rank_sim (#1225), the offline model of retrieval-runtime's hybrid query: the lexical scorer term for term, the
0.2 floor, an extra standing in for its source's first body chunk (per extra_lexical mode), one hit per chunk id (the
better), the runtime's order and tie-breaks, the top_k cut and total; importing it reads nothing of titan's.
The pool, bodies and embedding are fakes. Run: python3 scripts/test_kb_rank_sim.py"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import kb_rank_sim as S  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


print("── import ──")
check("importing reads no service env (lazy)", S._SVC, {})

print("── the lexical scorer, term for term ──")
check("all words, the whole query in the text: overlap·0.75 + exact 0.15", S.score_chunk("gpu layout", "GPU layout of titan", "Runbook", {})[0], 0.9)
check("…plus title 0.10 and metadata 0.05, capped at 1", S.score_chunk("gpu layout", "GPU layout", "GPU layout", {"a": 1})[0], 1.0)
check("half the words: 0.375", S.score_chunk("gpu fleet", "the gpu", "", {})[0], 0.375)
check("a set match: repeats don't count, case folded, [A-Za-z0-9_]+ tokens",
      S.score_chunk("Run run RUN deploy_rust", "deploy_rust run", "", {})[0], 0.75)
check("rounded half up to 3 places (Rust's round)", S.score_chunk("a b c", "a", "", {})[0], 0.25)
check("an empty query: 0", S.score_chunk("?!", "anything", "", {})[0], 0.0)


def row(cid, sid, idx, text, vec, extra=False, md=None, title="T"):
    return {"chunk_id": cid, "source_id": sid, "parent_record_id": sid, "chunk_index": idx, "citation_ref": cid,
            "chunk_text": text, "chunk_hash": "h", "section_path": "", "token_count": len(text.split()),
            "metadata_json": json.dumps(md or {}), "security_classification": "internal", "source_title": title,
            "is_extra": extra, "vector_score": vec}


BODY = {"note-a": {"source_id": "note-a", "chunk_id": "a0", "chunk_index": 0, "citation_ref": "a0", "chunk_text": "Commands",
                   "chunk_hash": "h", "token_count": 1, "whole": "Commands run preflight before a fleet change"}}
POOL = [row("ax", "note-a", 10000, "how do i run preflight", 0.80, extra=True),
        row("a0", "note-a", 0, "Commands", 0.30),
        row("b0", "note-b", 0, "run preflight first", 0.60),
        row("c0", "note-c", 0, "unrelated words", 0.10),
        row("x9", "note-x", 10000, "an extra whose source has no body", 0.95, extra=True)]


def q(mode=None, top_k=10, **variant):
    opts = {"extra_lexical": mode} if mode else {}
    return S.query("run preflight", top_k, opts, variant, pool_fn=lambda v, n, inc: POOL,
                   bodies_fn=lambda ids: BODY, embed_fn=lambda t: [0.0])


print("── an extra stands in for its source's first body chunk ──")
r = q("body")
ids = [h["chunk_id"] for h in r["results"]]
a0 = next(h for h in r["results"] if h["chunk_id"] == "a0")
check("it carries the first body chunk's id and text, and says so; an extra with no body is dropped",
      (a0["chunk_text"], a0["explanation"].endswith("via an extra chunk"), "x9" in ids), ("Commands", True, False))
check("ranked by hybrid: b0 0.72 (all words) over the extra 0.48 (vector 0.8, no words in 'Commands')", ids, ["b0", "a0"])
check("one hit per chunk id: the better of the extra and the body chunk it stands in for", ids.count("a0"), 1)
check("the floor: under 0.2 is no hit (c0: 0.06 vector, no words)", "c0" in ids, False)
lex = lambda res: [h["_lex"] for h in res["results"] if h["chunk_id"] == "a0"][0]  # noqa: E731
check("body: the extra's lexical part reads the first body chunk (no shared words)", lex(r), 0.0)
check("note: …all of its body (both words, and the phrase)", lex(q("note")), 0.9)
check("absent: …the extra's own text", lex(q()), 0.9)
check("total is after the dedupe; top_k cuts", (r["total_matches"], len(q("body", top_k=1)["results"])), (2, 1))

both = [row("bx", "note-b", 10000, "how do i run preflight", 0.90, extra=True), row("b0", "note-b", 0, "run preflight first", 0.60)]
bb = {"note-b": {"source_id": "note-b", "chunk_id": "b0", "chunk_index": 0, "citation_ref": "b0",
                 "chunk_text": "run preflight first", "chunk_hash": "h", "token_count": 3, "whole": "run preflight first"}}
got = S.query("run preflight", 5, {"extra_lexical": "body"}, {}, pool_fn=lambda v, n, inc: both, bodies_fn=lambda ids: bb,
              embed_fn=lambda t: [0.0])
check("both the extra (0.90) and its own chunk (0.72) score: one b0, the extra's", 
      ([h["chunk_id"] for h in got["results"]], round(got["results"][0]["score"], 2), got["total_matches"]), (["b0"], 0.9, 1))

print("── order ──")
tie = [row("z0", "note-z", 0, "run", 0.5), row("y1", "note-y", 1, "run", 0.5), row("y0", "note-y", 0, "run", 0.5)]
got = S.query("run", 5, {}, {}, pool_fn=lambda v, n, inc: tie, bodies_fn=lambda ids: {}, embed_fn=lambda t: [0.0])
check("equal scores: by source id, then chunk index, then chunk id", [h["chunk_id"] for h in got["results"]], ["y0", "y1", "z0"])
check("a lexical weight variant reweights", round(q("body", lexical_weight=0.0)["results"][0]["score"], 3), 0.8)
check("a reorder variant runs on the scored, deduped list",
      [h["chunk_id"] for h in q("body", reorder=lambda t, hs: list(reversed(hs)))["results"]], ["a0", "b0"])
print("── a note's extras as if re-indexed (#1228) ──")
check("normalize_text as the runtime's: trailing blanks before a newline, 3+ newlines, ends",
      S.normalize_text("  Title \t\nq1?\n\n\n\nq2?  "), "Title\nq1?\n\nq2?")
EMB = {"run preflight": [1.0, 0.0], "Commands\nHow do I run preflight?": [1.0, 0.0], "Commands\nWhat is for lunch?": [0.0, 1.0]}
STORED = [row("a0", "note-a", 0, "Commands", 0.30), row("b0", "note-b", 0, "run preflight first", 0.60),
          row("ax", "note-a", 10000, "how do i run preflight", 0.80, extra=True),
          row("bx", "note-b", 10000, "bins", 0.50, extra=True)]


def fake_pool(v, n, include):
    return sorted([r for r in STORED if include or not r["is_extra"]], key=lambda r: -r["vector_score"])[:n]


S.templates = lambda ids: [{"source_id": i, "parent_record_id": i, "metadata_json": "{}", "security_classification": "internal",
                            "section_path": "", "source_title": "T"} for i in ids]
emb = lambda t: EMB.get(t, [0.5, 0.5])  # noqa: E731


def ids_of(variant, mode="body"):
    r = S.query("run preflight", 10, {"extra_lexical": mode}, variant, pool_fn=fake_pool, bodies_fn=lambda ids: BODY | bb,
                embed_fn=emb)
    return [(h["chunk_id"], h["explanation"].endswith("via an extra chunk")) for h in r["results"]]


check("note-a's extras replaced by a question far from the query: its stored extra no longer stands in",
      ("a0", True) in ids_of({"extras": {"note-a": ["Commands\nWhat is for lunch?"]}}), False)
check("…replaced by one that matches: it stands in again", ("a0", True) in ids_of({"extras": {"note-a": ["Commands\nHow do I run preflight?"]}}), True)
check("other notes keep their stored extras (in the pool; the dedupe then keeps whichever of b0 scores better)",
      [r["chunk_id"] for r in S._extras_pool([1.0, 0.0], 10, True, {"note-a": []}, emb, fake_pool) if r["is_extra"]], ["bx"])
check("[] removes a note's extras", any(c == "a0" and x for c, x in ids_of({"extras": {"note-a": []}})), False)
check("no extras variant: the stored pool, unchanged", ids_of({}) == ids_of({"extras": {}}), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
