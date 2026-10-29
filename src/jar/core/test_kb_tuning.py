#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Hermetic tests for kb_tuning (titan-node#370): decayed usage labels and sweep proposals (the noise-magnet ranking
penalty, KB_USAGE_PRIOR, was removed after tying on the KB bank, #945).

    python3 scripts/test_kb_tuning.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DB = Path(tempfile.mkdtemp()) / "trace.db"
os.environ["RETRIEVAL_TRACE_DB"] = str(DB)
import kb_tuning as K  # noqa: E402
import retrieval_trace as T  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    if got == expected:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")


def h(sid, score, src="note"):
    return {"source_id": sid, "score": score, "metadata": {"source": src}}


# ten turns: the newsletter is shown every time and never cited; the ballet note is cited each time
for i in range(10):
    T.begin()
    raw = [h("news", 0.62, "mail"), h("ballet", 0.60), h("deep", 0.41)]
    T.query("passive", f"q{i}", raw, raw[:2])
    T.mark("shown", raw[:2])
    T.end(f"t{i}", "zach", ["ballet"])
u = K.usage(30)
check("labels: shown and cited per source", (round(u["news"]["shown"], 1), u["news"]["cited"], round(u["ballet"]["cited"], 1)),
      (10.0, 0.0, 10.0))
old = K.usage(30, now=time.time() + 28 * 86400)
check("labels fade: four weeks on, a quarter", round(old["news"]["shown"], 1), 2.5)
s = K.sweep(14)
check("sweep proposes a floor just under the lowest cited score, and top-k from the deepest cited rank",
      s["by_path_and_type"]["passive/note"]["proposal"], {"floor": 0.58, "top_k": 2})
check("…a type never cited: no proposal", s["by_path_and_type"]["passive/mail"]["proposal"], None)
check("…and says how to adopt one (a reviewed PR)", "reviewed PR" in s["how_to_adopt"], True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
