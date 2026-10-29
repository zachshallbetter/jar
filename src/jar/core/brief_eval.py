#!/usr/bin/env python3
"""The per-person brief, measured (titan-node#375): world_eval's lookups asked twice through the real path, once with
today's per-question world block and once with the whole brief in its place (an eval-only pin; never the cached
prefix), on the brain chosen. Report only: counts and sizes, never questions or replies. Adoption goes through the
owner session.

  python3 scripts/brief_eval.py [--brain gemini|local] [--limit 15]     (on titan, after a deploy with eval_brief)
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def error_kind(error: str) -> str:
    """The kind of a failed turn, content-free: the exception name, or "server" for an error event from titan.
    A client TimeoutError is the harness giving up, not titan failing; titan's own turn log says which."""
    head = error.split(":", 1)[0].strip()
    return head if head.isidentifier() else "server"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--brain", choices=["gemini", "local"], default="gemini")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--timeout", type=int, default=0, help="client seconds per read (default 180; local 300)")
    a = ap.parse_args()
    import truth_eval
    import turn_eval as te
    import world as W
    import world_eval as E
    w = W.get()
    cases = E.lookups(w, a.limit) + E.household_lookups(w)
    tools = sorted(truth_eval.READ_TOOLS)
    timeout = a.timeout or (300 if a.brain == "local" else 180)
    res = {"brief": 0, "world": 0, "errors": 0}
    kinds: Counter[str] = Counter()
    for c in cases:
        kid = c.name.endswith(".kid")
        as_ = {"eval_as": {"role": "kid", "name": "Eval Kid"}} if kid else {}
        for arm in ("world", "brief"):
            extra = {"eval_brain": a.brain, "eval_tools": tools, "eval_world": True}
            if arm == "brief":
                extra["eval_brief"] = True
            t = te.ask(c.question, extra=extra, timeout=timeout, **as_)
            if t.error:
                res["errors"] += 1
                kinds[error_kind(t.error)] += 1
            elif not c.check(t.text):
                res[arm] += 1
    toks = {"admin": len(W.brief(w, W.ADMIN, "admin")) // 4, "kid": len(W.brief(w, "eval-kid", "kid")) // 4}
    print(json.dumps({"brain": a.brain, "cases": len(cases), "pass_world_block": res["world"],
                      "pass_brief": res["brief"], "errors": res["errors"], "error_kinds": dict(kinds), "timeout_s": timeout,
                      "brief_tokens_approx": toks}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
