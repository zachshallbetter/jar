#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic check of rerank_eval's ranking and calibration arithmetic (titan-node#369), with the reranker and
the KB faked.

    python3 scripts/test_rerank_eval.py
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rerank_eval as E  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


KB = {"q found": [{"title": "noise", "chunk_text": "newsletter"}, {"title": "Pairing", "chunk_text": "pair a phone"}],
      "q empty": [{"title": "noise", "chunk_text": "newsletter", "score": 0.46}]}
sys.modules["kb_privacy"] = types.SimpleNamespace(query=lambda call, q, k, p, r: KB[q])
sys.modules["agent_runtime_lib"] = types.SimpleNamespace(_runtime_request=None)
sys.modules["retrieval_eval"] = types.SimpleNamespace(FOUND=[("pairing", "q found", ["pair"])],
                                                      NOT_STORED=[("nothing", "q empty", ["wifi"])])
E.rerank = lambda q, docs: [3.0 if "pair" in d else -5.0 for d in docs]
E.OFF_TOPIC = []
r = E.run(20)
check("reranked: the right passage moves from 2 to 1", (r["found"][0]["before"], r["found"][0]["after"]), (2, 1))
check("calibration: a floor between the lowest right answer and the highest nothing", r["calibration"]["floor"], -1.0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
