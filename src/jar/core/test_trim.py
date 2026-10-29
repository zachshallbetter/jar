#!/usr/bin/env python3
# gate: hermetic
"""Filler trimming for saved pages (#868): menus, banners, share rows, footers and repeated table-of-contents lines go;
the article, its headings and its date stay; a page that's mostly short lines stays whole. Wired into ingest_url and
POST /v1/capture (always: KB_TRIM was removed, #945); ingest_url also refuses to save a block page (#826). Made-up pages, no runtime.
Run: python3 scripts/test_trim.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-trim-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "AGENT_MEMORY_DIR": "memory", "PROACTIVE_DB": "proactive.db",
             "AGENT_TURN_LOG": "turns.jsonl", "DAILY_LOG_DIR": "daily", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db", "CONNECTORS_LEDGER": "ledger.db", "CAPTURE_DB": "capture.db"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import trim  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.220} expected={expected!r:.220}")


PAGE = """Skip to content
Home
Blog
About
Subscribe
How we fixed our sourdough starter
Contents
Why it went flat
What changed
Oct 2, 2026
Why it went flat
After two weeks of a flat starter, we noticed the kitchen had dropped to 64 degrees overnight.
What changed
The trick was feeding it twice a day with whole rye and keeping it near the oven light, around 78 degrees.
Read more
Share this
Accept all cookies
© 2026 Hale Bakery. All rights reserved.
Home"""
out, st = trim.trim(PAGE, "How we fixed our sourdough starter")
lines = out.splitlines()
check("the article's sentences stay", [l for l in lines if l.endswith(".")],
      ["After two weeks of a flat starter, we noticed the kitchen had dropped to 64 degrees overnight.",
       "The trick was feeding it twice a day with whole rye and keeping it near the oven light, around 78 degrees."])
check("each section's own heading stays, the table of contents doesn't",
      (lines.count("Why it went flat"), lines.count("What changed"), "Contents" in lines), (1, 1, False))
check("menus, banners, share rows and the footer go",
      [x for x in ("Skip to content", "Subscribe", "Read more", "Share this", "Accept all cookies", "Home") if x in lines], [])
check("the copyright line goes", any("rights reserved" in l for l in lines), False)
check("stats say what went", (st["lines_in"] > st["lines_out"], st["chars_out"] < st["chars_in"]), (True, True))
short = "\n".join(["Milk", "Eggs", "Bread", "Rye flour", "Butter", "Apples", "Coffee"])
check("a page of short lines (a list) stays whole", trim.trim(short)[0], short)
check("empty in, empty out", trim.trim("")[0], "")

print("── wired into the saves ──")
import capture  # noqa: E402
saved = []
idx = lambda sid, title, text, st_, md, **kw: saved.append(text)  # noqa: E731
capture.save("p-robin", {"url": "https://bakery.example/starter2", "title": "Starter", "text": PAGE}, index=idx)
check("capture: trimmed, the title and URL still on top", ("Subscribe" in saved[-1],
                                                                saved[-1].startswith("Starter\nhttps://bakery.example/starter2")),
      (False, True))

import agent_runtime_lib as lib  # noqa: E402
real_rt, real_idx = lib._runtime_request, lib._kb_index
try:
    lib._kb_index = lambda sid, title, text, st_, md, **kw: saved.append(text)
    lib._runtime_request = lambda *a, **k: {"title": "Starter", "content": PAGE}
    out = lib.execute_tool("ingest_url", {"url": "https://bakery.example/starter3"})
    check("ingest_url: saved trimmed", ("Saved" in out, "Accept all cookies" in saved[-1]), (True, False))
    n = len(saved)
    lib._runtime_request = lambda *a, **k: {"title": "", "content": "You've been blocked by network security.\nFile a ticket"}
    out = lib.execute_tool("ingest_url", {"url": "https://www.reddit.com/r/x/comments/y1/"})
    check("ingest_url won't save a block page as the page (#826)", (len(saved) == n, "blocked titan's request" in out,
                                                                    "couldn't save that page" in out), (True, True, True))
finally:
    lib._runtime_request, lib._kb_index = real_rt, real_idx

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
