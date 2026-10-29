#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Gmail through the Gmail API (titan-node#955 step 2) against a fake Gmail API, fictional mail only. The connector's
sync and search_mail work on it as on IMAP: headers and threads, envelopes from the original body that's never kept,
code mails masked, pagination, IMAP keys as Gmail's q. Read-only: only GETs and the token refresh leave. The one-time
sign-in writes the refresh token into the 0600 account file and never shows it.
Run: python3 scripts/test_mail_gmail.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-gmail-"))
os.environ["MAIL_SERVER"] = "1"
os.environ.pop("MAIL_SERVER_KB", None)
os.environ["MAIL_SERVER_DERIVE_DAYS"] = "100000"   # the fixtures' dates never age out; the window has its own test
os.environ["LINKS_DB"] = str(_TMP / "links.db")            # the link index (#951): never the real one
os.environ["LINK_SALT_FILE"] = str(_TMP / "link_salt")
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
sys.path.insert(0, str(Path(__file__).parent))

import egress  # noqa: E402
import mail_gmail as G  # noqa: E402
import mail_server as M  # noqa: E402
M.model_busy = lambda url=None: False   # never the real model server (the gate runs on titan)

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def mail(mid, frm, subject, body, *, date="Thu, 01 Oct 2026 09:00:00 -0700", irt=None, refs=None):
    h = [f"Message-ID: {mid}", f"From: {frm}", "To: Robin <robin@gmail.example>", f"Subject: {subject}", f"Date: {date}"]
    if irt:
        h.append(f"In-Reply-To: {irt}")
    if refs:
        h.append(f"References: {refs}")
    return ("\r\n".join(h + ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8"]) + "\r\n\r\n" + body
            + "\r\n").encode()


BOX = {  # Gmail id (hex) → (labels, raw)
    "18c0000000000a01": (["INBOX"], mail("<gym-1@club.example>", "Jordan <jordan@club.example>", "Gym class moved",
                                         "Robin, Tuesday's class moves to 6pm. MARIGOLD-1")),
    "18c0000000000a02": (["INBOX"], mail("<gym-2@gmail.example>", "Robin <robin@gmail.example>", "Re: Gym class moved",
                                         "Thanks, see you then. MARIGOLD-2", irt="<gym-1@club.example>",
                                         refs="<gym-1@club.example>", date="Thu, 01 Oct 2026 10:00:00 -0700")),
    "18c0000000000a03": (["INBOX"], mail("<otp-1@bank.example>", "Bank <no-reply@bank.example>", "Your one-time passcode",
                                         "Your one-time passcode is 771204. MARIGOLD-3")),
    "18c0000000000a04": (["ARCHIVE"], mail("<rent-1@hale.example>", "Hale Rentals <rent@hale.example>",
                                           "September rent receipt", "We received $1,250.00 rent. MARIGOLD-4",
                                           date="Wed, 02 Sep 2026 08:00:00 -0700")),
}
CALLS: list[tuple[str, str]] = []
REFRESH = "rt-fictional-PEONY"
ACCESS = "at-fictional-1"


def fake_http(method, url, data, headers):
    CALLS.append((method, url.split("?")[0]))
    if url == G.TOKEN_URL:
        form = dict(urllib.parse.parse_qsl(data.decode()))
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == REFRESH:
            return {"access_token": ACCESS, "expires_in": 3599}
        if form.get("grant_type") == "authorization_code" and form.get("code") == "code-ok":
            return {"access_token": ACCESS, "refresh_token": REFRESH}
        raise G.GmailError("Gmail answered HTTP 400")
    if headers.get("Authorization") != f"Bearer {ACCESS}":
        raise G.GmailError("Gmail answered HTTP 401")
    path, _, qs = url[len(G.API) + 1:].partition("?")
    params = urllib.parse.parse_qs(qs)
    if path == "messages":
        label = (params.get("labelIds") or [None])[0]
        q = (params.get("q") or [""])[0]
        ids = sorted((i for i, (labels, raw) in BOX.items() if (label is None or label in labels) and matches(raw, q)),
                     reverse=True)
        start = int((params.get("pageToken") or ["0"])[0])
        size = int(params["maxResults"][0])
        page = ids[start:start + size]
        out = {"messages": [{"id": i, "threadId": i} for i in page]}
        if start + size < len(ids):
            out["nextPageToken"] = str(start + size)
        return out
    mid = path.split("/")[1]
    raw = BOX[mid][1]
    if params["format"] == ["metadata"]:
        hdr = raw.decode().split("\r\n\r\n")[0]
        want = {h.lower() for h in params["metadataHeaders"]}
        hs = [{"name": n, "value": v} for n, v in (ln.split(": ", 1) for ln in hdr.split("\r\n")) if n.lower() in want]
        return {"id": mid, "payload": {"headers": hs}}
    return {"id": mid, "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}


def matches(raw, q):
    import email.utils
    from datetime import datetime
    text = raw.decode().lower()
    head = text.split("\r\n\r\n")[0]
    for term in re.findall(r'"[^"]+"|\S+', q):
        term = term.strip('"')
        if term.startswith("from:"):
            if term[5:] not in re.search(r"^from: (.*)$", head, re.M).group(1):
                return False
        elif term.startswith("after:"):
            sent = email.utils.parsedate_to_datetime(re.search(r"^date: (.*)$", head, re.M).group(1))
            if sent.date() <= datetime.strptime(term[6:], "%Y/%m/%d").date():
                return False
        elif term not in text:
            return False
    return True


ACCTS = _TMP / "accounts"
ACCTS.mkdir(mode=0o700)


def account_file(name, owner="robin", **extra):
    p = ACCTS / f"{name}.json"
    p.write_text(json.dumps({"owner": owner, "provider": "gmail", "client_id": "cid-fictional",
                             "client_secret": "cs-fictional", **extra}))
    os.chmod(p, 0o600)
    return p


ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}
role_of = ROLES.get
PROMPTS: list[str] = []


def chat(prompt, url):
    PROMPTS.append(prompt)
    return {"summary": "About " + re.search(r"TITLE: (.*)", prompt).group(1) + "."}, "fake"


class NoFences:
    def touches(self, text):
        return []

    def subject_in(self, text):
        return None

    def carry(self, *a):
        pass


DB = _TMP / "mail.db"


def connect(acct):
    return fake_http


def run(**kw):
    return M.sync(connect=connect, db=DB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), **kw)


def rows():
    con = M._db(DB)
    try:
        return {r["message_id"]: dict(r) for r in con.execute("SELECT * FROM messages")}
    finally:
        con.close()


print("── account files ──")
p = account_file("gmail")
a = M.load_account(p, role_of)
check("a Gmail account: no password, OAuth client and (later) a refresh token",
      (a["provider"], a["refresh_token"], "password" in a), ("gmail", "", False))
q = account_file("bad", client_secret="")
try:
    M.load_account(q, role_of)
    check("without the OAuth client: refused", "loaded", "refused")
except M.AccountError as exc:
    check("without the OAuth client: refused", "client_id" in str(exc), True)
q.unlink()
check("not signed in yet: that account is skipped with a reason", "not signed in" in run()["gmail"]["error"], True)

print("\n── the one-time sign-in ──")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
shown: list[str] = []


def browser(link, code="code-ok"):
    """What the person's browser does: Google redirects back to the loopback with the code and the state."""
    shown.append(link)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["state"][0]
    threading.Timer(0.3, lambda: urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?" + urllib.parse.urlencode({"code": code, "state": state}), timeout=5).read()).start()


ok = G.auth(p, http=fake_http, port=port, wait=5, show=browser)
link = urllib.parse.parse_qs(urllib.parse.urlparse(shown[0]).query)
check("the consent link asks for read-only Gmail, offline", (link["scope"], link["access_type"]),
      (["https://www.googleapis.com/auth/gmail.readonly"], ["offline"]))
check("signed in: the refresh token is in the file, still 0600", (ok, json.loads(p.read_text())["refresh_token"],
                                                                  oct(p.stat().st_mode & 0o777)), (True, REFRESH, "0o600"))
check("the token is never shown", any(REFRESH in x for x in shown), False)
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]


def forged(link):
    shown.append(link)
    threading.Timer(0.3, lambda: urllib.request.urlopen(
        f"http://127.0.0.1:{port}/?code=code-ok&state=forged", timeout=5).read()).start()


check("a redirect with someone else's state: ignored", G.auth(p, http=fake_http, port=port, wait=1.5, show=forged), False)

print("\n── sync ──")
CALLS.clear()
sink: list[str] = []
prev = egress.begin(sink)
try:
    G.PAGE = 2   # pagination, with a small page
    got = run()
finally:
    egress.begin(prev)
check("the egress ledger says mail", sink, ["mail"])
check("three INBOX mails, two envelopes (the passcode mail gets none)",
      (got["gmail"]["INBOX"]["new"], got["gmail"]["INBOX"]["derived"]), (3, 2))
r = rows()
check("the reply's thread is its original's", r["<gym-2@gmail.example>"]["thread_id"], "<gym-1@club.example>")
check("the passcode mail keeps no subject", r["<otp-1@bank.example>"]["subject"], "(a sign-in or verification code)")
check("Gmail ids are the uids, validity 1",
      (r["<gym-1@club.example>"]["uid"], r["<gym-1@club.example>"]["uidvalidity"]), (int("18c0000000000a01", 16), 1))
check("derived from the original body", any("MARIGOLD-1" in x for x in PROMPTS), True)
check("no body and no token in the store",
      [w for w in ("MARIGOLD", "771204", REFRESH, ACCESS) if w.encode() in DB.read_bytes()], [])
check("only GETs to the Gmail API and the token refresh left titan",
      sorted({m for m, u in CALLS}), ["GET", "POST"])
check("…the POST only to the token endpoint", {u for m, u in CALLS if m == "POST"}, {G.TOKEN_URL})
n = len(PROMPTS)
check("again: nothing new", (run()["gmail"]["INBOX"], len(PROMPTS)), ({"new": 0, "moved": 0, "removed": 0, "derived": 0, "hook_read": 0, "hooked": 0}, n))
del BOX["18c0000000000a02"]
check("a mail gone from Gmail goes from the store", (run()["gmail"]["INBOX"]["removed"], "<gym-2@gmail.example>" in rows()),
      (1, False))

print("\n── read-only ──")
gm = M.open_account(M.load_account(p, role_of), connect)
for name, call in (("modify (archive)", lambda: gm.modify("x")),
                   ("a POST to the API", lambda: gm._http("POST", f"{G.API}/messages/x/modify", b"{}", {})),
                   ("a DELETE", lambda: gm._http("DELETE", f"{G.API}/messages/x", None, {})),
                   ("a plain fetch", lambda: gm.fetch([1], "BODY[]"))):
    try:
        call()
        check(f"{name}: refused", "sent", "refused")
    except M.ReadOnlyViolation:
        check(f"{name}: refused before it leaves", True, True)
gm.logout()

print("\n── search_mail ──")


def find(q="", person="robin", **kw):
    return M.search(person, q, connect=connect, directory=ACCTS, role_of=role_of, **kw)


check("IMAP keys become Gmail's q", G.query(M.criteria("gym class", "jordan", 3, now=1_791_216_000.0)),
      "gym class from:jordan after:2026/10/02")
got = find("gym")
check("a match with its snippet", [(x["subject"], "moves to 6pm" in x["snippet"]) for x in got["results"]],
      [("Gym class moved", True)])
check("the passcode mail is never returned", find("passcode")["results"], [])
check("by sender", [x["subject"] for x in find(sender="jordan")["results"]], ["Gym class moved"])
account_file("gmail-all", folders=["ALL"], refresh_token=REFRESH)
check("folder ALL searches all mail (the archived receipt too)",
      sorted({x["subject"] for x in find("rent")["results"]}), ["September rent receipt"])
try:
    find("gym", person="alex")
    check("the admin has no Gmail here: nothing searched (not Robin's)", "searched", "NoMailbox")
except M.NoMailbox:
    check("the admin has no Gmail here: nothing searched (not Robin's)", True, True)
real = REFRESH
REFRESH = "rotated"
check("a revoked sign-in: that mailbox is named, not fatal", sorted(find("gym")["errors"]), ["gmail", "gmail-all"])
REFRESH = real

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
