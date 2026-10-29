#!/usr/bin/env python3
"""What fetch_url says when a site won't let titan read it (#826, 10-04). Reddit answers titan's headless browser with
a "You've been blocked by network security" page and HTTP success, and fetch_url handed that page to the model as the
content. Facts in code: a block page, a robot check or an empty JavaScript shell is said plainly, with the site's
name, not passed on as the page.

Reddit turns away every unauthenticated reader titan has (the browser, old.reddit, the .json pages, with any User-Agent:
403 or a sign-in wall, checked 10-04 from titan and from the Mac). Its own oEmbed service, the public endpoint sites
use to embed a post, still answers for a post link, so a Reddit post gets its title, author and subreddit, marked as
only that. Reading a thread itself needs Reddit's API with an app Zach registers (OAuth); nothing here works around a
site's block.

#934 (10-04): fetch and search outcomes, by class and site, for the turn log and the scorecard (their messages are
plain words, so the generic tool-failure heads missed them); the links the person gave, so a given link is read
before it is searched for; and GitHub links read through GitHub's public API (a repo's description and README, a
file, an issue or pull request), since the model searched for 10 of 13 repo links and search misses small repos.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

USER_AGENT = "titan-household-agent/1.0 (a family's personal assistant)"
# Short pages only: a long article that mentions "captcha" is an article.
_SHORT = 2500
_BLOCK_RX = re.compile(
    r"(?i)you've been blocked by network security|\bblocked by (?:network security|our security|the site)\b|"
    r"^\s*(?:blocked|access denied|forbidden|403 forbidden)\s*$|attention required!? \|? ?cloudflare|"
    r"\bjust a moment\.\.\.|verify (?:that )?you are (?:a )?human|are you a robot|\bunusual traffic\b|"
    r"enable javascript and cookies to continue|\brequest (?:was )?blocked\b|\bcaptcha\b|\bbot detection\b|"
    r"\bpress (?:and|&) hold\b", re.M)
_WALL_RX = re.compile(r"(?i)^\s*(?:welcome to reddit|log in to (?:continue|reddit)|sign in to (?:continue|see)|"
                      r"you must (?:log|sign) in)\b")
_REDDIT_POST_RX = re.compile(r"(?i)^https?://(?:www\.|old\.|new\.|np\.)?reddit\.com/r/[^/]+/comments/[a-z0-9]+")


def site(url: str) -> str:
    host = urllib.parse.urlsplit(url if "://" in url else "https://" + url).hostname or url
    return re.sub(r"^(?:www|old|new|m|np)\.", "", host.lower())


def verdict(url: str, title: str, content: str) -> str | None:
    """None when the page is a page; else what to tell the person instead of its text."""
    text = f"{title}\n{content}".strip()
    name = site(url)
    if len(text) < _SHORT and (_BLOCK_RX.search(text) or (name.endswith("reddit.com") and _WALL_RX.search(text))):
        return (f"{name} blocked titan's request (it turns away automated readers), so I couldn't read that page. "
                f"I haven't tried to get around it.")
    if not content.strip():   # a short page is still a page; nothing at all is a shell
        return (f"{name} sent back no readable text: the page needs JavaScript or a sign-in that titan's reader "
                f"can't do.")
    return None


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def reddit_embed(url: str, get: Callable[[str], Any] | None = None) -> str | None:
    """A Reddit post's title, author and subreddit from Reddit's oEmbed service, or None (not a post, or no answer)."""
    m = _REDDIT_POST_RX.match(url.strip())
    if not m:
        return None
    try:
        d = (get or _get_json)("https://www.reddit.com/oembed?" + urllib.parse.urlencode({"url": m.group(0) + "/"}))
    except Exception:  # noqa: BLE001 - no embed: the block message stands alone
        return None
    title = " ".join(str((d or {}).get("title") or "").split())
    if not title:
        return None
    sub = re.search(r"reddit\.com/r/([^/\"]+)/", str(d.get("html") or ""))
    author = str(d.get("author_name") or "").strip()
    return "\n".join(x for x in (
        f"Reddit post: {title}",
        f"in r/{sub.group(1)}" + (f", by u/{author}" if author else "") if sub else (f"by u/{author}" if author else ""),
        "(Only the title Reddit's embed service shares: Reddit blocks titan from reading the thread.)") if x)


def error_text(url: str, exc: Exception) -> str:
    """A failed fetch in plain words: refused, rate-limited, missing or unreachable, with the site's name."""
    name, msg = site(url), str(exc)
    if re.search(r"\b(?:401|403)\b", msg):
        return f"{name} refused titan's request (HTTP 403), so I couldn't read that page."
    if re.search(r"\b429\b", msg):
        return f"{name} is rate-limiting titan right now (HTTP 429); try again in a while."
    if re.search(r"\b(?:404|410)\b", msg):
        return f"{name} says that page doesn't exist (HTTP 404)."
    if re.search(r"(?i)timed? ?out", msg):
        return f"{name} didn't answer in time, so I couldn't read that page."
    return f"Couldn't fetch {url}: {msg[:200]}"


# ── #934: what a fetch or a search came to ──
_FETCH_CLASSES = (("broker_down", re.compile(r"^Couldn't fetch that right now: titan's fetch service is unreachable")),
                  ("not_public", re.compile(r"^Error: not fetched: that address is inside the house network")),
                  ("blocked", re.compile(r"^\S+ blocked titan's request")),
                  ("empty", re.compile(r"^\S+ sent back no readable text")),
                  ("refused", re.compile(r"^\S+ refused titan's request")),
                  ("rate_limited", re.compile(r"^\S+ is rate-limiting titan")),
                  ("missing", re.compile(r"^\S+ says that page doesn't exist|^GitHub says \S+ doesn't exist")),
                  ("timeout", re.compile(r"^\S+ didn't answer in time")),
                  ("error", re.compile(r"^Couldn't fetch ")),
                  ("bad_args", re.compile(r"^Error: ")),
                  ("not_run", re.compile(r"^Not run\b")))
_SEARCH_CLASSES = (("error", re.compile(r"^(?:Web search error|Search error)\b")),
                   ("empty", re.compile(r"^(?:No results found\.|No answer from web search\.)\s*$")),
                   ("bad_args", re.compile(r"^Error: ")),
                   ("not_run", re.compile(r"^Not run\b")))


def outcome(tool: str, args: dict[str, Any], result: Any) -> tuple[str, str] | None:
    """(class, site) for a fetch_url or web_search result: "ok" or what went wrong. None for any other tool.
    A search's site is "search": the query is what the person asked about, so it isn't logged."""
    text = " ".join(str(result or "").split())
    if tool == "fetch_url":
        url = str((args or {}).get("url") or "").strip()
        name = site(url) if url else "-"
        return next((c for c, rx in _FETCH_CLASSES if rx.search(text)), "ok"), name[:60]
    if tool == "web_search":
        return next((c for c, rx in _SEARCH_CLASSES if rx.search(text)), "ok"), "search"
    return None


_LINK_RX = re.compile(r"(?i)\bhttps?://[^\s<>\"'`)\]]+|\b(?:www\.)[a-z0-9-]+(?:\.[a-z0-9-]+)+[^\s<>\"'`)\]]*|"
                      r"\bgithub\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+[^\s<>\"'`)\]]*")


def links(text: str | None) -> list[str]:
    """The web links in what the person wrote, in order, without trailing punctuation."""
    out: list[str] = []
    for m in _LINK_RX.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?")
        if u not in out:
            out.append(u)
    return out[:5]


# ── #934: GitHub links through GitHub's public API ──
# Unauthenticated on purpose: anyone in the house may paste a link, and the admin's token would open his private
# repos to them. Public repos only; 60 requests an hour per address, plenty for chat. A rate limit falls back to the
# browser fetch.
_GH_RX = re.compile(r"(?i)^(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?"
                    r"(?:/(blob|tree|issues|pull)/([^?#]+))?/?(?:[?#].*)?$")
_GH_NOT_REPOS = {"orgs", "settings", "marketplace", "topics", "features", "sponsors", "about", "login", "search",
                 "notifications", "explore", "collections", "trending", "apps", "enterprise", "pricing", "users"}
GH_API = "https://api.github.com"


class GitHubLimited(Exception):
    """Rate-limited or refused: read the page with the browser instead."""


def _gh_get(url: str, raw: bool = False) -> Any:
    accept = "application/vnd.github.raw" if raw else "application/vnd.github+json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(400_000)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError(url) from e
        if e.code in (403, 429):
            raise GitHubLimited(str(e.code)) from e
        raise
    return body.decode("utf-8", "replace") if raw else json.loads(body)


def github(url: str, get: Callable[[str, bool], Any] | None = None) -> str | None:
    """A GitHub repo, file, issue or pull request read through the API, as text; None when the link is some other
    GitHub page or the API won't answer (the caller then fetches the page as usual)."""
    m = _GH_RX.match(url.strip())
    if not m or m.group(1).lower() in _GH_NOT_REPOS:
        return None
    owner, repo, kind, rest = m.group(1), m.group(2), (m.group(3) or "").lower(), m.group(4) or ""
    full = f"{owner}/{repo}"
    get = get or _gh_get
    try:
        if kind in ("issues", "pull"):
            num = re.match(r"(\d+)", rest)
            if not num:
                return None
            d = get(f"{GH_API}/repos/{full}/issues/{num.group(1)}", False)
            what = "Pull request" if d.get("pull_request") or kind == "pull" else "Issue"
            head = (f"{what} {full}#{d.get('number')}: {' '.join(str(d.get('title') or '').split())}\n"
                    f"{d.get('state')}, opened {str(d.get('created_at') or '')[:10]} by {(d.get('user') or {}).get('login')}"
                    f", {d.get('comments', 0)} comments")
            return head + "\n\n" + str(d.get("body") or "(no description)").strip()
        if kind == "blob":
            ref, _, path = rest.partition("/")
            if not path:
                return None
            text = get(f"{GH_API}/repos/{full}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}", True)
            return f"File {path} in {full} ({ref})\n\n{text}"
        d = get(f"{GH_API}/repos/{full}", False)
        bits = [d.get("language"), f"{d.get('stargazers_count', 0)} stars",
                f"last push {str(d.get('pushed_at') or '')[:10]}" if d.get("pushed_at") else None,
                ((d.get("license") or {}).get("spdx_id") or None), "archived" if d.get("archived") else None,
                "a fork" if d.get("fork") else None]
        head = (f"GitHub repository {d.get('full_name') or full}: {' '.join(str(d.get('description') or '').split()) or '(no description)'}\n"
                + " · ".join(str(b) for b in bits if b))
        if d.get("homepage"):
            head += f"\nHomepage: {d['homepage']}"
        if d.get("topics"):
            head += "\nTopics: " + ", ".join(d["topics"][:10])
        try:
            readme = str(get(f"{GH_API}/repos/{full}/readme", True) or "").strip()
        except FileNotFoundError:
            readme = ""
        return head + ("\n\nREADME:\n" + readme if readme else "\n\n(No README.)")
    except FileNotFoundError:
        return f"GitHub says {full} doesn't exist, or it's private (HTTP 404)."
    except Exception:  # noqa: BLE001 - rate limit, network: the browser fetch tries instead
        return None


# ── where a fetch may go (#936 design): the public web only, never titan itself or the house's network ────────────────
import ipaddress as _ip
import socket as _socket
from urllib.parse import urlsplit as _urlsplit

_PRIVATE_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".home.arpa", ".localdomain")
NOT_PUBLIC = "Error: not fetched: that address is inside the house network (or titan itself); only the public web is read."


def _private_ip(ip: "_ip.IPv4Address | _ip.IPv6Address") -> bool:
    if isinstance(ip, _ip.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or (isinstance(ip, _ip.IPv4Address) and ip in _ip.ip_network("100.64.0.0/10")))


def public_url(url: str, *, resolve=None) -> str | None:
    """None when `url` is an http(s) address on the public internet; else the refusal to answer with. Checked before any
    request (the fetch-runtime has no guard of its own): another scheme, a bare or private-looking name (localhost,
    *.local, *.internal, …), an IP literal or a name that resolves to loopback, private (RFC 1918, ULA), link-local,
    CGNAT, multicast, reserved or unspecified. A name that doesn't resolve is left to the fetch (it fails there)."""
    try:
        u = _urlsplit(str(url or "").strip())
    except ValueError:
        return NOT_PUBLIC
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        return "Error: not fetched: only http and https web addresses can be read."
    host = u.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES) or "." not in host.strip("[]") and ":" not in host:
        return NOT_PUBLIC
    try:
        return NOT_PUBLIC if _private_ip(_ip.ip_address(host)) else None
    except ValueError:
        pass                                   # a name: what does it resolve to?
    try:
        infos = (resolve or _socket.getaddrinfo)(host, u.port or (443 if u.scheme == "https" else 80))
    except (OSError, UnicodeError):
        return None
    for info in infos:
        try:
            if _private_ip(_ip.ip_address(str(info[4][0]).split("%", 1)[0])):
                return NOT_PUBLIC
        except ValueError:
            continue
    return None

