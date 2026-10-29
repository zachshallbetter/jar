#!/usr/bin/env python3
# gate: hermetic
"""The public /now feed (titan-node#1043): GitHub read with no token, ever; only allowlisted repos, a private one
dropped; titan's own repos never; household names never in the output (a canary from the people store); the 6 h
cache; and the route open without a token. GitHub is faked; fictional people (Alex, Robin, Ava). No network.
Run: python3 scripts/test_now_feed.py"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-now-feed-test-"))
for k, v in {"NOW_FEED_CACHE": "now_cache.json", "HOUSEHOLD_DB": "household.db", "HOUSEHOLD_PREFS": "household.json",
             "KB_ACL_DB": "kb_acl.db", "AGENT_MEMORY_DIR": "memory", "AGENT_TURN_LOG": "turns.jsonl"}.items():
    os.environ[k] = str(_TMP / v)
os.environ["GITHUB_TOKEN"] = os.environ["GH_TOKEN"] = "ghp_should_never_be_sent"
sys.path.insert(0, str(Path(__file__).parent))
import now_feed as NF  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


NOW = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)
API = NF.API + "/repos/alexhale"
GH = {
    f"{API}/harbor-engine": {"name": "harbor-engine", "private": False, "visibility": "public", "description": "An engine.",
                             "html_url": "https://github.com/alexhale/harbor-engine", "homepage": "https://harbor.example",
                             "pushed_at": "2026-10-05T20:00:00Z", "stargazers_count": 12},
    f"{API}/harbor-engine/releases/latest": {"tag_name": "v0.4.0", "published_at": "2026-10-01T10:00:00Z"},
    f"{API}/harbor-engine/pulls?state=closed&sort=updated&direction=desc&per_page=30": [
        {"title": "Faster resolver", "number": 41, "merged_at": "2026-10-04T12:00:00Z"},
        {"title": "Bump actions/checkout from 4 to 5", "number": 42, "merged_at": "2026-10-05T12:00:00Z",
         "user": {"login": "dependabot[bot]", "type": "Bot"}},
        {"title": "Fix Robin's import bug", "number": 40, "merged_at": "2026-10-03T12:00:00Z"},
        {"title": "Closed, never merged", "number": 39, "merged_at": None},
        {"title": "Old change", "number": 12, "merged_at": "2026-07-01T12:00:00Z"},
        {"title": "Docs", "number": 38, "merged_at": "2026-10-02T12:00:00Z"},
        {"title": "Tests", "number": 37, "merged_at": "2026-10-01T12:00:00Z"},
        {"title": "Refactor", "number": 36, "merged_at": "2026-09-30T12:00:00Z"}],
    f"{API}/lakeside-notes": {"name": "lakeside-notes", "private": False, "visibility": "public",
                              "description": "Notes about Ava's school", "html_url": "u", "pushed_at": "2026-09-01T00:00:00Z",
                              "stargazers_count": 0},
    f"{API}/lakeside-notes/releases/latest": None,
    f"{API}/lakeside-notes/tags?per_page=1": [{"name": "v1"}],
    f"{API}/lakeside-notes/pulls?state=closed&sort=updated&direction=desc&per_page=30": [],
    f"{API}/secret-plans": None,   # private: the unauthenticated API can't see it (404)
}
fetched: list[str] = []


def fake(url):
    fetched.append(url)
    return GH.get(url)


CFG = {"owner": "alexhale", "repos": [{"name": "harbor-engine", "blurb": "Where Alex's engine lives."},
                                      {"name": "secret-plans"}, "lakeside-notes", {"name": "titan-node"},
                                      {"name": "Jellybean"}],
       "building": ["A garden planner", "Robin's birthday app"], "titan": "A private household assistant and its apps."}
import re  # noqa: E402
HIDE = [re.compile(r"(?i)\b" + w + r"\b") for w in ("Robin", "Ava")]

print("── no token, ever ──")
req = NF._request(NF.API + "/repos/alexhale/harbor-engine")
check("the request carries no Authorization (a GITHUB_TOKEN in the environment is ignored)",
      [k for k in req.header_items() if k[0].lower() in ("authorization", "cookie")], [])
sent: list[dict] = []


class _Opener:
    def open(self, r, timeout=None):
        sent.append(dict(r.header_items()) | dict(r.unredirected_hdrs))
        raise urllib.error.HTTPError(r.full_url, 404, "nf", {}, None)


real_build = urllib.request.build_opener
urllib.request.build_opener = lambda *a: _Opener()
try:
    check("a 404 (private or gone) is None", NF._get(NF.API + "/repos/alexhale/x"), None)
finally:
    urllib.request.build_opener = real_build
check("what actually went out had no credential", any(k.lower() in ("authorization", "cookie") for k in sent[0]), False)
try:
    NF._request("https://example.com/repos/x")
    check("only the GitHub API is ever called", "called", "refused")
except ValueError:
    check("only the GitHub API is ever called", "refused", "refused")

print("── the allowlist, public only ──")
feed = NF.build(fake, CFG, NOW, HIDE)
check("only the public allowlisted repos", [r["name"] for r in feed["repos"]], ["harbor-engine", "lakeside-notes"])
check("a private repo on the allowlist is dropped", any(r["name"] == "secret-plans" for r in feed["repos"]), False)
check("titan's own repos never, whatever the allowlist says",
      any(u.endswith(("/titan-node", "/Jellybean")) for u in fetched), False)
h = feed["repos"][0]
check("per repo: the facts", (h["release"], h["stars"], h["homepage"], h["pushed_at"]),
      ({"tag": "v0.4.0", "date": "2026-10-01T10:00:00Z"}, 12, "https://harbor.example", "2026-10-05T20:00:00Z"))
check("up to 3 PRs merged in the last 30 days, newest first, bots' left out", [p["number"] for p in h["merged"]], [41, 38, 37])
check("no release: the latest tag, no date", feed["repos"][1]["release"], {"tag": "v1", "date": None})
check("titan is the hand-written line only", feed["titan"], "A private household assistant and its apps.")
check("sources and generated_at", (feed["generated_at"], all(s.startswith(NF.API) for s in feed["sources"])),
      ("2026-10-06T01:00:00+00:00", True))

print("── household names never appear (canary) ──")
out = json.dumps(feed)
check("no household member's name anywhere in the output", [w for w in ("Robin", "Ava") if w in out], [])
check("…a PR title naming one is left out, the rest kept", "Fix Robin" in out or h["merged"][0]["title"] != "Faster resolver",
      False)
check("…a description naming one is left out", feed["repos"][1]["description"], None)
check("…and a building line", feed["building"], ["A garden planner"])
import household_identity as hh  # noqa: E402
st = hh.get_store()
for name in ("Robin Hale", "Ava Hale"):
    try:
        st.add_person(name, "adult" if name.startswith("Robin") else "kid")
    except Exception:  # noqa: BLE001
        pass
pats = NF._names_to_hide()
check("the guard's names come from the people store (never the site's owner)",
      sorted(p.pattern for p in pats if "Robin" in p.pattern or "Ava" in p.pattern) != [] and
      not any(hh.ADMIN_PERSON_ID in p.pattern.lower() for p in pats), True)
check("the canary with the store's own names", [w for w in ("Robin", "Ava") if w in json.dumps(NF.build(fake, CFG, NOW))], [])

print("── cached 6 h ──")
fetched.clear()
t0 = NOW.timestamp()
NF._mem.clear()
NF.get(t0, fake, CFG, HIDE)
n = len(fetched)
NF.get(t0 + 3600, fake, CFG, HIDE)
check("within 6 h: no new GitHub call", len(fetched), n)
NF._mem.clear()
NF.get(t0 + 3 * 3600, fake, CFG, HIDE)
check("…even after a restart (the cache file)", len(fetched), n)
NF.get(t0 + 6 * 3600 + 1, fake, CFG, HIDE)
check("after 6 h: fetched again", len(fetched), 2 * n)
check("n calls a refresh stays far under 60 an hour", n <= 12, True)

print("── the route ──")
import agent_api_server as api  # noqa: E402
check("open without a token", "/v1/public/now" in api._PUBLIC_PATHS, True)
check("…and public-read for every role", all(hh.route_allowed(r, "GET", "/v1/public/now") for r in hh.ROLES), True)
from fastapi.testclient import TestClient  # noqa: E402
NF._mem.clear()
NF.CACHE.write_text(json.dumps({**feed, "_at": __import__("time").time()}))
r = TestClient(api.app).get("/v1/public/now")
check("GET with no token: 200 and the feed", (r.status_code, [x["name"] for x in r.json()["repos"]]),
      (200, ["harbor-engine", "lakeside-notes"]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
