#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Outlook through Microsoft Graph (titan-node#955, "all providers") against a fake Graph, fictional mail only. The
connector's sync works on it as on IMAP and Gmail: headers and threads, envelopes from the original body that's never
kept, code mails masked; Graph's long ids get stable integer uids kept beside the store; IMAP keys become $filter /
$search. Read-only: only GETs to /me and the two sign-in posts leave. The device-code sign-in writes the refresh token
into the 0600 account file and never shows it; a rotated token replaces the old one. Off until an account file exists.
Also: a generic IMAP account's host and port are checked.
Run: python3 scripts/test_mail_outlook.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.parse
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-outlook-"))
os.environ["MAIL_SERVER"] = "1"
os.environ.pop("MAIL_SERVER_KB", None)
os.environ["MAIL_SERVER_DERIVE_DAYS"] = "100000"
os.environ["LINKS_DB"] = str(_TMP / "links.db")
os.environ["LINK_SALT_FILE"] = str(_TMP / "link_salt")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
os.environ["MAIL_SERVER_DB"] = str(_TMP / "mail.db")          # the uid map lives beside it
os.environ["MAIL_ACCOUNTS_DIR"] = str(_TMP / "accounts")      # a rotated refresh token is written here
os.environ["MAIL_TRIAGE_DB"] = str(_TMP / "mail_triage.db")
sys.path.insert(0, str(Path(__file__).parent))

import mail_outlook as O  # noqa: E402
import mail_server as M  # noqa: E402
M.model_busy = lambda url=None: False

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def mail(mid, frm, subject, body, *, date="Thu, 01 Oct 2026 09:00:00 -0700", irt=None, refs=None):
    h = [f"Message-ID: {mid}", f"From: {frm}", "To: Robin <robin@outlook.example>", f"Subject: {subject}", f"Date: {date}"]
    if irt:
        h.append(f"In-Reply-To: {irt}")
    if refs:
        h.append(f"References: {refs}")
    return ("\r\n".join(h + ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8"]) + "\r\n\r\n" + body
            + "\r\n").encode()


def hdrs(raw: bytes):
    head = raw.split(b"\r\n\r\n", 1)[0].decode()
    return [{"name": ln.split(": ", 1)[0], "value": ln.split(": ", 1)[1]} for ln in head.split("\r\n")
            + ["X-Extra: not asked for"]]


LONG = "AAMkAGI2THVSAAA" + "x" * 120   # Graph ids are long opaque strings
BOX = {   # graph id → (folder, raw)
    LONG + "=1": ("inbox", mail("<pool-1@club.example>", "Jordan <jordan@club.example>", "Pool hours changed",
                                "Robin, the pool opens at 7 from Monday. LUPINE-1")),
    LONG + "=2": ("inbox", mail("<pool-2@outlook.example>", "Robin <robin@outlook.example>", "Re: Pool hours changed",
                                "Thanks! LUPINE-2", irt="<pool-1@club.example>", refs="<pool-1@club.example>",
                                date="Thu, 01 Oct 2026 10:00:00 -0700")),
    LONG + "=3": ("inbox", mail("<otp-1@bank.example>", "Bank <no-reply@bank.example>", "Your verification code",
                                "Your verification code is 552901. LUPINE-3")),
    LONG + "=4": ("archive", mail("<fern-1@fern.example>", "Fern Water <billing@fern.example>", "Your water bill",
                                  "Amount due $42.10. LUPINE-4", date="Wed, 02 Sep 2026 08:00:00 -0700")),
}
CALLS: list[tuple[str, str]] = []
REFRESH = "rt-fictional-IRIS"
ROTATED = "rt-fictional-IRIS-2"
ACCESS = "at-fictional-1"
TENANT = "consumers"


def fake_http(method, url, data, headers):
    CALLS.append((method, url.split("?")[0]))
    if url == O.token_url(TENANT):
        form = dict(urllib.parse.parse_qsl(data.decode()))
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in (REFRESH, ROTATED):
            return {"access_token": ACCESS, "refresh_token": ROTATED}
        return {"error": "invalid_grant"}
    assert headers.get("Authorization") == f"Bearer {ACCESS}", "no token on a Graph call"
    assert headers.get("Prefer") == 'IdType="ImmutableId"'
    path, _, qs = url.partition("?")
    q = dict(urllib.parse.parse_qsl(qs))
    rest = path[len(O.GRAPH) + 1:]
    if rest == "mailFolders":
        return {"value": [{"id": "F-ARCH"}] if "Archive" in q.get("$filter", "") else []}
    m = re.fullmatch(r"(?:mailFolders/([^/]+)/)?messages", rest)
    if m:
        folder = {"F-ARCH": "archive"}.get(m.group(1), m.group(1))
        ids = [g for g, (f, _r) in BOX.items() if folder is None or f == folder]
        if q.get("$search"):
            words = q["$search"].strip('"').lower().split()
            ids = [g for g in ids if all(w.split(":")[-1] in BOX[g][1].decode().lower() for w in words
                                         if not w.startswith("received"))]
        page = int(q.get("$skip", "0"))
        chunk = ids[page:page + 2]
        out = {"value": [{"id": g} for g in chunk]}
        if page + 2 < len(ids):
            out["@odata.nextLink"] = f"{O.GRAPH}/{rest}?$skip={page + 2}"
        return out
    m = re.fullmatch(r"messages/([^/]+)(/\$value)?", rest)
    if m:
        gid = urllib.parse.unquote(m.group(1))
        raw = BOX[gid][1]
        return {"_raw": raw} if m.group(2) else {"internetMessageHeaders": hdrs(raw)}
    raise AssertionError(f"unexpected Graph call {rest}")


ACCTS = Path(os.environ["MAIL_ACCOUNTS_DIR"])
ACCTS.mkdir(mode=0o700)


def account_file(name, owner="robin", **extra):
    p = ACCTS / f"{name}.json"
    p.write_text(json.dumps({"owner": owner, "provider": "outlook", "client_id": "cid-fictional", "tenant": TENANT, "folders": "auto",
                             **extra}))
    os.chmod(p, 0o600)
    return p


ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}
role_of = ROLES.get


def chat(prompt, url):
    return {"summary": "About " + re.search(r"TITLE: (.*)", prompt).group(1) + "."}, "fake"


class NoFences:
    def touches(self, text):
        return []

    def subject_in(self, text):
        return None

    def carry(self, *a):
        pass


DB = Path(os.environ["MAIL_SERVER_DB"])


def run(**kw):
    return M.sync(connect=lambda acct: fake_http, db=DB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(),
                  **kw)


print("── off until an account file exists ──")
check("no account files: nothing to sync", run(), {})

print("\n── account files ──")
p = account_file("outlook")
a = M.load_account(p, role_of)
check("an Outlook account: a client id, a tenant, no password (and no token until sign-in)",
      (a["provider"], a["tenant"], a["refresh_token"], "password" in a), ("outlook", "consumers", "", False))
for bad, why in (({"client_id": ""}, "client_id"), ({"tenant": "https://evil.example/"}, "tenant")):
    q = account_file("bad", **bad)
    try:
        M.load_account(q, role_of)
        check(f"refused: {why}", "loaded", "refused")
    except M.AccountError as exc:
        check(f"refused: {why}", why in str(exc), True)
    q.unlink()
q = account_file("kidbox", owner="ava")
try:
    M.load_account(q, role_of)
    check("a kid's mailbox: refused", "loaded", "refused")
except M.AccountError:
    check("a kid's mailbox: refused", True, True)
q.unlink()
check("not signed in yet: that account is skipped with a reason", "not signed in" in run()["outlook"]["error"], True)

print("\n── the device-code sign-in ──")
shown: list[str] = []
polls = iter([{"error": "authorization_pending"}, {"error": "slow_down"}, {"refresh_token": REFRESH}])
waits: list[float] = []


def auth_http(method, url, data, headers):
    if url == O.device_url(TENANT):
        return {"device_code": "dc-1", "user_code": "WXYZ-1234", "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5, "expires_in": 900}
    if url == O.token_url(TENANT):
        return next(polls)
    raise AssertionError(url)


clock = [0.0]
ok = O.auth(p, http=auth_http, show=shown.append, sleep=lambda s: (waits.append(s), clock.__setitem__(0, clock[0] + s)),
            now=lambda: clock[0])
check("signed in: the token is in the 0600 file", (ok, json.loads(p.read_text())["refresh_token"], oct(p.stat().st_mode & 0o777)),
      (True, REFRESH, "0o600"))
check("…it showed the link and the short code, never a token", (len(shown), "WXYZ-1234" in shown[0], REFRESH in shown[0]),
      (1, True, False))
check("…it waited, slowing down when asked", waits, [5.0, 5.0, 10.0])
q = account_file("declined")
polls = iter([{"error": "authorization_declined"}])
check("declined: nothing kept", (O.auth(q, http=auth_http, show=lambda s: None, sleep=lambda s: None, now=lambda: 0.0),
                                  "refresh_token" in json.loads(q.read_text())), (False, False))
q.unlink()

print("\n── the sync ──")
CALLS.clear()
st = run()
rs = {r["message_id"]: dict(r) for r in M._db(DB).execute("SELECT * FROM messages")}
check("every folder's mail is in (Inbox, Archive, Sent: auto)", sorted(rs), sorted(
    ["<pool-1@club.example>", "<pool-2@outlook.example>", "<otp-1@bank.example>", "<fern-1@fern.example>"]))
check("the reply joined its thread", rs["<pool-2@outlook.example>"]["thread_id"] == rs["<pool-1@club.example>"]["thread_id"],
      True)
check("the code mail is masked", (rs["<otp-1@bank.example>"]["code"], rs["<otp-1@bank.example>"]["subject"]),
      (1, M.mail_triage_masked() if hasattr(M, "mail_triage_masked") else __import__("mail_triage").MASKED_TITLE))
check("uids are integers that fit SQLite", all(0 < r["uid"] < (1 << 63) for r in rs.values()), True)
store = b"".join(Path(x).read_bytes() for x in _TMP.rglob("*.db"))
check("no body is kept anywhere (the fixtures' marker words)", [w for w in ("LUPINE-1", "LUPINE-4") if w.encode() in store], [])
check("only GETs to /me and the token post left", sorted({m for m, u in CALLS}), ["GET", "POST"])
check("…every POST was the sign-in's", {u for m, u in CALLS if m == "POST"}, {O.token_url(TENANT)})
check("the rotated refresh token replaced the old one in the file", json.loads(p.read_text())["refresh_token"], ROTATED)
check("a second sync reads no new mail", run()["outlook"].get("new", 0), 0)

print("\n── IMAP keys → Graph ──")
check("SINCE → $filter", O.search(["SINCE", "01-Oct-2026"]), (None, "receivedDateTime ge 2026-10-01T00:00:00Z"))
check("TEXT + FROM + SINCE → one KQL $search", O.search(["TEXT", '"pool hours"', "FROM", "jordan", "SINCE", "01-Oct-2026"]),
      ('"pool hours from:jordan received>=2026-10-01"', None))
check("ALL → nothing", O.search(["ALL"]), (None, None))

print("\n── read-only ──")
ol = O.Outlook({"name": "outlook", "client_id": "c", "tenant": TENANT, "refresh_token": REFRESH}, fake_http,
               keep_token=lambda *a: None)
for meth, url in (("POST", f"{O.GRAPH}/messages/x/move"), ("PATCH", f"{O.GRAPH}/messages/x"),
                  ("DELETE", f"{O.GRAPH}/messages/x"), ("GET", "https://graph.microsoft.com/v1.0/users/someone/messages"),
                  ("POST", "https://evil.example/token")):
    try:
        ol._http(meth, url, None, {})
        check(f"{meth} {url.split('/v1.0')[-1][:30]}: refused", "sent", "refused")
    except M.ReadOnlyViolation:
        check(f"{meth} {url.split('/v1.0')[-1][:30]}: refused", True, True)
for name in ("move", "store", "expunge", "copy"):
    try:
        getattr(ol, name)
        check(f"{name}: refused", "exists", "refused")
    except M.ReadOnlyViolation:
        check(f"{name}: refused", True, True)
try:
    ol.fetch([1], "RFC822")
    check("a non-PEEK fetch: refused", "fetched", "refused")
except M.ReadOnlyViolation:
    check("a non-PEEK fetch: refused", True, True)

print("\n── a generic IMAP server ──")
for extra, ok_ in (({"host": "mail.quarry.example", "port": 993}, True), ({"host": "mail.quarry.example"}, True),
                   ({"host": "imaps://mail.quarry.example"}, False), ({"host": "mail.quarry.example/x"}, False),
                   ({"host": "mail.quarry.example", "port": "abc"}, False),
                   ({"host": "mail.quarry.example", "port": 70000}, False), ({}, False)):
    g = ACCTS / "imapbox.json"
    g.write_text(json.dumps({"owner": "robin", "provider": "imap", "user": "robin@quarry.example", "password": "pw-fictional",
                             **extra}))
    os.chmod(g, 0o600)
    try:
        got = M.load_account(g, role_of)
        check(f"imap {extra}: loads (port {got['port']})", ok_, True)
    except M.AccountError:
        check(f"imap {extra}: refused", ok_, False)
    g.unlink()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
