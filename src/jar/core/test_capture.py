#!/usr/bin/env python3
# gate: hermetic
"""Capture over crawl (#858): a page saved from the person's own browser, private in the KB's web zone, one item per
address (updated, never duplicated), Undo for its owner only; a link alone is fetched by titan, a walled or blocked one
refused with 422's body (never fetched when walled). The fetch is always a fake here.

Run: python3 scripts/test_capture.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["CAPTURE_DB"] = str(Path(tempfile.mkdtemp()) / "capture.db")
sys.path.insert(0, os.path.dirname(__file__))
import capture  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed += ok
    failed += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} expected={expected!r}")


indexed = []
index = lambda sid, title, text, st, md, owner=None, audience=None: indexed.append((sid, title, st, md, owner, audience))  # noqa: E731
PAGE = "Robin asked how the dishwasher filter comes out. Twist the cylinder left, lift, rinse, and seat it again."

print("── the address ──")
check("tracking params and the fragment go", capture.clean_url("https://Example.com/a?utm_source=x&id=7#top"),
      "https://example.com/a?id=7")
for bad in ("javascript:alert(1)", "file:///etc/passwd", "", "example.com/x"):
    try:
        capture.clean_url(bad)
        check(f"refused: {bad!r}", False, True)
    except capture.CaptureError:
        check(f"refused: {bad!r}", True, True)

print("── save, update, unchanged ──")
out = capture.save("alex", {"url": "https://reddit.com/r/x/comments/1?utm_source=share", "title": "Filter",
                            "text": PAGE, "app": "safari"}, index=index)
check("saved, private to the person, web zone", (out["status"], indexed[-1][2], indexed[-1][4], indexed[-1][5]),
      ("saved", "web", "alex", "private"))
check("provenance says it came from the person's browser", (indexed[-1][3]["via"], indexed[-1][3]["captured_from"]),
      ("capture", "safari"))
check("the same id ingest_url gives that address", out["id"], capture.item_id("https://reddit.com/r/x/comments/1", "alex"))
again = capture.save("alex", {"url": "https://reddit.com/r/x/comments/1#c2", "title": "Filter", "text": PAGE}, index=index)
check("the same page again: unchanged, not re-indexed", (again["status"], again["id"], len(indexed)), ("unchanged", out["id"], 1))
upd = capture.save("alex", {"url": "https://reddit.com/r/x/comments/1", "title": "Filter", "text": PAGE + " Edited."}, index=index)
check("changed text: updated in place (same id)", (upd["status"], upd["id"], len(indexed)), ("updated", out["id"], 2))
other = capture.save("jordan", {"url": "https://reddit.com/r/x/comments/1", "text": PAGE}, index=index)
check("another person's capture of it is their own item", other["id"] != out["id"], True)
check("an unknown app is recorded as other", capture.save("alex", {"url": "https://a.example/p", "text": PAGE, "app": "evil"},
                                                          index=index) and indexed[-1][3]["captured_from"], "other")
def no_fetch(url):
    raise AssertionError(f"fetched {url}")


try:
    capture.save("alex", {"url": "https://a.example/q", "text": "  "}, index=index, fetch=lambda u: {"title": "", "content": ""})
    check("no text, and titan's fetch finds none: refused", False, True)
except capture.CaptureNotFetched as e:
    check("no text, and titan's fetch finds none: refused", e.body(), {"error": "blocked", "message": capture.BLOCKED_MESSAGE, "site": "a.example"})
check("a long title is capped at 300", len(capture.save("alex", {"url": "https://a.example/t", "title": "x" * 900, "text": PAGE}, index=index) and indexed[-1][1]), 300)

print("── a link alone: titan fetches it (#858 follow-up) ──")
ARTICLE = "How to descale a kettle: fill it halfway with equal parts vinegar and water, boil, leave it an hour, rinse twice."
asked = []


def fetch_ok(url):
    asked.append(url)
    return {"title": "Descale a kettle", "content": ARTICLE}


n = len(indexed)
got = capture.save("alex", {"url": "https://kettles.example/descale?utm_source=x", "app": "iphone"}, index=index, fetch=fetch_ok)
check("fetched once, at the cleaned address", asked, ["https://kettles.example/descale"])
check("saved with the page's own title and text, via capture-fetch", (got["status"], got["title"], got["fetched"], indexed[-1][3]["via"],
      indexed[-1][3]["captured_from"], len(indexed) - n), ("saved", "Descale a kettle", True, "capture-fetch", "iphone", 1))
check("…private to the person, web zone", (indexed[-1][2], indexed[-1][4], indexed[-1][5]), ("web", "alex", "private"))
titled = capture.save("alex", {"url": "https://kettles.example/other", "title": "My kettle notes", "text": "see link"}, index=index, fetch=fetch_ok)
check("a title the app sent wins over the page's; a few words of text still means fetch", (titled["title"], titled["fetched"]), ("My kettle notes", True))
asked.clear()
with_text = capture.save("alex", {"url": "https://kettles.example/third", "text": ARTICLE}, index=index, fetch=no_fetch)
check("with the page's text: never fetched, as before", (with_text["fetched"], asked, indexed[-1][3]["via"]), (False, [], "capture"))


def refused(label, body, fetch, want):
    try:
        capture.save("alex", body, index=index, fetch=fetch)
        check(label, "saved", want)
    except capture.CaptureNotFetched as e:
        check(label, (e.kind, e.message if want[0] == "blocked" else e.message[:40], e.site), want)


for url, site in (("https://www.reddit.com/r/x/comments/abc/t/", "reddit.com"), ("https://www.linkedin.com/posts/robin-123", "linkedin.com"),
                  ("https://lnkd.in/abc", "lnkd.in"), ("https://old.reddit.com/r/x", "reddit.com"), ("https://m.facebook.com/p/1", "facebook.com"),
                  ("https://x.com/hale/status/1", "x.com")):
    refused(f"walled, never fetched: {site}", {"url": url}, no_fetch, ("blocked", capture.BLOCKED_MESSAGE, site))
refused("a block page is blocked", {"url": "https://shop.example/p"}, lambda u: {"title": "Just a moment...", "content": "Verify you are human"},
        ("blocked", capture.BLOCKED_MESSAGE, "shop.example"))
refused("titan refused (403) is blocked", {"url": "https://shop.example/p"}, lambda u: (_ for _ in ()).throw(RuntimeError("HTTP Error 403: Forbidden")),
        ("blocked", capture.BLOCKED_MESSAGE, "shop.example"))
refused("missing (404) is unreachable, in plain words", {"url": "https://shop.example/gone"},
        lambda u: (_ for _ in ()).throw(RuntimeError("HTTP Error 404: Not Found")), ("unreachable", "shop.example says that page doesn't exis", "shop.example"))
refused("timed out is unreachable", {"url": "https://slow.example/p"}, lambda u: (_ for _ in ()).throw(TimeoutError("timed out")),
        ("unreachable", "slow.example didn't answer in time, so I", "slow.example"))
refused("under 20 characters fetched is blocked", {"url": "https://tiny.example/p"}, lambda u: {"title": "T", "content": "Hi there."},
        ("blocked", capture.BLOCKED_MESSAGE, "tiny.example"))
before = len(indexed)
try:
    capture.save("alex", {"url": "https://www.reddit.com/r/x"}, index=index, fetch=no_fetch)
except capture.CaptureNotFetched:
    pass
check("…nothing indexed for a walled site", len(indexed), before)
check("the 422 body is error, message, site", capture.CaptureNotFetched("blocked", capture.BLOCKED_MESSAGE, "reddit.com").body(),
      {"error": "blocked", "message": "Open it in Safari and share from there", "site": "reddit.com"})
check("a CaptureNotFetched is still a CaptureError (callers that catch that keep working)",
      issubclass(capture.CaptureNotFetched, capture.CaptureError), True)

print("── Undo ──")
deleted = []
check("someone else can't remove it", capture.remove("jordan", out["id"], delete=deleted.append), False)
check("…and nothing was deleted", deleted, [])
check("the owner can", capture.remove("alex", out["id"], delete=deleted.append), True)
check("…its KB copy goes", deleted, [out["id"]])
check("a second Undo finds nothing", capture.remove("alex", out["id"], delete=deleted.append), False)
check("saving it again after Undo is a new save", capture.save("alex", {"url": "https://reddit.com/r/x/comments/1",
                                                                       "text": PAGE}, index=index)["status"], "saved")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
