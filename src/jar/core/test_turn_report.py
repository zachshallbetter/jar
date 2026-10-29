#!/usr/bin/env python3
# gate: hermetic   (a temp turn log; the broker lines are stubbed)
"""The weekly turn report's prompt-cache lines (#405): cache share per brain, local prefill, cold local
turns named by prefix id and role (#404), guides read with skill_guide, and the week-over-week drop
flag. Hermetic: a temp turn log, and the broker lines (network) are stubbed out.

Run: python3 scripts/test_turn_report.py
"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_turn_report as rep  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


def turn(brain, fresh=None, cached=None, prefill=None, prefix="aaaa1111", role="admin", read=None, ts=None, source="user"):
    r = {"ts": ts or time.time() - 3600, "source": source, "brain": brain, "ms": 2000, "route": "chat"}
    if fresh is not None:
        r.update(prompt_fresh=fresh, prompt_cached=cached or 0, prefix=prefix, role=role)
    if prefill is not None:
        r["prefill_ms"] = prefill
    if read:
        r["skills_read"] = read
    return r


print("\n── cache lines ──")
check("no usage logged → no cache lines", rep.cache_lines([turn("gemini")], []), [])

rows = [turn("gemini", 2000, 6000), turn("gemini", 1000, 7000),
        turn("local", 1300, 21500, prefill=7000), turn("local", 900, 15200, prefill=4800),
        turn("local", 7800, 1, prefill=41000, prefix="bbbb2222", role="adult"),
        turn("gemini", read=["calendar"]), turn("gemini", read=["calendar", "media"])]
lines = rep.cache_lines(rows, [])
check("one line per brain: share, tokens per turn, local prefill", lines[0],
      "prompt cache: gemini 81% of 8,000 tokens/turn; local 79% of 15,567 tokens/turn, prefill p50/p90 7.0/41.0s")
check("a cold local turn is named by prefix and role", lines[1],
      "local turns cold: 1 of 3 (prefix role: bbbb2222 adult ×1)")
check("guides read", lines[2], "skill guides read: 2 of 7 turns (calendar 2, media 1)")
check("no drop flag without a period before", any(l.startswith("Look into") for l in lines), False)

before = [turn("gemini", 500, 9500, ts=time.time() - 9 * 86400)]
low = [turn("gemini", 5000, 3000)]
flag = rep.cache_lines(low, before)
check("a 15-point drop against the period before is flagged",
      flag[-1].startswith("Look into: cache share fell 95% → 38%"), True)
check("a small drop isn't", any(l.startswith("Look into") for l in rep.cache_lines([turn("gemini", 800, 9200)], before)), False)

print("\n── the whole report reads the log: user turns only, the period before for the flag ──")
with tempfile.TemporaryDirectory() as d:
    log = os.path.join(d, "turns.jsonl")
    with open(log, "w") as fh:
        for r in rows + [turn("local", 9000, 0, source="eval")] + [turn("gemini", 100, 9900, ts=time.time() - 8 * 86400)]:
            fh.write(json.dumps(r) + "\n")
    rep.broker_spend = rep.broker_shadow = lambda days: None
    sys.argv = ["agent_turn_report.py", "--log", log, "--days", "7"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rep.main()
    out = buf.getvalue()
    check("7 real turns (the eval turn is left out)", out.startswith("7 real turns in 7 days"), True)
    check("the cache lines are in it", "prompt cache: gemini 81%" in out and "local turns cold: 1 of 3" in out, True)
    check("compared with the week before (99% → 79% flagged)", "Look into: cache share fell 99% → 79%" in out, True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
