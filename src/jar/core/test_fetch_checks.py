#!/usr/bin/env python3
# gate: hermetic
"""fetch_url on a site that won't let titan read it (#826): Reddit's block page, a robot check or an empty JavaScript
shell is said plainly with the site's name, never handed on as the page; a Reddit post still gets its title from
Reddit's oEmbed service. No network: the fetch runtime and oEmbed are faked.
Run: python3 scripts/test_fetch_checks.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-fetch-test-"))
for k, v in {"HOUSEHOLD_DB": "household.db", "CONNECTORS_LEDGER": "ledger.db", "AGENT_MEMORY_DIR": "memory",
             "KB_ACL_DB": "kb_acl.db", "HOUSEHOLD_PREFS": "household.json", "DAILY_LOG_DIR": "daily",
             "PROACTIVE_DB": "proactive.db", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
sys.path.insert(0, str(Path(__file__).parent))
import fetch_checks as fc  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r:.200} expected={expected!r:.200}")


# What titan's fetch runtime returned for reddit.com on 10-04, with success: true.
REDDIT_BLOCK = ("You've been blocked by network security.\nIf you think you've been blocked by mistake, file a ticket "
                "below and we'll look into it.\nFile a ticket")
ARTICLE = "Hale Garden Club\n\n" + ("The fall bulb sale runs through Saturday at the community hall. " * 40)

print("── what is a page and what isn't ──")
check("Reddit's block page: blocked, by name", (fc.verdict("https://www.reddit.com/r/gardening/", "", REDDIT_BLOCK) or "").startswith(
      "reddit.com blocked titan's request"), True)
check("old.reddit's sign-in wall: blocked", fc.verdict("https://old.reddit.com/r/gardening/", "", "Welcome to Reddit Skip to main content") is not None, True)
check("a Cloudflare check: blocked", (fc.verdict("https://shop.example.com/", "Just a moment...", "Checking your browser") or "")[:24],
      "shop.example.com blocked")
check("an empty JavaScript shell (x.com): said as no readable text", (fc.verdict("https://x.com/home", "", "") or "").startswith(
      "x.com sent back no readable text"), True)
check("a short page is still a page", fc.verdict("https://example.test/p", "Deals", "Two for one on Tuesdays."), None)
check("a real page: passed on", fc.verdict("https://news.example.org/", "News", ARTICLE), None)
check("a long article that mentions a captcha: still a page",
      fc.verdict("https://blog.example.org/", "Why sites use a captcha", ARTICLE + " Some sites show a captcha."), None)
check("the message never claims a workaround", "haven't tried to get around it" in fc.verdict("https://www.reddit.com/", "", REDDIT_BLOCK),
      True)

print("── a Reddit post: its title from Reddit's oEmbed service ──")
asked: list[str] = []


def fake_get(u):
    asked.append(u)
    return {"title": "Best bulbs for a shady yard?", "author_name": "robin_hale",
            "html": '<blockquote><a href="https://www.reddit.com/r/gardening/comments/abc123/best_bulbs/">x</a>'}


emb = fc.reddit_embed("https://old.reddit.com/r/gardening/comments/abc123/best_bulbs/?utm=x", get=fake_get)
check("title, subreddit, author, and that it's only that", emb.splitlines(),
      ["Reddit post: Best bulbs for a shady yard?", "in r/gardening, by u/robin_hale",
       "(Only the title Reddit's embed service shares: Reddit blocks titan from reading the thread.)"])
check("asked Reddit's own oEmbed endpoint for the post link", asked[0].startswith("https://www.reddit.com/oembed?url=https%3A%2F%2Fold.reddit.com%2Fr%2Fgardening%2Fcomments%2Fabc123"),
      True)
check("a subreddit page isn't a post: no embed", fc.reddit_embed("https://www.reddit.com/r/gardening/", get=fake_get), None)
check("oEmbed down: no embed", fc.reddit_embed("https://www.reddit.com/r/a/comments/x1/", get=lambda u: 1 / 0), None)
check("a deleted post's empty title: no embed", fc.reddit_embed("https://www.reddit.com/r/a/comments/x1/", get=lambda u: {"title": ""}), None)

print("── a failed fetch in plain words ──")
check("403", fc.error_text("https://www.reddit.com/r/a/", Exception("HTTP Error 403: Forbidden")),
      "reddit.com refused titan's request (HTTP 403), so I couldn't read that page.")
check("429", fc.error_text("https://api.example.com/x", Exception("HTTP Error 429: Too Many Requests")).startswith(
      "api.example.com is rate-limiting titan right now"), True)
check("timeout", fc.error_text("https://slow.example.com/", Exception("timed out")),
      "slow.example.com didn't answer in time, so I couldn't read that page.")

print("── fetch_url itself ──")
import agent_runtime_lib as lib  # noqa: E402
real_rt, real_get = lib._runtime_request, fc._get_json
try:
    lib._runtime_request = lambda *a, **k: {"url": "x", "title": "", "content": REDDIT_BLOCK, "success": True}
    fc._get_json = fake_get
    out = lib.execute_tool("fetch_url", {"url": "https://www.reddit.com/r/gardening/comments/abc123/best_bulbs/"})
    check("the block is said, the block page's text isn't passed on", ("blocked titan's request" in out, "file a ticket" in out.lower()),
          (True, False))
    check("…with the post's title, as untrusted web text", ("Best bulbs for a shady yard?" in out, "untrusted" in out), (True, True))
    lib._runtime_request = lambda *a, **k: {"title": "Hale Garden Club", "content": ARTICLE}
    out = lib.execute_tool("fetch_url", {"url": "https://garden.example.org/"})
    check("a real page still comes through", ("fall bulb sale" in out, "blocked" in out), (True, False))

    def boom(*a, **k):
        raise RuntimeError("HTTP Error 403: Forbidden")
    lib._runtime_request = boom
    check("a refused fetch, by name", lib.execute_tool("fetch_url", {"url": "https://www.reddit.com/r/x/"}),
          "reddit.com refused titan's request (HTTP 403), so I couldn't read that page.")
finally:
    lib._runtime_request, fc._get_json = real_rt, real_get

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
