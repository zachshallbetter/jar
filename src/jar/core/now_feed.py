#!/usr/bin/env python3
"""The public /now feed for zachshallbetter.com (titan-node#1043): GET /v1/public/now.

Public by construction:
  - GitHub is read through its REST API with NO token at all (_get refuses to send one), so a private repo, a private
    project item or a private event can't be seen, let alone shown;
  - only the repos in config/now_feed.json; one the unauthenticated API can't see (private, renamed, gone) is dropped
    and logged;
  - titan itself is one hand-written line from the config. Never household data, todos, cards, issues, or anything
    from titan-node or jellybean;
  - a last guard: any text naming a household member (other than the site's owner) is left out.
Per repo: name, description, homepage, url, the latest release (tag and date) or tag, pushed_at, stars, and up to 3
PRs merged in the last 30 days (title, number, merged_at; bots' PRs left out). Cached CACHE_TTL_S (6 h), in memory and in a file, so the
60-requests-an-hour unauthenticated limit is never near (about 3 calls a repo a refresh).

  python3 scripts/now_feed.py            print the feed (fetches if the cache is stale)
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

API = "https://api.github.com"
CONFIG = Path(os.environ.get("NOW_FEED_CONFIG", str(Path(__file__).resolve().parent.parent / "config" / "now_feed.json")))
CACHE = Path(os.environ.get("NOW_FEED_CACHE", "/ai/datasets/connectors/now_feed_cache.json"))
CACHE_TTL_S = 6 * 3600
MERGED_DAYS = 30
MAX_PRS = 3
NEVER = frozenset({"titan-node", "jellybean"})   # titan's own repos: never in the feed, whatever the allowlist says
_lock = threading.Lock()
_mem: dict[str, Any] = {}

Fetch = Callable[[str], Any]


class NoTokenError(AssertionError):
    pass


def _request(url: str) -> urllib.request.Request:
    """A GitHub API request that carries no credential, ever."""
    if not url.startswith(API + "/"):
        raise ValueError("only the GitHub REST API")
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "titan-now-feed",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    if any(k.lower() in ("authorization", "cookie") for k in req.headers) or req.unredirected_hdrs:
        raise NoTokenError("the /now feed never sends a credential")
    return req


def _get(url: str) -> Any:
    """GET one GitHub API URL without a token. → the JSON, or None for 404 (private or missing)."""
    req = _request(url)
    opener = urllib.request.build_opener()   # no auth handlers, no netrc
    try:
        with opener.open(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _names_to_hide() -> list[re.Pattern]:
    """Household members' names and aliases (everyone but the admin, who owns the site), as word patterns."""
    try:
        import household_identity as hh
        people = [p for p in hh.get_store().people() if str(p.get("id")) != hh.ADMIN_PERSON_ID]
    except Exception:  # noqa: BLE001 - no people store here: nothing to hide by name
        people = []
    words = {w for p in people for w in [str(p.get("name") or "").split(" ")[0], *(p.get("aliases") or [])]
             if len(str(w)) >= 3}
    return [re.compile(r"(?i)\b" + re.escape(str(w)) + r"\b") for w in sorted(words)]


def _clean(text: Any, hide: list[re.Pattern]) -> str | None:
    t = " ".join(str(text or "").split())[:300]
    if not t:
        return None
    return None if any(rx.search(t) for rx in hide) else t


def repo(owner: str, name: str, blurb: str, fetch: Fetch, hide: list[re.Pattern], now: datetime,
         sources: list[str]) -> dict[str, Any] | None:
    if name.lower() in NEVER:
        print(f"[now_feed] {name}: titan's own repo; never in the feed", file=sys.stderr)
        return None
    base = f"{API}/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"
    sources.append(base)
    r = fetch(base)
    if not r or r.get("private") or r.get("visibility", "public") != "public":
        print(f"[now_feed] {name}: not visible without a token; left out", file=sys.stderr)
        return None
    sources.append(base + "/releases/latest")
    rel = fetch(base + "/releases/latest")
    release = None
    if rel and rel.get("tag_name"):
        release = {"tag": rel["tag_name"], "date": rel.get("published_at") or rel.get("created_at")}
    else:
        sources.append(base + "/tags?per_page=1")
        tags = fetch(base + "/tags?per_page=1") or []
        if tags:
            release = {"tag": tags[0].get("name"), "date": None}
    sources.append(base + "/pulls?state=closed&sort=updated&direction=desc&per_page=30")
    pulls = fetch(base + "/pulls?state=closed&sort=updated&direction=desc&per_page=30") or []
    since = now - timedelta(days=MERGED_DAYS)
    merged = []
    for p in pulls:
        at = p.get("merged_at")
        if not at or datetime.fromisoformat(at.replace("Z", "+00:00")) < since:
            continue
        user = p.get("user") or {}
        if user.get("type") == "Bot" or str(user.get("login") or "").endswith("[bot]"):
            continue   # a dependency bump says nothing about what's being built
        title = _clean(p.get("title"), hide)
        if title:
            merged.append({"title": title, "number": p.get("number"), "merged_at": at})
    merged.sort(key=lambda x: x["merged_at"], reverse=True)
    return {"name": r.get("name") or name, "description": _clean(r.get("description"), hide),
            "blurb": _clean(blurb, hide), "url": r.get("html_url"), "homepage": r.get("homepage") or None,
            "release": release, "pushed_at": r.get("pushed_at"), "stars": int(r.get("stargazers_count") or 0),
            "merged": merged[:MAX_PRS]}


def build(fetch: Fetch | None = None, config: dict[str, Any] | None = None, now: datetime | None = None,
          hide: list[re.Pattern] | None = None) -> dict[str, Any]:
    fetch = fetch or _get
    cfg = config if config is not None else json.loads(CONFIG.read_text())
    now = now or datetime.now(timezone.utc)
    hide = _names_to_hide() if hide is None else hide
    owner = str(cfg.get("owner") or "")
    sources: list[str] = []
    repos = []
    for entry in cfg.get("repos") or []:
        name = str(entry.get("name") if isinstance(entry, dict) else entry)
        got = repo(owner, name, (entry.get("blurb") if isinstance(entry, dict) else "") or "", fetch, hide, now, sources)
        if got:
            repos.append(got)
    return {"generated_at": now.isoformat(timespec="seconds"),
            "building": [t for t in (_clean(x, hide) for x in cfg.get("building") or []) if t],
            "titan": _clean(cfg.get("titan"), hide),
            "repos": repos, "sources": sources}


def get(now: float | None = None, fetch: Fetch | None = None, config: dict[str, Any] | None = None,
        hide: list[re.Pattern] | None = None) -> dict[str, Any]:
    """The feed, from the cache when it's younger than CACHE_TTL_S (memory, then the file), else fetched again."""
    now = time.time() if now is None else now
    with _lock:
        hit = _mem.get("feed")
        if hit is None:
            try:
                hit = json.loads(CACHE.read_text())
            except (OSError, ValueError):
                hit = None
        if hit and now - float(hit.get("_at", 0)) < CACHE_TTL_S:
            _mem["feed"] = hit
            return {k: v for k, v in hit.items() if k != "_at"}
        feed = build(fetch, config, datetime.fromtimestamp(now, timezone.utc), hide)
        _mem["feed"] = {**feed, "_at": now}
        try:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(_mem["feed"]))
        except OSError:
            pass
        return feed


if __name__ == "__main__":
    print(json.dumps(get(), indent=1))
