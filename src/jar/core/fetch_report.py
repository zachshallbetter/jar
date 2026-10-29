#!/usr/bin/env python3
"""The #936 measurement: how titan's web fetches went since #942 started logging them (00:09 UTC 10-05), from the turn
log's `web` key ([tool, class, site]; the site is a hostname only, never a path or query). Read-only, counts only.

Real turns only (a person's own; eval, canary and pinned turns are counted apart). The headline is #936's number:
fetch_url results that were blocked, refused (403) or rate-limited (429) outside Reddit (Reddit's block has its own fix,
#899). The recommendation line applies #936's bar as stated here: a Railway fallback is worth building only if those
are at least BAR_COUNT fetches and BAR_SHARE of real fetches outside Reddit over the week.

  python3 scripts/fetch_report.py [--since 2026-10-05T00:09:00Z] [--days 7] [--log /ai/logs/agent-turns.jsonl]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SINCE = "2026-10-05T00:09:00Z"     # #942 deployed (c61cf51)
BAR_COUNT = 5
BAR_SHARE = 0.10
FAILED = frozenset({"blocked", "refused", "rate_limited"})
CATEGORIES = (("reddit", ("reddit.com", "redd.it")), ("github", ("github.com", "githubusercontent.com")),
              ("video", ("youtube.com", "youtu.be", "vimeo.com")),
              ("social", ("x.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com", "linkedin.com")),
              ("shopping", ("amazon.com", "ebay.com", "etsy.com", "walmart.com", "target.com", "bestbuy.com")),
              ("news", ("nytimes.com", "wsj.com", "washingtonpost.com", "bbc.com", "bbc.co.uk", "cnn.com", "reuters.com",
                        "apnews.com", "npr.org", "theguardian.com", "spokesman.com")),
              ("reference", ("wikipedia.org", "wikimedia.org", "stackoverflow.com", "stackexchange.com")))


def category(site: str) -> str:
    s = str(site or "").lower()
    if s == "search":
        return "search"
    for name, hosts in CATEGORIES:
        if any(s == h or s.endswith("." + h) for h in hosts):
            return name
    return "other"


def _ts(v: str) -> float:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()


def read(lines: Iterable[str], since: float, until: float) -> tuple[list[tuple[str, str, str]], int]:
    """(real turns' web items, how many items came from eval, canary or pinned turns)."""
    real, other = [], 0
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not (since <= float(r.get("ts") or 0) < until):
            continue
        items = [tuple(x[:3]) for x in (r.get("web") or []) if isinstance(x, list) and len(x) >= 3]
        if not items:
            continue
        if r.get("source", "user") != "user" or r.get("pin"):
            other += len(items)
            continue
        real += [(str(t), str(c), str(s)) for t, c, s in items]
    return real, other


def routes(lines: Iterable[str], since: float, until: float) -> dict[str, int]:
    """#936: real turns' fetch_url results by route (railway | local; entries logged before the route was), and how many
    went through the broker and found it down. Counts only."""
    out: Counter = Counter()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not (since <= float(r.get("ts") or 0) < until) or r.get("source", "user") != "user" or r.get("pin"):
            continue
        for x in r.get("web") or []:
            if isinstance(x, list) and len(x) >= 3 and x[0] == "fetch_url":
                route = str(x[3]) if len(x) >= 4 else "unrecorded"
                out[route] += 1
                if x[1] == "broker_down":
                    out["railway down"] += 1
    return dict(sorted(out.items()))


DEVICE_PLATFORMS = ("mac", "iphone", "ipad", "watch", "tv")


def by_device(lines: Iterable[str], since: float, until: float) -> dict[str, int]:
    """#1366: real turns' device-tool calls by platform and tool ("iphone list_calendar_events": n), from the turn log's
    tool_devices. Counts only; an entry outside the platform vocabulary is skipped."""
    out: Counter = Counter()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not (since <= float(r.get("ts") or 0) < until) or r.get("source", "user") != "user" or r.get("pin"):
            continue
        for x in r.get("tool_devices") or []:
            if isinstance(x, list) and len(x) == 2 and x[1] in DEVICE_PLATFORMS:
                out[f"{x[1]} {x[0]}"] += 1
    return dict(sorted(out.items()))


def report(items: list[tuple[str, str, str]], other: int, days: float) -> dict[str, Any]:
    fetches = [(c, s) for t, c, s in items if t == "fetch_url"]
    outside = [(c, s) for c, s in fetches if category(s) != "reddit"]
    failed_out = [(c, s) for c, s in outside if c in FAILED]
    share = len(failed_out) / len(outside) if outside else 0.0
    return {
        "days": days, "real_items": len(items), "eval_or_pinned_items": other,
        "by_tool_class": {f"{t} {c}": n for (t, c), n in sorted(Counter((t, c) for t, c, _ in items).items())},
        "fetch_by_category": {f"{category(s)} {c}": n for (s, c), n in
                              sorted(Counter((category(s), c) for c, s in fetches).items())},
        "fetches": len(fetches), "fetches_outside_reddit": len(outside),
        "failed_outside_reddit": len(failed_out), "share_outside_reddit": round(share, 3),
        "failing_sites_outside_reddit": dict(Counter(s for _, s in failed_out).most_common(8)),
        "reddit_failed": sum(1 for c, s in fetches if category(s) == "reddit" and c in FAILED),
        "matters": len(failed_out) >= BAR_COUNT and share >= BAR_SHARE,
    }


def lines_for(r: dict[str, Any]) -> list[str]:
    out = [f"fetches over {r['days']:g} d (real turns): {r['fetches']} fetch_url, {r['real_items'] - r['fetches']} web_search "
           f"({r['eval_or_pinned_items']} more from eval or pinned turns, not counted)",
           f"#936 headline: {r['failed_outside_reddit']} of {r['fetches_outside_reddit']} fetches outside Reddit were "
           f"blocked, refused (403) or rate-limited (429) ({r['share_outside_reddit']:.0%}); Reddit's own: {r['reddit_failed']}",
           "by class: " + (", ".join(f"{k} {n}" for k, n in r["by_tool_class"].items()) or "none"),
           "by site category: " + (", ".join(f"{k} {n}" for k, n in r["fetch_by_category"].items()) or "none")]
    if r["failing_sites_outside_reddit"]:
        out.append("sites behind it: " + ", ".join(f"{s} {n}" for s, n in r["failing_sites_outside_reddit"].items()))
    out.append(("recommendation: build the Railway fallback (#936): the failures outside Reddit meet the bar"
                if r["matters"] else
                "recommendation: no Railway fallback (#936): the failures outside Reddit are below the bar")
               + f" (at least {BAR_COUNT} and {BAR_SHARE:.0%} of real fetches outside Reddit)")
    if r["fetches_outside_reddit"] < 20:
        out.append(f"note: only {r['fetches_outside_reddit']} real fetches outside Reddit; a thin week, read it as such")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default=SINCE)
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--log", default=os.environ.get("AGENT_TURN_LOG", "/ai/logs/agent-turns.jsonl"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    since = _ts(a.since)
    log = Path(a.log)
    items, other = read(log.read_text().splitlines() if log.exists() else [], since, since + a.days * 86400)
    r = report(items, other, a.days)
    r["fetch_routes"] = routes(log.read_text().splitlines() if log.exists() else [], since, since + a.days * 86400)
    r["device_tools"] = by_device(log.read_text().splitlines() if log.exists() else [], since, since + a.days * 86400)
    print(json.dumps(r, indent=1) if a.json else "\n".join(lines_for(r) + [
        "by route: " + (", ".join(f"{k} {n}" for k, n in r["fetch_routes"].items()) or "none"),
        "device tools by platform (real turns): " + (", ".join(f"{k} {n}" for k, n in r["device_tools"].items()) or "none")]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
