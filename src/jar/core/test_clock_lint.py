#!/usr/bin/env python3
# gate: hermetic
"""Clock time bombs, test side (#1515): a test that pins a world clock must not hand production code the real clock.

test_consolidate (#1512) and test_miss_proposals (#1529) pinned the world's clock to a fixed day but passed or let
production read `time.time()` for "the last 7 days", so each turned red on its own a week later, with no merge. This
lint flags, in any hermetic test that builds a fixed clock (`Clock(...)` or `clock=` on a world), a real-clock read
passed as `now=`, `at=` or `today=`. The fix is to pass the test's clock (`now=clock()`). It reads files only.
Run: python3 scripts/test_clock_lint.py"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
FIXED_CLOCK = re.compile(r"\bClock\(|[(,]\s*clock\s*=")   # a Clock, or clock= passed as a keyword (not self.clock = …)
REAL_AS_ARG = re.compile(r"\b(now|at|today)\s*=\s*(time\.time\(\)|(?:_?dt\.)?datetime\.now\(|date\.today\(|"
                         r"datetime\.datetime\.now\(|(?:\w+\.)?_local_now\(\))")

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.600} expected={expected!r}"))


def hits(text: str) -> list[int]:
    """Line numbers of real-clock arguments, in a test that pins a clock; [] when it pins none."""
    if not FIXED_CLOCK.search(text):
        return []
    return [i for i, line in enumerate(text.splitlines(), 1)
            if REAL_AS_ARG.search(line.split("#", 1)[0])]


# The lint itself: the shape that bombed is caught; the fixed shape and a clock-free test aren't.
BOMB = 'clock = Clock("2026-10-07T12:00")\nB = W.Bubbles(p, clock=clock)\ncheck("x", MP.counts(B, 7, now=time.time()), 1)\n'
check("the 10-14 bomb's shape is flagged", hits(BOMB), [3])
check("…passing the test's clock is not", hits(BOMB.replace("now=time.time()", "now=clock()")), [])
check("…a test with no fixed clock may use the real one", hits("x = f(now=time.time())\n"), [])
check("…datetime.now() and _local_now() count too",
      hits('clock = Clock("x")\nf(today=date.today())\ng(at=datetime.now())\nh(now=lib._local_now())\n'), [2, 3, 4])
check("…an attribute named clock isn't a pinned clock", hits("self.clock = 0\nf(now=time.time())\n"), [])
check("…a commented-out call doesn't", hits('clock = Clock("x")\n# f(now=time.time())\n'), [])

found = {}
for p in sorted(HERE.glob("test_*.py")):
    if p.name == Path(__file__).name:
        continue
    text = p.read_text(errors="replace")
    if "# gate: hermetic" not in text[:400]:
        continue
    h = hits(text)
    if h:
        found[p.name] = h
check("no hermetic test pins a clock and hands production the real one", found, {})

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
