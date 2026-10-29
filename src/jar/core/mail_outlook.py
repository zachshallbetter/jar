#!/usr/bin/env python3
"""Outlook (Microsoft 365 / Outlook.com) through Microsoft Graph, for the mail server connector (titan-node#955, "all
providers"): read-only, the same small surface as mail_gmail.py and mail_server._ReadOnly:
  examine(folder) → validity, uids(*IMAP SEARCH keys), fetch(uids, "BODY.PEEK[...]") → raw bytes per uid, logout().
  - a folder is a well-known name (INBOX, Archive, Sent) or a folder's display name; "ALL" is every message;
  - Graph ids are long opaque strings, so each gets a stable integer uid (a keyed hash) kept in a small map beside
    mail_server's store; immutable ids (Prefer: IdType="ImmutableId") don't change when a mail moves, so validity is 1;
  - IMAP SEARCH keys become Graph's: SINCE → $filter receivedDateTime ge; TEXT / FROM → $search (KQL);
  - headers come from internetMessageHeaders, a whole mail from /$value (MIME), cut to a partial fetch's length.
Read-only: the scope is Mail.Read (plus offline_access for the refresh token), and the transport lets through only
GETs to Graph's /me and the two sign-in posts (device code, token).

The account file (0600, /ai/secrets/mail/<name>.json, the person's own; off until it exists):
  {"owner": "<person id>", "provider": "outlook", "client_id": "<an app registration's id>", "tenant": "consumers" |
   "common" | "<tenant id>", "refresh_token": "...", "folders": ["INBOX"] | "auto"}
The refresh token comes from a one-time device-code sign-in the person does themselves, on any device:
  python3 scripts/mail_outlook.py auth --account outlook     (on titan: shows a short code and a Microsoft link)
They open the link, enter the code and sign in; the token goes into the account file and is never printed. Microsoft
may hand back a new refresh token at each sign-in; it replaces the old one in the file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

GRAPH = "https://graph.microsoft.com/v1.0/me"
LOGIN = "https://login.microsoftonline.com"
SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
PAGE = 500
HEADERS = ("Message-ID", "In-Reply-To", "References", "From", "Subject", "Date")
WELL_KNOWN = {"INBOX": "inbox", "ARCHIVE": "archive", "SENT": "sentitems", "SENT ITEMS": "sentitems",
              "SENT MESSAGES": "sentitems"}
_TENANT_RX = re.compile(r"^(common|consumers|organizations|[0-9a-f-]{36}|[a-z0-9.-]+\.[a-z]{2,})$", re.I)

Http = Callable[[str, str, "bytes | None", "dict[str, str]"], dict]


class OutlookError(OSError):
    """Graph or the sign-in refused or couldn't be reached. The message never carries a token."""


def token_url(tenant: str) -> str:
    return f"{LOGIN}/{tenant}/oauth2/v2.0/token"


def device_url(tenant: str) -> str:
    return f"{LOGIN}/{tenant}/oauth2/v2.0/devicecode"


def urllib_http(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> dict:
    """JSON answers as dicts; anything else (a /$value MIME body) as {"_raw": bytes}."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read() or b""
            if "json" in (r.headers.get("Content-Type") or ""):
                return json.loads(body or b"{}")
            return {"_raw": body}
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read() or b"{}").get("error")
        except ValueError:
            err = None
        if isinstance(err, str):   # the sign-in's own answers while it waits (authorization_pending, slow_down...)
            return {"error": err}
        raise OutlookError(f"Microsoft answered HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OutlookError(f"Microsoft couldn't be reached ({type(exc).__name__})") from None


def _read_only(http: Http, tenant: str) -> Http:
    """Only GETs to Graph's /me, and the sign-in's two posts."""
    allowed_posts = {token_url(tenant), device_url(tenant)}

    def call(method: str, url: str, data: bytes | None, headers: dict[str, str]) -> dict:
        if not ((method == "GET" and url.startswith(GRAPH + "/")) or (method == "POST" and url in allowed_posts)):
            import mail_server
            raise mail_server.ReadOnlyViolation(f"{method} isn't allowed: the Outlook connector is read-only")
        return http(method, url, data, headers)
    return call


def _ids_db(path: Path | None = None) -> sqlite3.Connection:
    """The uid ↔ Graph id map, beside mail_server's store (so wherever its DB is, this is too)."""
    if path is None:
        import mail_server
        path = mail_server.DB.parent / "mail_outlook_ids.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS ids (account TEXT NOT NULL, uid INTEGER NOT NULL, graph_id TEXT NOT NULL, "
                "PRIMARY KEY (account, uid))")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return con


def uid_of(account: str, graph_id: str) -> int:
    """A stable 60-bit integer for a Graph id (fits SQLite's INTEGER and mail_server's uid column)."""
    return int(hashlib.sha256(f"{account}\0{graph_id}".encode()).hexdigest()[:15], 16)


def search(keys: Iterable[str]) -> tuple[str | None, str | None]:
    """IMAP SEARCH keys (mail_server.criteria) → ($search KQL, $filter). Graph won't take both on messages, so a
    search that has words carries the date in KQL too (received>=)."""
    ks, words, since = list(keys), [], None
    i = 0
    while i < len(ks):
        k = ks[i].upper()
        if k == "ALL":
            i += 1
            continue
        v = ks[i + 1].strip('"').replace('\\"', '"')
        if k == "TEXT":
            words.append(v.replace('"', ""))
        elif k == "FROM":
            words.append(f"from:{v.replace(chr(34), '')}")
        elif k == "SINCE":
            d = time.strptime(v, "%d-%b-%Y")
            since = f"{d.tm_year}-{d.tm_mon:02d}-{d.tm_mday:02d}"
        i += 2
    if words:
        kql = " ".join(words) + (f" received>={since}" if since else "")
        return f'"{kql}"', None
    return None, (f"receivedDateTime ge {since}T00:00:00Z" if since else None)


class Outlook:
    """One person's Outlook mailbox, behind the same read-only surface as mail_server._ReadOnly."""

    def __init__(self, acct: dict[str, Any], http: Http, ids: Path | None = None,
                 keep_token: Callable[[str, str], None] | None = None):
        self._acct = acct
        self._tenant = str(acct.get("tenant") or "common")
        self._http = _read_only(http, self._tenant)
        self._token: str | None = None
        self._base: str | None = None
        self._ids = ids
        self._keep = keep_token if keep_token is not None else _keep_refresh

    def login(self) -> None:
        a = self._acct
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": a["refresh_token"],
                                       "client_id": a["client_id"], "scope": SCOPE}).encode()
        got = self._http("POST", token_url(self._tenant), body, {"Content-Type": "application/x-www-form-urlencoded"})
        self._token = str(got.get("access_token") or "") or None
        if not self._token:
            raise OutlookError("Outlook sign-in failed (sign in again with mail_outlook.py auth)")
        new = str(got.get("refresh_token") or "")
        if new and new != a["refresh_token"]:   # Microsoft rotates it; keep the newest in the file
            self._keep(a["name"], new)
            a["refresh_token"] = new

    def _get(self, url: str, params: list[tuple[str, str]] | None = None) -> dict:
        full = url if url.startswith("https://") else f"{GRAPH}/{url}"
        full += ("?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)) if params else ""
        return self._http("GET", full, None, {"Authorization": f"Bearer {self._token}",
                                              "Prefer": 'IdType="ImmutableId"', "ConsistencyLevel": "eventual"})

    def examine(self, folder: str) -> int:
        f = folder.strip()
        if f.upper() == "ALL":
            self._base = "messages"
        elif f.upper() in WELL_KNOWN:
            self._base = f"mailFolders/{WELL_KNOWN[f.upper()]}/messages"
        else:
            got = self._get("mailFolders", [("$filter", f"displayName eq '{f.replace(chr(39), chr(39) * 2)}'"),
                                            ("$select", "id")])
            hit = (got.get("value") or [None])[0]
            if not hit:
                raise OutlookError("that folder isn't in this mailbox")
            self._base = f"mailFolders/{hit['id']}/messages"
        return 1

    def uids(self, *keys: str) -> list[int]:
        q, flt = search(keys)
        params = [("$select", "id"), ("$top", str(PAGE))]
        if q:
            params.append(("$search", q))
        if flt:
            params.append(("$filter", flt))
        out: list[int] = []
        rows: list[tuple[str, int, str]] = []
        url: str | None = self._base or "messages"
        first = True
        while url:
            got = self._get(url, params if first else None)
            first = False
            for m in got.get("value") or []:
                gid = str(m.get("id") or "")
                if gid:
                    u = uid_of(self._acct["name"], gid)
                    out.append(u)
                    rows.append((self._acct["name"], u, gid))
            url = got.get("@odata.nextLink")
        con = _ids_db(self._ids)
        try:
            with con:
                con.executemany("INSERT OR REPLACE INTO ids VALUES (?,?,?)", rows)
        finally:
            con.close()
        return sorted(out)

    def _graph_id(self, uid: int) -> str | None:
        con = _ids_db(self._ids)
        try:
            r = con.execute("SELECT graph_id FROM ids WHERE account=? AND uid=?", (self._acct["name"], int(uid))).fetchone()
        finally:
            con.close()
        return r[0] if r else None

    def fetch(self, uids: Iterable[int], what: str) -> dict[int, bytes]:
        import mail_server
        spec = what.upper()
        if "PEEK" not in spec or "RFC822" in spec:
            raise mail_server.ReadOnlyViolation("only BODY.PEEK fetches")
        cut = int(spec.split("<0.")[1].split(">")[0]) if "<0." in spec else None
        out: dict[int, bytes] = {}
        for uid in uids:
            gid = self._graph_id(uid)
            if not gid:
                continue   # never listed here: as a mail gone from the server
            path = f"messages/{urllib.parse.quote(gid, safe='')}"
            if "HEADER" in spec:
                got = self._get(path, [("$select", "internetMessageHeaders")])
                hs = got.get("internetMessageHeaders") or []
                wanted = {h.lower() for h in HEADERS} if "HEADER.FIELDS" in spec else None
                out[uid] = "".join(f"{h['name']}: {h['value']}\r\n" for h in hs
                                   if wanted is None or str(h.get("name", "")).lower() in wanted).encode() + b"\r\n"
            else:
                raw = self._get(f"{path}/$value").get("_raw") or b""
                out[uid] = raw[:cut] if cut else raw
        return out

    def logout(self) -> None:
        self._token = None

    def __getattr__(self, name: str) -> Any:
        import mail_server
        raise mail_server.ReadOnlyViolation(f"{name} isn't allowed: the Outlook connector is read-only (#955)")


# ── the one-time sign-in (device code: the person signs in on any device; the token never shows) ───────────────────

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


def _keep_refresh(account: str, token: str) -> None:
    import mail_server
    _write_token(mail_server.ACCOUNTS_DIR / f"{account}.json", token)


def auth(path: Path, *, http: Http = urllib_http, show: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.time) -> bool:
    """Device-code sign-in: show Microsoft's link and a short code, wait while the person signs in, keep the token."""
    st = path.stat()
    if st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise SystemExit(f"{path}: must be a 0600 file owned by this user")
    raw = json.loads(path.read_text())
    tenant = str(raw.get("tenant") or "common")
    if raw.get("provider") != "outlook" or not raw.get("client_id") or not _TENANT_RX.match(tenant):
        raise SystemExit(f"{path}: needs provider outlook, a client_id and a valid tenant")
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    dc = http("POST", device_url(tenant), urllib.parse.urlencode({"client_id": raw["client_id"], "scope": SCOPE}).encode(),
              form)
    if not dc.get("device_code") or not dc.get("user_code"):
        return False
    show(f"On any device, open {dc.get('verification_uri') or 'https://microsoft.com/devicelogin'} "
         f"and enter the code {dc['user_code']}")
    interval = max(1.0, float(dc.get("interval") or 5))
    end = now() + float(dc.get("expires_in") or 900)
    while now() < end:
        sleep(interval)
        tok = http("POST", token_url(tenant), urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "client_id": raw["client_id"],
            "device_code": dc["device_code"]}).encode(), form)
        if tok.get("refresh_token"):
            _write_token(path, str(tok["refresh_token"]))
            return True
        err = tok.get("error")
        if err == "slow_down":
            interval += 5
        elif err != "authorization_pending":
            return False   # declined, expired or refused: nothing kept
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Outlook sign-in for the mail server connector (#955)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth")
    a.add_argument("--account", required=True, help="the account file's name in /ai/secrets/mail (without .json)")
    args = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mail_server
    ok = auth(mail_server.ACCOUNTS_DIR / f"{args.account}.json")
    print("Signed in; the token is in the account file." if ok else "No sign-in finished (declined or timed out).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
