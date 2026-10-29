#!/usr/bin/env python3
# gate: hermetic
"""Fetches and searches that go wrong reach the turn log and the scorecard (#934): each fetch_url / web_search call is
logged as [tool, class, site], and a failure adds [tool, "class site"] to tool_fails. A link the person gave is read
before it is searched for, and GitHub repo, file, issue and pull request links come from GitHub's API. No network:
the fetch runtime and the GitHub API are faked.
Run: python3 scripts/test_web_outcomes.py"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-web-test-"))
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
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


O = fc.outcome
print("── what a fetch or a search came to ──")
for text, cls in ((fc.verdict("https://www.reddit.com/r/x/", "", "You've been blocked by network security."), "blocked"),
                  (fc.verdict("https://app.example.com/", "App", ""), "empty"),
                  (fc.error_text("https://shop.example.com/a", RuntimeError("HTTP Error 403: Forbidden")), "refused"),
                  (fc.error_text("https://shop.example.com/a", RuntimeError("HTTP Error 429")), "rate_limited"),
                  (fc.error_text("https://shop.example.com/a", RuntimeError("HTTP Error 404")), "missing"),
                  (fc.error_text("https://shop.example.com/a", RuntimeError("timed out")), "timeout"),
                  (fc.error_text("https://shop.example.com/a", RuntimeError("connection reset")), "error"),
                  ("GitHub says hale/garden doesn't exist, or it's private (HTTP 404).", "missing"),
                  ("Error: url is required", "bad_args"),
                  ("<untrusted>Hale Garden Club\n\nThe fall bulb sale…</untrusted>", "ok")):
    check(f"fetch: {cls}", O("fetch_url", {"url": "https://www.example.com/a"}, text)[0], cls)
check("the site, without www", O("fetch_url", {"url": "https://www.example.com/a?q=1"}, "ok")[1], "example.com")
for text, cls in (("Web search error: HTTP 500", "error"), ("Search error: timeout", "error"),
                  ("No results found.", "empty"), ("No answer from web search.", "empty"),
                  ("Not run: read the link the person gave with fetch_url first (x)", "not_run"),
                  ("Bulbs go in six weeks before the ground freezes.", "ok")):
    check(f"search: {cls}", O("web_search", {"query": "when to plant bulbs"}, text), (cls, "search"))
check("a search never logs its query", "bulbs" in str(O("web_search", {"query": "bulbs"}, "Search error: x")), False)
check("other tools: nothing", O("list_todos", {}, "Error: x"), None)

print("── the links the person gave ──")
check("links, in order, trailing punctuation off",
      fc.links("Is https://github.com/hale/garden-planner any good? Also www.example.org/bulbs, and github.com/ava/seeds."),
      ["https://github.com/hale/garden-planner", "www.example.org/bulbs", "github.com/ava/seeds"])
check("no links: none", fc.links("what's the weather tomorrow?"), [])
check("a repeated link once", fc.links("https://a.example.com/x and https://a.example.com/x"), ["https://a.example.com/x"])

print("── GitHub links through the API ──")
REPO = {"full_name": "hale/garden-planner", "description": "Plan beds  and sowing dates", "language": "Rust",
        "stargazers_count": 12, "pushed_at": "2026-10-01T10:00:00Z", "license": {"spdx_id": "MIT"},
        "topics": ["gardening", "planner"], "archived": False, "fork": False, "homepage": ""}
calls = []


def fake(url, raw=False, readme="# Garden planner\nPlan your beds."):
    calls.append(url)
    if url.endswith("/repos/hale/garden-planner"):
        return REPO
    if url.endswith("/repos/hale/garden-planner/readme"):
        if readme is None:
            raise FileNotFoundError(url)
        return readme
    if "/contents/src/main.rs" in url:
        return "fn main() {}"
    if url.endswith("/issues/7"):
        return {"number": 7, "title": "Frost  dates", "state": "open", "created_at": "2026-09-30T00:00:00Z",
                "user": {"login": "ava"}, "comments": 2, "body": "Add frost dates per zone."}
    if url.endswith("/issues/8"):
        return {"number": 8, "title": "Sowing table", "state": "closed", "created_at": "2026-09-29T00:00:00Z",
                "user": {"login": "leo"}, "comments": 0, "body": "", "pull_request": {"url": "x"}}
    if "/repos/hale/missing" in url:
        raise FileNotFoundError(url)
    raise fc.GitHubLimited("403")


out = fc.github("https://github.com/hale/garden-planner", fake)
check("a repo: name, description, facts, README", (out.splitlines()[0], "Rust · 12 stars · last push 2026-10-01 · MIT" in out,
      "Topics: gardening, planner" in out, out.endswith("README:\n# Garden planner\nPlan your beds.")),
      ("GitHub repository hale/garden-planner: Plan beds and sowing dates", True, True, True))
check("…from api.github.com", calls[0], "https://api.github.com/repos/hale/garden-planner")
check("a repo link with .git and a trailing slash", fc.github("https://github.com/hale/garden-planner.git/", fake) is not None, True)
check("a repo without a README says so", fc.github("github.com/hale/garden-planner",
      lambda u, r=False: fake(u, r, readme=None)).endswith("(No README.)"), True)
check("a file", fc.github("https://github.com/hale/garden-planner/blob/main/src/main.rs", fake),
      "File src/main.rs in hale/garden-planner (main)\n\nfn main() {}")
check("an issue", fc.github("https://github.com/hale/garden-planner/issues/7", fake).splitlines()[:2],
      ["Issue hale/garden-planner#7: Frost dates", "open, opened 2026-09-30 by ava, 2 comments"])
check("a pull request", fc.github("https://github.com/hale/garden-planner/pull/8/files", fake).splitlines()[0],
      "Pull request hale/garden-planner#8: Sowing table")
check("missing or private: said", fc.github("https://github.com/hale/missing", fake),
      "GitHub says hale/missing doesn't exist, or it's private (HTTP 404).")
check("rate-limited: None, the browser reads it", fc.github("https://github.com/ava/other", fake), None)
for url in ("https://github.com/hale", "https://github.com/orgs/hale/repos", "https://github.com/hale/garden-planner/actions",
            "https://gist.github.com/hale/abc", "https://example.com/hale/garden"):
    calls.clear()
    check(f"not read through the API: {url}", (fc.github(url, fake), calls), (None, []))

print("── into the turn log and tool_fails ──")
import agent_runtime_lib as lib  # noqa: E402
lib.begin_turn_info()
real = lib._execute_tool_checked
lib._execute_tool_checked = lambda name, args: {
    "fetch_url": fc.verdict("https://www.reddit.com/r/x/", "", "You've been blocked by network security."),
    "web_search": "Bulbs go in six weeks before frost."}[name]
lib.execute_tool("fetch_url", {"url": "https://www.reddit.com/r/gardening/"})
lib.execute_tool("web_search", {"query": "when to plant bulbs in zone 5"})
info = lib._TURN_INFO.info
check("every call: [tool, class, site]", info.get("web"),
      [["fetch_url", "blocked", "reddit.com"], ["web_search", "ok", "search"]])
check("a failure: [tool, class site] in tool_fails", info.get("tool_fails"), [["fetch_url", "blocked reddit.com"]])
check("the query never reaches the log", "zone 5" in str(info), False)
lib._execute_tool_checked = real
check("web is one of the logged keys", '"tool_fails", "web",' in Path(lib.__file__).read_text(), True)

print("── a given link is read before it is searched for ──")
lib.begin_turn_info()
real_rt, real_search = lib._runtime_request, lib._gemini_search
os.environ.pop("BRAVE_API_KEY", None)
lib._gemini_search = lambda q, *a, **k: "Bulbs go in six weeks before frost."   # never the real search
try:
    lib._TURN_IO.links, lib._TURN_IO.tools_used = ["https://github.com/hale/garden-planner"], set()
    lib._TURN_IO.outcomes, lib._TURN_IO.failed_calls = [], {}
    out = lib.execute_tool("web_search", {"query": "hale garden-planner github"})
    check("search before the fetch: not run, pointed at fetch_url", out.startswith("Not run: read the link the person gave "
          "with fetch_url first (https://github.com/hale/garden-planner)"), True)
    check("…its tool_fails head carries no link", lib._TURN_INFO.info["tool_fails"][-1],
          ["web_search", "Not run: read the link the person gave w"])
    lib._runtime_request = lambda *a, **k: {"title": "Garden planner", "content": "Plan your beds."}
    real_gh = fc._gh_get
    fc._gh_get = fake
    out = lib.execute_tool("fetch_url", {"url": "https://github.com/hale/garden-planner"})
    check("the GitHub link comes from the API, as untrusted web text", ("GitHub repository hale/garden-planner" in out,
          "untrusted" in out), (True, True))
    out = lib.execute_tool("fetch_url", {"url": "https://github.com/hale/missing"})
    check("a missing repo: said, logged as missing", (out.startswith("GitHub says hale/missing"),
          lib._TURN_INFO.info["web"][-1]), (True, ["fetch_url", "missing", "github.com"]))
    fc._gh_get = real_gh
    out = lib.execute_tool("web_search", {"query": "garden planner alternatives"})
    check("after the fetch, search runs", "six weeks" in out, True)
    lib._TURN_IO.links, lib._TURN_IO.tools_used, lib._TURN_IO.jev_source = ["https://news.example.com/x"], set(), "proactive"
    out = lib.execute_tool("web_search", {"query": "x"})
    check("a background turn's links never push a fetch", "six weeks" in out, True)
    lib._TURN_IO.jev_source = None
    lib._TURN_IO.links, lib._TURN_IO.tools_used = [], set()
    out = lib.execute_tool("web_search", {"query": "x"})
    check("no link given: search runs", "six weeks" in out, True)
finally:
    lib._runtime_request, lib._gemini_search = real_rt, real_search
    lib._TURN_IO.links = lib._TURN_IO.tools_used = lib._TURN_IO.outcomes = lib._TURN_IO.failed_calls = None

print("── a pasted GitHub link declares the web tools ──")
check("github.com/… picks the web group", {"fetch_url", "web_search"} <= lib.narrowed_tools("is github.com/hale/garden any good"), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
