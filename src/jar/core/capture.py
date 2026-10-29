#!/usr/bin/env python3
"""Capture over crawl (#858): a page the person saved from their own browser, as they saw it.

Reddit and LinkedIn block titan outright (#826), and crawling them breaks their terms; the person's own logged-in
browser already shows the page. The Mac (Safari/Chrome, jellybean#691) and the iPhone share sheet (jellybean#692)
send the page's readable text here, and titan keeps it in the knowledge base's web zone (#833): the person's own,
private, under the same id ingest_url would give the same address, so a page saved twice, or fetched once and
captured later, is one item that gets updated, never a duplicate.

  POST   /v1/capture      {url, text, title?, app?, captured_at?} → 201 {id, title, status: saved|updated|unchanged}
  DELETE /v1/capture/{id} the person's own capture only (Undo) → {id, removed: true}

The text is someone else's page: KB hits from the web zone are wrapped as untrusted when a turn reads them
(untrusted.kb_source), like any fetched page.

A link alone (#858 follow-up: a share from an app that hands over only the address, so under 20 characters of text):
titan fetches the page itself (the fetch runtime's /v1/parse, judged by fetch_checks like fetch_url) and saves it with
via "capture-fetch". A site that walls titan out (Reddit, LinkedIn, X, Facebook, Instagram) is never fetched, and one
that sends a block page, a sign-in wall or no readable text is not saved: 422 {error: "blocked", message, site}, so the
app can say to open it in Safari and share from there (the person's own view, which those sites show). A fetch that
fails otherwise (missing, timed out, rate-limited) is 422 {error: "unreachable", message, site}.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

DB_PATH = Path(os.environ.get("CAPTURE_DB", "/ai/datasets/connectors/capture.db"))
MAX_TEXT = 200_000
APPS = {"safari", "chrome", "share", "iphone", "mac", "other"}
_TRACKING = re.compile(r"^(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|ref_src|igshid|si)$", re.I)


class CaptureError(ValueError):
    pass


class CaptureNotFetched(CaptureError):
    """A link-only capture titan couldn't read: `kind` is "blocked" (open it in the browser and share from there)
    or "unreachable" (try again later); `message` is for the person."""

    def __init__(self, kind: str, message: str, site: str):
        super().__init__(message)
        self.kind, self.message, self.site = kind, message, site

    def body(self) -> dict[str, str]:
        return {"error": self.kind, "message": self.message, "site": self.site}


BLOCKED_MESSAGE = "Open it in Safari and share from there"
# Sites that turn titan's reader away or show it only a sign-in wall (#826): never fetched, the person is told how
# to save it from their own view instead. Crawling them would also break their terms.
WALLED = ("reddit.com", "redd.it", "linkedin.com", "lnkd.in", "x.com", "twitter.com", "facebook.com", "fb.com",
          "instagram.com", "threads.net")


def _walled(site: str) -> bool:
    return any(site == w or site.endswith("." + w) for w in WALLED)


def _parse(url: str) -> dict[str, Any]:
    import agent_runtime_lib as lib
    return lib._runtime_request("fetch", "/v1/parse", "POST", {"url": url}, timeout=60)


def fetch_page(url: str, fetch: Callable[[str], dict[str, Any]] | None = None) -> tuple[str, str]:
    """(title, text) titan read at the address, or CaptureNotFetched saying why not."""
    import fetch_checks
    site = fetch_checks.site(url)
    if _walled(site):
        raise CaptureNotFetched("blocked", BLOCKED_MESSAGE, site)
    try:
        r = (fetch or _parse)(url) or {}
    except Exception as exc:  # noqa: BLE001 - said to the person, never raised as a 500
        said = fetch_checks.error_text(url, exc)
        refused = bool(re.search(r"\b(?:401|403)\b", str(exc)))
        raise CaptureNotFetched("blocked" if refused else "unreachable", BLOCKED_MESSAGE if refused else said, site) from exc
    title = " ".join(str(r.get("title") or "").split())
    text = str(r.get("content") or "").strip()
    if fetch_checks.verdict(url, title, text) or len(text) < 20:
        raise CaptureNotFetched("blocked", BLOCKED_MESSAGE, site)
    return title, text


def _db(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE IF NOT EXISTS captures (id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL, "
                "digest TEXT NOT NULL, app TEXT, saved_at REAL, updated_at REAL)")
    return con


def clean_url(url: str) -> str:
    """http(s) only; no fragment; tracking parameters dropped (the same page gets the same id)."""
    u = urllib.parse.urlsplit(str(url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise CaptureError("url must be an http(s) address")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if not _TRACKING.match(k)]
    return urllib.parse.urlunsplit((u.scheme, u.netloc.lower(), u.path or "/", urllib.parse.urlencode(q), ""))


def item_id(url: str, person: str | None) -> str:
    import kb_privacy
    return kb_privacy.scoped_id("web-", hashlib.sha1(url.encode()).hexdigest()[:12], person)


def save(person: str, body: dict[str, Any], index: Callable[..., Any] | None = None,
         db: Path | None = None, fetch: Callable[[str], dict[str, Any]] | None = None) -> dict[str, Any]:
    url = clean_url(body.get("url"))
    text = re.sub(r"\n{3,}", "\n\n", str(body.get("text") or "")).strip()[:MAX_TEXT]
    title = " ".join(str(body.get("title") or "").split())
    via = "capture"
    if len(text) < 20:   # a link alone: titan reads the page itself, or says why it can't
        fetched_title, fetched = fetch_page(url, fetch)
        text = re.sub(r"\n{3,}", "\n\n", fetched).strip()[:MAX_TEXT]
        title = title or fetched_title
        via = "capture-fetch"
    title = title[:300] or url
    import trim
    # menus, banners, share rows and footers stay out of the KB (#868)
    text, _st = trim.trim(text, title)
    app = str(body.get("app") or "other").lower()
    app = app if app in APPS else "other"
    sid = item_id(url, person)
    digest = hashlib.sha256(f"{title}\n{text}".encode()).hexdigest()
    now = time.time()
    con = _db(db)
    try:
        row = con.execute("SELECT digest FROM captures WHERE id=?", (sid,)).fetchone()
        if row and row[0] == digest:
            return {"id": sid, "title": title, "status": "unchanged", "fetched": via == "capture-fetch"}
        if index is None:
            import agent_runtime_lib as lib
            index = lib._kb_index
        index(sid, title, f"{title}\n{url}\n\n{text}", "web",
              {"url": url, "via": via, "captured_from": app,
               "captured_at": str(body.get("captured_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))},
              owner=person, audience="private")
        con.execute("INSERT INTO captures VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET digest=excluded.digest, "
                    "app=excluded.app, updated_at=excluded.updated_at", (sid, person, url, digest, app, now, now))
        con.commit()
        return {"id": sid, "title": title, "status": "updated" if row else "saved", "fetched": via == "capture-fetch"}
    finally:
        con.close()


def remove(person: str, sid: str, delete: Callable[[str], Any] | None = None, db: Path | None = None) -> bool:
    """Undo: only the person's own capture. False when it isn't theirs or isn't a capture."""
    con = _db(db)
    try:
        row = con.execute("SELECT owner FROM captures WHERE id=?", (sid,)).fetchone()
        if not row or row[0] != person:
            return False
        if delete is None:
            import urllib.parse as _u
            import agent_runtime_lib as lib
            import kb_privacy
            def delete(i: str) -> None:  # noqa: E306
                lib._runtime_request("retrieval", "/v1/retrieval/sources/" + _u.quote(i, safe=""), "DELETE")
                kb_privacy.drop_acl(i)
        delete(sid)
        con.execute("DELETE FROM captures WHERE id=?", (sid,))
        con.commit()
        return True
    finally:
        con.close()
