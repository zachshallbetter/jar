#!/usr/bin/env python3
"""A person's YouTube liked videos (and playlists they name), synced read-only into their own items (titan-node#470).

YouTube Data API v3 with the youtube.readonly scope, in the Google project already registered for titan. The API has
returned Watch Later (WL) empty since 2016, so it's skipped; a person who wants it copies it into an ordinary playlist
and names that playlist here (#470's option a). Liked videos are the LL playlist.

Where they go: the connectors runner, source "youtube", one record node per video (find_records type=youtube_video),
owner the person, audience private, source_id the video id. Kept per video: title, channel, the link, when it was
added to the list and which list. Nothing else (no description, comments or watch history): a title and a channel
are what the person chose; a description is someone else's text and isn't needed to find a video again.
Pruning: a video no longer in any of the person's lists, or added more than YOUTUBE_KEEP_DAYS ago (365), goes through
the runner's deletion path. Only a complete listing changes anything: a failed page leaves everything as it was.

The account file (0600, /ai/secrets/youtube/<name>.json, the person's own; off until it exists and they've signed in):
  {"owner": "<person id>", "client_id": "...", "client_secret": "...", "refresh_token": "...",
   "playlists": ["LL", "<a playlist id>"]}            (default ["LL"])
The refresh token comes from a one-time sign-in the person does themselves:
  ssh -L 8766:127.0.0.1:8766 titan                       (on the Mac, so the browser's redirect reaches titan)
  python3 scripts/youtube_sync.py auth --account youtube  (on titan: prints the consent link, writes the token)
  python3 scripts/youtube_sync.py sync [--dry-run]         (every account; counts only)
The token goes into the account file and is never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

SOURCE = "youtube"
NODE_KIND = "youtube_video"
API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
REDIRECT_PORT = 8766
ACCOUNTS_DIR = Path(os.environ.get("YOUTUBE_ACCOUNTS_DIR", "/ai/secrets/youtube"))
KEEP_DAYS = int(os.environ.get("YOUTUBE_KEEP_DAYS", "365"))
PER_PAGE = 50
MAX_PAGES = 100                    # 5,000 videos a list; stop there and change nothing
BATCH = 500                        # connectors_runner.MAX_ITEMS
TITLE_MAX = 200
_VIDEO_RX = re.compile(r"^[A-Za-z0-9_-]{11}$")
_LIST_RX = re.compile(r"^(LL|[A-Za-z0-9_-]{10,64})$")

Http = Callable[[str, str, "bytes | None", "dict[str, str]"], dict]


class YouTubeError(OSError):
    """Google refused or couldn't be reached. The message never carries a token."""


class AccountError(ValueError):
    pass


class Incomplete(Exception):
    """A listing stopped before its end: nothing is changed."""


def urllib_http(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> dict:
    import egress
    egress.note("youtube")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise YouTubeError(f"YouTube answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise YouTubeError(f"YouTube couldn't be reached ({type(exc).__name__})") from None


def _read_only(http: Http) -> Http:
    """Only GETs to the YouTube API, and the token refresh."""
    def call(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> dict:
        if not ((method == "GET" and url.startswith(API + "/")) or (method == "POST" and url == TOKEN_URL)):
            raise PermissionError(f"{method} isn't allowed: the YouTube sync is read-only")
        return http(method, url, data, headers)
    return call


# ── accounts ──

def _role_of(person: str) -> str | None:
    import household_identity as hh
    for p in hh.get_store().people():
        if str(p.get("id")) == person:
            return str(p.get("role") or "")
    return None


def load_account(path: Path, role_of: Callable[[str], str | None] = _role_of) -> dict[str, Any]:
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise AccountError(f"{path}: must be a regular 0600 file owned by this user")
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AccountError(f"{path}: not JSON") from None
    owner = str(raw.get("owner") or "")
    if not owner or not raw.get("client_id") or not raw.get("client_secret"):
        raise AccountError(f"{path}: needs owner, client_id and client_secret")
    role = role_of(owner)
    if role is None:
        raise AccountError(f"{path}: owner isn't a household member")
    if role == "kid":
        raise AccountError(f"{path}: no kids' accounts")
    lists = [str(x) for x in (raw.get("playlists") or ["LL"])]
    bad = [x for x in lists if not _LIST_RX.match(x)]
    if bad or "WL" in lists:
        raise AccountError(f"{path}: playlists must be LL or playlist ids (Watch Later isn't in the API: copy it into a "
                           "playlist and name that)")
    return {"name": path.stem, "owner": owner, "client_id": str(raw["client_id"]),
            "client_secret": str(raw["client_secret"]), "refresh_token": str(raw.get("refresh_token") or ""),
            "playlists": lists}


def accounts(directory: Path | None = None, role_of: Callable[[str], str | None] = _role_of) -> list[dict[str, Any]]:
    d = directory or ACCOUNTS_DIR
    return [load_account(p, role_of) for p in sorted(d.glob("*.json"))] if d.is_dir() else []


# ── the API ──

class YouTube:
    def __init__(self, acct: dict[str, Any], http: Http):
        self._acct = acct
        self._http = _read_only(http)
        self._token: str | None = None

    def login(self) -> None:
        a = self._acct
        if not a.get("refresh_token"):
            raise YouTubeError(f"{a['name']}: not signed in yet (youtube_sync.py auth)")
        got = self._http("POST", TOKEN_URL, urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": a["refresh_token"], "client_id": a["client_id"],
            "client_secret": a["client_secret"]}).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
        self._token = str(got.get("access_token") or "") or None
        if not self._token:
            raise YouTubeError("YouTube sign-in failed (sign in again with youtube_sync.py auth)")

    def playlist(self, list_id: str) -> list[dict[str, Any]]:
        """Every video of one list, each as kept here. Raises Incomplete when paging fails or runs out."""
        out: list[dict[str, Any]] = []
        page = None
        for _ in range(MAX_PAGES):
            params = [("part", "snippet,contentDetails"), ("playlistId", list_id), ("maxResults", str(PER_PAGE))]
            if page:
                params.append(("pageToken", page))
            try:
                got = self._http("GET", f"{API}/playlistItems?" + urllib.parse.urlencode(params), None,
                                 {"Authorization": f"Bearer {self._token}"})
            except YouTubeError as exc:
                raise Incomplete(str(exc)) from None
            for it in got.get("items") or []:
                k = keep(it, list_id)
                if k:
                    out.append(k)
            page = got.get("nextPageToken")
            if not page:
                return out
        raise Incomplete(f"more than {MAX_PAGES * PER_PAGE} videos in {list_id}")


def _ts(stamp: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def keep(item: dict[str, Any], list_id: str) -> dict[str, Any] | None:
    """One playlist item as titan keeps it, or None (a deleted or private video, no id)."""
    sn = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
    cd = item.get("contentDetails") if isinstance(item.get("contentDetails"), dict) else {}
    vid = str(cd.get("videoId") or (sn.get("resourceId") or {}).get("videoId") or "")
    title = " ".join(str(sn.get("title") or "").split())[:TITLE_MAX]
    added = _ts(sn.get("publishedAt"))
    if not _VIDEO_RX.match(vid) or not title or title in ("Deleted video", "Private video") or added is None:
        return None
    return {"video_id": vid, "title": title,
            "channel": " ".join(str(sn.get("videoOwnerChannelTitle") or "").split())[:120],
            "url": f"https://youtu.be/{vid}", "added_at": added.isoformat(), "list": list_id}


def item(v: dict[str, Any]) -> dict[str, Any]:
    fields = {"title": v["title"], "channel": v["channel"], "url": v["url"], "added_at": v["added_at"],
              "lists": v["lists"]}
    return {"source_id": v["video_id"], "observed_at": v["added_at"], "kind": NODE_KIND, "title": v["title"],
            "account": "youtube", "fields": fields}


def _ledger_keys(owner: str) -> set[str]:
    import connectors_runner as cx
    con = cx._db()
    try:
        return {r[0] for r in con.execute("SELECT upstream_id FROM items WHERE source=? AND owner=? AND deleted_at IS NULL",
                                          (SOURCE, owner))}
    finally:
        con.close()


def sync_account(acct: dict[str, Any], http: Http, *, dry_run: bool = False, runtime: Any = None,
                 now: datetime | None = None, keys: Callable[[str], set[str]] = _ledger_keys) -> dict[str, Any]:
    """One person's lists → their items. Counts only."""
    import connectors_runner as cx
    yt = YouTube(acct, http)
    yt.login()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=KEEP_DAYS)
    vids: dict[str, dict[str, Any]] = {}
    old = 0
    for lid in acct["playlists"]:
        for v in yt.playlist(lid):
            if _ts(v["added_at"]) < cutoff:
                old += 1
                continue
            have = vids.get(v["video_id"])
            if have:
                have["lists"] = sorted(set(have["lists"]) | {lid})
                have["added_at"] = min(have["added_at"], v["added_at"])
            else:
                vids[v["video_id"]] = {**v, "lists": [lid]}
    had = keys(acct["owner"])
    gone = sorted(had - set(vids))
    out = {"videos": len(vids), "new": len(set(vids) - had), "removed": len(gone), "older_than_keep": old,
           "dry_run": dry_run}
    if dry_run:
        return out
    if SOURCE in cx.sources_off(acct["owner"]):
        return {**out, "off": True, "new": 0, "removed": 0}
    items = [item(v) for v in vids.values()] + [{"source_id": k, "deleted": True} for k in gone]
    totals = {"written": 0, "unchanged": 0, "removed": 0, "forgotten": 0, "errors": 0}
    for i in range(0, len(items), BATCH):
        got = cx.ingest(SOURCE, items[i:i + BATCH], runtime=runtime, owner=acct["owner"])
        for k in ("written", "unchanged", "removed", "forgotten"):
            totals[k] += got.get(k, 0)
        totals["errors"] += len(got.get("errors") or [])
    return {**out, "ledger": totals}


def sync(*, directory: Path | None = None, connect: Callable[[dict[str, Any]], Http] = lambda a: urllib_http,
         dry_run: bool = False, runtime: Any = None, role_of: Callable[[str], str | None] = _role_of,
         now: datetime | None = None, keys: Callable[[str], set[str]] = _ledger_keys) -> dict[str, Any]:
    """Every account file; each account's counts or its error (a bad one never stops the others)."""
    out: dict[str, Any] = {}
    d = directory or ACCOUNTS_DIR
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            acct = load_account(p, role_of)
            out[acct["name"]] = sync_account(acct, connect(acct), dry_run=dry_run, runtime=runtime, now=now, keys=keys)
        except (AccountError, YouTubeError, Incomplete, PermissionError) as exc:
            out[p.stem] = {"error": str(exc)[:200]}
    return out


# ── the one-time sign-in (the person runs it; the token goes into their account file, never on screen) ──

def _write_token(path: Path, token: str) -> None:
    raw = json.loads(path.read_text())
    raw["refresh_token"] = token
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(raw, fh)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def auth(path: Path, *, http: Http = urllib_http, port: int = REDIRECT_PORT, wait: float = 300.0,
         show: Callable[[str], None] = print) -> bool:
    """Show the consent link, catch the redirect on 127.0.0.1:port, trade the code for a refresh token, keep it."""
    st = path.stat()
    if st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise SystemExit(f"{path}: must be a 0600 file owned by this user")
    raw = json.loads(path.read_text())
    if not raw.get("client_id") or not raw.get("client_secret"):
        raise SystemExit(f"{path}: needs client_id and client_secret")
    state, redirect = secrets.token_urlsafe(16), f"http://127.0.0.1:{port}/"
    got: dict[str, str] = {}

    class Catch(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if q.get("state") == state:
                got.update(q)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab.")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", port), Catch)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        show(AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": raw["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "state": state}))
        end = time.time() + wait
        while not got and time.time() < end:
            time.sleep(0.2)
    finally:
        srv.shutdown()
    if not got.get("code"):
        return False
    tok = http("POST", TOKEN_URL, urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": redirect,
        "client_id": raw["client_id"], "client_secret": raw["client_secret"]}).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"})
    if not tok.get("refresh_token"):
        return False
    _write_token(path, str(tok["refresh_token"]))
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="YouTube liked videos into titan, read-only (#470)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth")
    a.add_argument("--account", required=True, help="the account file's name in the accounts dir (without .json)")
    s = sub.add_parser("sync")
    s.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "auth":
        print("Open this link in the browser on the Mac with the ssh -L tunnel up:")
        ok = auth(ACCOUNTS_DIR / f"{args.account}.json")
        print("Signed in; the token is in the account file." if ok else "No sign-in arrived (or Google gave no token).")
        return 0 if ok else 1
    print(json.dumps(sync(dry_run=args.dry_run), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
