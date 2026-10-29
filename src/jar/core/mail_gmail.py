#!/usr/bin/env python3
"""Gmail through the Gmail API, for the mail server connector (titan-node#955 step 2): read-only.

mail_server.py talks to every mailbox through one small surface: examine(folder) → validity, uids(*IMAP SEARCH keys),
fetch(uids, "BODY.PEEK[...]") → raw bytes per id, logout(). Gmail gives the same here:
  - a folder is a label ("INBOX"), or "ALL" for all mail (Gmail leaves out spam and trash);
  - a Gmail message id (hex) is the uid; Gmail ids don't renumber, so validity is always 1;
  - IMAP SEARCH keys become Gmail's q: TEXT w → w, FROM x → from:x, SINCE d → after:YYYY/MM/DD;
  - headers come from format=metadata, a whole mail from format=raw (cut to a partial fetch's length).

Read-only: the OAuth scope is gmail.readonly, and the transport here lets through only GETs to the Gmail API and the
token refresh. Archive/move (label changes, gmail.modify) is step 5.

The account file (0600, /ai/secrets/mail/<name>.json, the person's own): {"owner": "<person id>", "provider": "gmail",
"client_id": "...", "client_secret": "...", "refresh_token": "...", "folders": ["INBOX"]}. The refresh token comes
from a one-time sign-in the person does themselves:

  ssh -L 8765:127.0.0.1:8765 titan                   (on the Mac, so the browser's redirect reaches titan)
  python3 scripts/mail_gmail.py auth --account gmail  (on titan: prints the consent link, writes the token)

The token goes into the account file and is never printed.
"""
from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
REDIRECT_PORT = 8765
PAGE = 500
HEADERS = ("Message-ID", "In-Reply-To", "References", "From", "Subject", "Date")
_MAX_ID = (1 << 63) - 1   # SQLite's INTEGER; Gmail's ids are well under it

Http = Callable[[str, str, "bytes | None", "dict[str, str]"], dict]


class GmailError(OSError):
    """Gmail refused or couldn't be reached. The message never carries a token."""


def urllib_http(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise GmailError(f"Gmail answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GmailError(f"Gmail couldn't be reached ({type(exc).__name__})") from None


def _read_only(http: Http) -> Http:
    """Only GETs to the Gmail API, and the token refresh."""
    def call(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> dict:
        if not ((method == "GET" and url.startswith(API + "/")) or (method == "POST" and url == TOKEN_URL)):
            import mail_server
            raise mail_server.ReadOnlyViolation(f"{method} isn't allowed: the Gmail connector is read-only")
        return http(method, url, data, headers)
    return call


def query(keys: Iterable[str]) -> str:
    """IMAP SEARCH keys (mail_server.criteria) → Gmail's q."""
    ks, out = list(keys), []
    i = 0
    while i < len(ks):
        k = ks[i].upper()
        if k == "ALL":
            i += 1
            continue
        v = ks[i + 1].strip('"').replace('\\"', '"')
        if k == "TEXT":
            out.append(f'"{v}"' if " " in v else v)
        elif k == "FROM":
            out.append(f"from:{v}")
        elif k == "SINCE":
            d = time.strptime(v, "%d-%b-%Y")
            out.append(f"after:{d.tm_year}/{d.tm_mon:02d}/{d.tm_mday:02d}")
        i += 2
    return " ".join(out)


class Gmail:
    """One person's Gmail, behind the same read-only surface as mail_server._ReadOnly."""

    def __init__(self, acct: dict[str, Any], http: Http):
        self._acct = acct
        self._http = _read_only(http)
        self._token: str | None = None
        self._label: str | None = None

    def login(self) -> None:
        a = self._acct
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": a["refresh_token"],
                                       "client_id": a["client_id"], "client_secret": a["client_secret"]}).encode()
        got = self._http("POST", TOKEN_URL, body, {"Content-Type": "application/x-www-form-urlencoded"})
        self._token = str(got.get("access_token") or "") or None
        if not self._token:
            raise GmailError("Gmail sign-in failed (sign in again with mail_gmail.py auth)")

    def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict:
        url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        return self._http("GET", url, None, {"Authorization": f"Bearer {self._token}"})

    def examine(self, folder: str) -> int:
        self._label = None if folder.upper() == "ALL" else folder
        return 1

    def uids(self, *keys: str) -> list[int]:
        params = [("maxResults", str(PAGE))]
        if self._label:
            params.append(("labelIds", self._label))
        q = query(keys)
        if q:
            params.append(("q", q))
        out: list[int] = []
        page = None
        while True:
            got = self._get("messages", params + ([("pageToken", page)] if page else []))
            for m in got.get("messages") or []:
                n = int(str(m["id"]), 16)
                if n <= _MAX_ID:
                    out.append(n)
            page = got.get("nextPageToken")
            if not page:
                return sorted(out)

    def fetch(self, uids: Iterable[int], what: str) -> dict[int, bytes]:
        import mail_server
        spec = what.upper()
        if "PEEK" not in spec or "RFC822" in spec:
            raise mail_server.ReadOnlyViolation("only BODY.PEEK fetches")
        cut = int(spec.split("<0.")[1].split(">")[0]) if "<0." in spec else None
        out: dict[int, bytes] = {}
        for uid in uids:
            mid = format(uid, "x")
            if "HEADER.FIELDS" in spec:
                got = self._get(f"messages/{mid}", [("format", "metadata")] + [("metadataHeaders", h) for h in HEADERS])
                hs = (got.get("payload") or {}).get("headers") or []
                out[uid] = "".join(f"{h['name']}: {h['value']}\r\n" for h in hs).encode() + b"\r\n"
            else:
                got = self._get(f"messages/{mid}", [("format", "raw")])
                raw = base64.urlsafe_b64decode(str(got.get("raw") or "") + "==")
                out[uid] = raw[:cut] if cut else raw
        return out

    def logout(self) -> None:
        self._token = None

    def __getattr__(self, name: str) -> Any:
        import mail_server
        raise mail_server.ReadOnlyViolation(f"{name} isn't allowed: the Gmail connector is read-only (#955)")


# ── the one-time sign-in (the person runs it; the token goes into their account file, never on screen) ─────────────

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
    if raw.get("provider") != "gmail" or not raw.get("client_id") or not raw.get("client_secret"):
        raise SystemExit(f"{path}: needs provider gmail, client_id and client_secret")
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
    ap = argparse.ArgumentParser(description="Gmail sign-in for the mail server connector (#955)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth")
    a.add_argument("--account", required=True, help="the account file's name in /ai/secrets/mail (without .json)")
    args = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mail_server
    path = mail_server.ACCOUNTS_DIR / f"{args.account}.json"
    print("Open this link in the browser on the Mac with the ssh -L tunnel up:")
    ok = auth(path)
    print("Signed in; the token is in the account file." if ok else "No sign-in arrived (or Google gave no token).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
