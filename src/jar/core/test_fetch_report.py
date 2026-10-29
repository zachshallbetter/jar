#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The 10-12 decision tools: fetch_report (#936: blocked/403/429 fetches outside Reddit against the bar, real turns
only, hostnames only) and mail_compare --summary (#955's run-sheet gate: 7 consecutive days with mac_only = 0).
Run: python3 scripts/test_fetch_report.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fetch_report as F  # noqa: E402
import mail_compare as C  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


T0 = F._ts(F.SINCE)


def turn(at, web, source="user", pin=None):
    return json.dumps({"ts": T0 + at, "source": source, "pin": pin, "web": web})


LOG = [turn(60, [["fetch_url", "ok", "hale.example"], ["web_search", "ok", "search"]]),
       turn(120, [["fetch_url", "blocked", "reddit.com"]]),
       turn(180, [["fetch_url", "refused", "shop.example"], ["fetch_url", "ok", "github.com"]]),
       turn(240, [["fetch_url", "rate_limited", "news.example"], ["fetch_url", "timeout", "hale.example"]]),
       turn(300, [["fetch_url", "blocked", "shop.example"]], source="eval"),
       turn(360, [["fetch_url", "blocked", "shop.example"]], pin="p1"),
       turn(-60, [["fetch_url", "blocked", "old.example"]]),
       turn(8 * 86400, [["fetch_url", "blocked", "late.example"]])]
items, other = F.read(LOG, T0, T0 + 7 * 86400)
r = F.report(items, other, 7)
check("real turns in the window only; eval and pinned apart", (r["real_items"], r["eval_or_pinned_items"]), (7, 2))
check("the headline: blocked/403/429 outside Reddit", (r["failed_outside_reddit"], r["fetches_outside_reddit"], r["reddit_failed"]),
      (2, 5, 1))
check("…by site, hostnames only", r["failing_sites_outside_reddit"], {"shop.example": 1, "news.example": 1})
check("categories", (F.category("old.reddit.com".replace("old.", "")), F.category("raw.githubusercontent.com"),
                     F.category("search"), F.category("hale.example")), ("reddit", "github", "search", "other"))
text = "\n".join(F.lines_for(r))
check("below the bar (2 < 5): no fallback, said with the bar", ("no Railway fallback" in text, "at least 5 and 10%" in text,
                                                                "a thin week" in text), (True, True, True))
many = [("fetch_url", "refused", f"s{i}.example") for i in range(6)] + [("fetch_url", "ok", "x.example")] * 20
check("at the bar: build it", F.report(many, 0, 7)["matters"], True)
check("never a path or query in what it prints", any(w in text for w in ("?", "/r/", "http")), False)

print("\n── mail_compare --summary ──")


def day(d, mac_only):
    return json.dumps({"at": f"2026-10-{d:02d}T14:10:00Z", "total": {"both": 30, "server_only": 1, "mac_only": mac_only}})


ok7 = [day(d, 0) for d in range(6, 13)]
check("7 consecutive clean days: GO", C.summary(ok7)["go"], True)
check("a day with a Mac mail missing: not yet", C.summary(ok7[:3] + [day(9, 2)] + ok7[4:])["go"], False)
check("a missing day: not consecutive", C.summary([day(d, 0) for d in (5, 6, 7, 9, 10, 11, 12)])["go"], False)
check("fewer than 7 days: not yet", C.summary(ok7[:5])["go"], False)
check("the last run of a day counts", C.summary(ok7 + [day(12, 1)])["go"], False)
log = Path(tempfile.mkdtemp()) / "cmp.jsonl"
log.write_text("\n".join(ok7) + "\n")
out = subprocess.run([sys.executable, str(HERE / "mail_compare.py"), "--summary", str(log)], capture_output=True, text=True).stdout
check("the CLI prints each day and the verdict (INBOX-scoped since 10-06, #955)",
      (out.count("mac_only (inbox) 0"), out.strip().splitlines()[-1]), (7, "GO: 7 consecutive days, INBOX mac_only = 0 on all"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
