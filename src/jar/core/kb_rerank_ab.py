#!/usr/bin/env python3
"""The reranker A/B for #1622 (Zach 10-14: "test it first"): the KB bank scored twice on titan's real KB, as each
case's owner sees it.

  A  kb_retrieve as today: _kb_query, the top 3 this person may see
  B  kb_retrieve with KB_RERANK=1: _kb_query_reranked, the top 20 this person may see reranked by bge-reranker
     (titan-reranker, :18030) with its -2.8 floor, then the top 3

Scored like kb_bank's nightly (found@3: an expected item in the top 3; the same cases and owners), plus how long each
arm took. B's floor can drop every hit, so "B found nothing" is counted separately. It prints counts and case ids only,
never a question, a title or a passage. It changes nothing: KB_RERANK is set for this process only, and nothing is
written but the report.

On titan, outside the eval windows (it loads GPU2 and the retrieval runtime), as zach:
  python3 scripts/kb_rerank_ab.py [--out /ai/tmp/kb-rerank-ab.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_bank  # noqa: E402


def compare(cases: list[dict[str, Any]], arm_a: Callable, arm_b: Callable, role_of: Callable,
            about_titan: Callable | None = None, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Both arms over the same cases: kb_bank.score for each, the cases one finds and the other doesn't, B's empties."""
    empties: list[str] = []

    def b(q, case):
        hits = arm_b(q, case)
        if not hits:
            empties.append(case["id"])
        return hits

    t0 = clock()
    ra = kb_bank.score(cases, arm_a, role_of, about_titan)
    t1 = clock()
    rb = kb_bank.score(cases, b, role_of, about_titan)
    t2 = clock()
    miss_a, miss_b = set(ra["misses"]), set(rb["misses"])
    return {"cases": ra["cases"],
            "a": {"found": ra["found"], "by_kind": ra["by_kind"], "seconds": round(t1 - t0, 1)},
            "b": {"found": rb["found"], "by_kind": rb["by_kind"], "seconds": round(t2 - t1, 1), "empty": len(empties)},
            "b_gains": sorted(miss_a - miss_b), "b_losses": sorted(miss_b - miss_a),
            "b_empty_cases": sorted(empties)}


def verdict(r: dict[str, Any]) -> str:
    """One line: does reranking help on this bank?"""
    a, b, n = r["a"]["found"], r["b"]["found"], r["cases"] or 1
    per = (r["b"]["seconds"] / n, r["a"]["seconds"] / n)
    return (f"found@3: A {a}/{r['cases']} ({a / n:.0%}), B {b}/{r['cases']} ({b / n:.0%}); B gains {len(r['b_gains'])}, "
            f"loses {len(r['b_losses'])}, empty {r['b']['empty']}; {per[0]:.2f} s vs {per[1]:.2f} s a case")


def _live() -> tuple[Callable, Callable, Callable, Callable]:
    import agent_runtime_lib as lib
    import household_identity as hh
    os.environ["KB_RERANK"] = "1"   # this process only: arm B's path
    try:
        roles = {str(p["id"]): str(p.get("role") or "") for p in hh.get_store().people()}
    except Exception:  # noqa: BLE001
        roles = {}

    def role_of(owner):
        return roles.get(str(owner or "")) or ("admin" if owner in (None, hh.ADMIN_PERSON_ID) else "adult")

    def as_owner(fn):
        def run(q, case):
            lib._TURN_IO.person = {"id": case.get("owner") or hh.ADMIN_PERSON_ID, "name": "eval"}
            lib._TURN_IO.role = role_of(case.get("owner"))
            lib._TURN_IO.source = "eval"
            try:
                return fn(q, kb_bank.K)
            finally:
                lib._TURN_IO.person = lib._TURN_IO.role = None
        return run

    return as_owner(lib._kb_query), as_owner(lib._kb_query_reranked), role_of, lib._about_titan


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    arm_a, arm_b, role_of, about = _live()
    r = compare(kb_bank.load(), arm_a, arm_b, role_of, about)
    r["verdict"] = verdict(r)
    print(r["verdict"])
    if a.out:
        Path(a.out).write_text(json.dumps(r, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
