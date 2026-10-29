#!/usr/bin/env python3
"""Shadow replay: test a knowledge change against the last days of real searches before it ships (titan-node#367).

The retrieval trace (#368) keeps only a hash of each search, so each traced search (`search` / `search-rerank`)
is paired with that person's own message just before it, from their stored conversation on titan, and that
message is replayed as the query, as that person (their visibility and fences). Two variants are compared on
each: what the model would have read today vs with the change. A local judge (the local model; never a hosted
one) says which set answers the question better, with A and B shown in a random order. Nothing leaves titan, and
the report is counts only: no question, title or passage is printed or stored.

First target: KB_RERANK (#369/#575), the gate for switching it on.

  python3 scripts/shadow_replay.py rerank [--days 7] [--limit 60] [--no-judge]
  python3 scripts/shadow_replay.py misses        #377 P2: each structural proposal kind's replay gate (counts only)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

MEMORY_DB = __import__("agent_paths").memory_db()
PAIR_WINDOW_S = 300          # the person's message this long before a traced search is its question
JUDGE_URL = os.environ.get("AGENT_ENDPOINT") or "http://127.0.0.1:8010"


def questions(days: float, trace_db: Path | None = None, memory_db: Path | None = None,
              limit: int = 60, paths: tuple[str, ...] = ("search", "search-rerank", "passive")) -> list[dict[str, Any]]:
    """[{person, text, at}] for traced searches (explicit, and the per-turn context's: both are real questions):
    the person's own last message before each one."""
    import household_identity as hh
    import retrieval_trace
    con = retrieval_trace._db(trace_db)
    try:
        rows = con.execute(f"SELECT DISTINCT turn_id, person, at FROM queries WHERE path IN ({','.join('?' * len(paths))}) "
                           "AND at >= ? AND person IS NOT NULL ORDER BY at DESC",
                           (*paths, time.time() - days * 86400)).fetchall()
    finally:
        con.close()
    mem = sqlite3.connect(f"file:{memory_db or MEMORY_DB}?mode=ro", uri=True)
    out, seen = [], set()
    try:
        for _, person, at in rows:
            r = mem.execute("SELECT content FROM messages WHERE conv_id=? AND role='user' AND ts<=? AND ts>=? "
                            "ORDER BY ts DESC LIMIT 1", (hh.conv_id_for(person), at + 5, at - PAIR_WINDOW_S)).fetchone()
            if not r or not str(r[0]).strip():
                continue
            text = " ".join(str(r[0]).split())[:500]
            if (person, text.lower()) in seen:
                continue
            seen.add((person, text.lower()))
            out.append({"person": person, "text": text, "at": at})
            if len(out) >= limit:
                break
    finally:
        mem.close()
    return out


def _render(hits: list[dict[str, Any]]) -> str:
    return "\n".join(f"- {h.get('title') or ''}: {' '.join(str(h.get('chunk_text') or '').split())[:300]}"
                     for h in hits) or "(nothing)"


JUDGE = """/no_think
A person asked titan, their household assistant: "{q}"
Two sets of notes titan could read to answer it:

SET 1:
{a}

SET 2:
{b}

Which set better lets titan answer the question correctly (the right facts, nothing misleading)? Return JSON only:
{{"better": 1 or 2 or 0}}  (0 when they're about the same)"""


def local_judge(prompt: str) -> str:
    body = json.dumps({"messages": [{"role": "user", "content": prompt}], "max_tokens": 40, "temperature": 0,
                       "response_format": {"type": "json_object"},
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    req = urllib.request.Request(JUDGE_URL + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"].get("content") or ""


def compare(qs: list[dict[str, Any]], baseline: Callable[[str, str, str], list[dict]],
            candidate: Callable[[str, str, str], list[dict]], roles: dict[str, str],
            judge: Callable[[str], str] | None, seed: int = 7) -> dict[str, Any]:
    """Counts only: identical sets, and where they differ the judge's better / same / worse for the candidate."""
    rnd = random.Random(seed)
    c = {"questions": len(qs), "identical": 0, "differ": 0, "better": 0, "same": 0, "worse": 0, "unjudged": 0,
         "empty_baseline": 0, "empty_candidate": 0, "worse_when_candidate_empty": 0, "better_when_candidate_empty": 0}
    for q in qs:
        role = roles.get(q["person"], "adult")
        a, b = baseline(q["text"], q["person"], role), candidate(q["text"], q["person"], role)
        c["empty_baseline"] += not a
        c["empty_candidate"] += not b
        if [h.get("source_id") for h in a] == [h.get("source_id") for h in b]:
            c["identical"] += 1
            continue
        c["differ"] += 1
        if judge is None:
            c["unjudged"] += 1
            continue
        flip = rnd.random() < 0.5
        first, second = (b, a) if flip else (a, b)
        try:
            raw = judge(JUDGE.format(q=q["text"], a=_render(first), b=_render(second)))
            m = re.search(r"\{.*\}", raw or "", re.S)
            v = int((json.loads(m.group(0)) if m else {}).get("better", -1))
        except Exception:  # noqa: BLE001
            v = -1
        if v not in (0, 1, 2):
            c["unjudged"] += 1
        elif v == 0:
            c["same"] += 1
        else:
            cand_won = (v == 1) == flip
            c["better" if cand_won else "worse"] += 1
            if not b:                      # the candidate showed nothing: was nothing the right answer?
                c["better_when_candidate_empty" if cand_won else "worse_when_candidate_empty"] += 1
    judged = c["better"] + c["same"] + c["worse"]
    c["verdict"] = ("no data" if not qs else "candidate holds" if c["worse"] <= c["better"] and
                    (judged == 0 or c["worse"] / max(judged, 1) <= 0.2) else "candidate loses")
    return c


def _as(person: str, role: str):
    """Run a search as this person: the same thread-local identity a turn has."""
    import agent_runtime_lib as lib
    lib._TURN_IO.person = {"id": person, "name": person}
    lib._TURN_IO.role = role


def rerank_variants():
    import agent_runtime_lib as lib

    def baseline(q, person, role):
        _as(person, role)
        return lib._kb_query(q, 5)

    def candidate(q, person, role):
        _as(person, role)
        return lib._kb_query_reranked(q, 5)
    return baseline, candidate


def misses_rank_as(person: str, role: str):
    """#377 P2's replay: kb_rank_sim's ranking as if a proposal existed (extras, never written), kept to what this
    person may see (the KB's sharing table), as source ids in rank order."""
    import audience as A
    import kb_privacy
    import kb_rank_sim

    def rank(question: str, extras: dict | None) -> list[str]:
        got = kb_rank_sim.query(question, 30, variant={"extras": extras} if extras else None)["results"]
        acl = kb_privacy.acl_rows([h["source_id"] for h in got])
        out: list[str] = []
        for h in got:
            owner, aud = acl.get(h["source_id"]) or (None, "household")
            if A.visible(aud, person, role, owner=owner) and h["source_id"] not in out:
                out.append(h["source_id"])
        return out
    return rank


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("change", choices=["rerank", "misses"])
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--floor", type=float, help="the reranker floor to try (KB_RERANK_FLOOR; default -2.8)")
    a = ap.parse_args()
    if a.floor is not None:
        os.environ["KB_RERANK_FLOOR"] = str(a.floor)
    import household_identity as hh
    if a.change == "misses":   # #377 P2: the replay gate per proposal kind, counts only
        import miss_proposals as MP
        import world as W
        from types import SimpleNamespace
        mem = SimpleNamespace(_db=sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True))
        folks = [p for p in hh.get_store().people() if p.get("role") != "household"]
        print(json.dumps({"change": "misses", **MP.gate_all(W.get(), folks, mem, misses_rank_as)}, indent=1))
        return 0
    roles = {str(p["id"]): str(p.get("role") or "adult") for p in hh.get_store().people()}
    qs = questions(a.days, limit=a.limit)
    base, cand = rerank_variants()
    print(json.dumps({"change": a.change, "days": a.days,
                      **compare(qs, base, cand, roles, None if a.no_judge else local_judge)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
