#!/usr/bin/env python3
# gate: hermetic   (a fake IMAP server, a fake resolver and a fake POST; fictional mail; nothing leaves the machine)
"""Unsubscribing on the owner's tap (#1642, mail_unsubscribe.py): one sender's newest one-click unsubscribe
(List-Unsubscribe https + List-Unsubscribe-Post), planned read-only; a card; only the owner's own iPhone or Mac sends
it; one POST of List-Unsubscribe=One-Click to an address checked to be public https (pinned to the checked IP); a
mailto-only or page-only list said plainly; the address cleared once used; the tool wraps the sender's text.
Fictional people (Alex the admin). Run: python3 scripts/test_mail_unsubscribe.py"""
from __future__ import annotations

import json
import os
import shlex
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailunsub-"))
os.environ.update({"MAIL_SERVER": "1", "MAIL_SERVER_WRITES": "1", "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "SPACES_DB": str(_TMP / "spaces.db"), "CAPABILITY_SWITCHES": str(_TMP / "switches.json"),
                   "LINKS_DB": str(_TMP / "links.db"), "LINK_SALT_FILE": str(_TMP / "link_salt"),
                   "MAIL_SERVER_DB": str(_TMP / "mail.db"), "PROACTIVE_DB": str(_TMP / "proactive.db")})
os.environ.pop("MAIL_SERVER_KB", None)
sys.path.insert(0, str(Path(__file__).parent))
import mail_server as M  # noqa: E402
import mail_unsubscribe as U  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def mail(mid, frm, unsub="", post="", dkim="auto"):
    dom = frm.split("@")[-1].rstrip(">")
    auth = f"mx.mail.example; spf=pass; dkim=pass header.d={dom} header.s=k1" if dkim == "auto" else dkim
    extra = ((f"Authentication-Results: {auth}\r\n" if auth else "") + (f"List-Unsubscribe: {unsub}\r\n" if unsub else "")
             + (f"List-Unsubscribe-Post: {post}\r\n" if post else ""))
    return (f"Message-ID: {mid}\r\nFrom: {frm}\r\nSubject: News\r\nDate: Thu, 01 Oct 2026 09:00:00 -0700\r\n{extra}"
            "Content-Type: text/plain\r\n\r\nbody\r\n").encode()


ONE = "List-Unsubscribe=One-Click"
INBOX = {
    1: mail("<a1@harbor.example>", "Harbor News <news@harbor.example>", "<https://old.harbor.example/u/1>", ONE),
    2: mail("<a2@harbor.example>", "Harbor News <news@harbor.example>",
            "<mailto:leave@harbor.example>, <https://lists.harbor.example/u/abc?t=9>", ONE),
    3: mail("<l1@lantern.example>", "Lantern Deals <deals@lantern.example>", "<mailto:off@lantern.example>"),
    4: mail("<w1@wren.example>", "Wren Weekly <hi@wren.example>", "<https://wren.example/prefs>"),
    5: mail("<p1@robin.example>", "Robin <robin@mail.example>"),
    6: mail("<g1@gate.example>", "Gate Club <club@gate.example>", "<https://10.0.0.5/unsub>", ONE),
    7: mail("<k1@kite.example>", "Kite <kite@kite.example>", "<https://lists.kite.example/u>", ONE),
    8: mail("<k2@kite2.example>", "Kite Two <kite@kite2.example>", "<https://kite2.example/u>", ONE),
    # a forged From: someone else's address, unsigned, or signed by another domain
    9: mail("<f1@lark.example>", "Lark Mail <hi@lark.example>", "<https://track.lark-evil.example/u>", ONE, dkim=""),
    10: mail("<f2@owl.example>", "Owl Notes <hi@owl.example>", "<https://track.owl-evil.example/u>", ONE,
             dkim="mx.mail.example; dkim=pass header.d=evil.example"),
    # Harbor's newest mail is a forgery (unsigned): titan uses the older, signed one
    11: mail("<a3@harbor.example>", "Harbor News <news@harbor.example>", "<https://phish.example/u>", ONE, dkim=""),
}
LOG: list[str] = []
PASSWORD = "fake-app-pass-HERON"


class Fake(socketserver.StreamRequestHandler):
    def send(self, line):
        self.wfile.write(line if isinstance(line, bytes) else line.encode() + b"\r\n")

    def handle(self):
        self.send("* OK [CAPABILITY IMAP4rev1] fake")
        while True:
            line = self.rfile.readline().decode().rstrip("\r\n")
            if not line:
                return
            tag, _, rest = line.partition(" ")
            parts = rest.split(" ")
            cmd = parts[0].upper() + (" " + parts[1].upper() if parts[0].upper() == "UID" else "")
            LOG.append("LOGIN" if cmd == "LOGIN" else rest)
            if cmd == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 MOVE")
            elif cmd == "LOGIN":
                if PASSWORD not in rest:
                    self.send(f"{tag} NO bad")
                    continue
            elif cmd in ("EXAMINE", "SELECT"):
                self.send(f"* {len(INBOX)} EXISTS")
                self.send(f"{tag} OK [READ-ONLY] done")
                continue
            elif cmd == "UID SEARCH":
                keys = shlex.split(rest.split(" ", 2)[2])
                want = [keys[i + 1].lower() for i in range(len(keys) - 1) if keys[i].upper() == "FROM"]
                hits = [u for u, raw in sorted(INBOX.items())
                        if all(w in raw.decode().lower().split("from: ")[1].split("\r\n")[0] for w in want)]
                self.send("* SEARCH " + " ".join(map(str, hits)))
            elif cmd == "UID FETCH":
                _, _, uids, _what = rest.split(" ", 3)
                for n, u in enumerate(sorted({int(x) for x in uids.split(",")} & set(INBOX)), 1):
                    raw = INBOX[u].split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                    self.send(f"* {n} FETCH (UID {u} BODY[HEADER.FIELDS ({U.FIELDS})] {{{len(raw)}}}\r\n".encode()
                              + raw + b")\r\n")
            elif cmd == "LOGOUT":
                self.send("* BYE")
                self.send(f"{tag} OK bye")
                return
            else:
                self.send(f"{tag} NO not here")
                continue
            self.send(f"{tag} OK done")


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Fake)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
ACCTS = _TMP / "accounts"
ACCTS.mkdir(mode=0o700)
(ACCTS / "icloud.json").write_text(json.dumps({"owner": "alex", "provider": "imap", "host": "127.0.0.1",
                                               "port": srv.server_address[1], "user": "alex@mail.example", "password": PASSWORD}))
os.chmod(ACCTS / "icloud.json", 0o600)
CARDS: list[dict] = []
POSTS: list[tuple] = []
DNS = {"lists.harbor.example": "93.184.215.14", "lists.kite.example": "10.1.2.3", "kite2.example": "93.184.215.20"}


def connect(acct):
    import imaplib
    return imaplib.IMAP4(acct["host"], acct["port"], timeout=10)


def add_card(title, detail="", **kw):
    CARDS.append({"title": title, "detail": detail, **kw})
    return {"id": f"c{len(CARDS) - 1}"}


def resolve(host, port, *a, **k):
    if host not in DNS:
        raise OSError("no such name")
    return [(2, 1, 6, "", (DNS[host], port))]


def post(url, ip):
    POSTS.append((url, ip))
    return STATUS["code"]


STATUS = {"code": 200}
KW = dict(connect=connect, directory=ACCTS, role_of={"alex": "admin"}.get, resolve=None)
IPHONE = {"id": "d-ip", "platform": "ios"}
KW["resolve"] = lambda *a, **k: resolve(*a, **k)


def tap(verb, pid, device=IPHONE):
    return U.answer(f"{verb}:{pid}", "alex", "admin", device, post=post, resolve=resolve)


def writes():
    return [x for x in LOG if x.split(" ")[0].upper() in ("SELECT", "STORE", "EXPUNGE", "APPEND")
            or x.upper().startswith(("UID STORE", "UID MOVE", "UID COPY"))]


print("── the plan: read-only, the sender's newest one-click ──")
LOG.clear()
plan = U.preview("alex", sender="harbor", **KW)
check("Harbor: its newest signed one-click address (not the older one, not the newer forgery), the host for the card",
      (plan["count"], plan["host"], "Harbor News" in plan["sender"]), (3, "lists.harbor.example", True))
check("…planning only reads (EXAMINE, PEEK), nothing sent", (writes(), POSTS), ([], []))
for label, sender, why in (("mailto only", "lantern", "only offers unsubscribing by email"),
                           ("a page to open, no one-click", "wren", "needs its web page opened"),
                           ("no unsubscribe at all", "robin", "has no unsubscribe"),
                           ("two senders match", "kite", "more than one sender"),
                           ("a forged From, unsigned", "lark", "isn't signed as coming from them"),
                           ("signed, but by another domain", "owl", "isn't signed as coming from them"),
                           ("an address inside the house: no card at all", "gate", "can't send it")):
    try:
        U.preview("alex", sender=sender, **KW)
        check(f"{label}: refused", "planned", "refused")
    except U.UnsubError as exc:
        check(f"{label}: said plainly", why in str(exc), True)
try:
    U.preview("robin", sender="harbor", **KW)
    check("Robin: no mailbox", "planned", "NoMailbox")
except M.NoMailbox:
    check("Robin: no mailbox, nothing planned", True, True)

print("── the card, and only the owner's own iPhone or Mac sends ──")
U.request(plan["plan_id"], "alex", add_card=add_card)
card = CARDS[-1]
check("a card to Alex: the sender, the site, can't be undone, Unsubscribe / Not now",
      (card["to"], card["title"].startswith("Unsubscribe from Harbor News"), "lists.harbor.example" in card["detail"],
       "can't be undone" in card["detail"], [a["value"].split(":")[0] for a in card["actions"]], card["source"]),
      (["alex"], True, True, True, ["unsub", "decline"], "mail-unsub"))
for who, (pers, role, dev) in (("Robin's tap", ("robin", "adult", IPHONE)), ("Alex on a TV", ("alex", "admin", {"id": "tv", "platform": "tvos"})),
                               ("Alex with no device", ("alex", "admin", None))):
    ok, said = U.answer(f"unsub:{plan['plan_id']}", pers, role, dev, post=post, resolve=resolve)
    check(f"{who}: refused, nothing sent", (ok, POSTS), (False, []))
os.environ["MAIL_UNSUBSCRIBE"] = "0"
check("the house switch off: nothing sent", (tap("unsub", plan["plan_id"])[0], POSTS), (False, []))
os.environ["MAIL_UNSUBSCRIBE"] = "1"
import egress  # noqa: E402
SINK: list = []
egress.begin(SINK)
ok, said = tap("unsub", plan["plan_id"])
egress.begin(None)
check("…the tap notes its egress as mail-unsubscribe", SINK, ["mail-unsubscribe"])
check("Alex's iPhone: one POST to that address, at the address the check resolved",
      (ok, POSTS), (True, [("https://lists.harbor.example/u/abc?t=9", "93.184.215.14")]))
check("…said: unsubscribed, their mail stays", said.startswith("Unsubscribed from Harbor News") and "stays" in said, True)
check("a second tap: already answered", tap("unsub", plan["plan_id"]), (False, "That's been answered already."))
con = U._db()
row = dict(con.execute("SELECT * FROM mail_unsub_plans WHERE id=?", (plan["plan_id"],)).fetchone())
con.close()
check("the audit: done, who, which device, the status; the full address cleared",
      (row["state"], row["confirmed_by"], row["confirmed_device"], row["status"], row["url"]), ("done", "alex", "d-ip", 200, ""))

print("── where it may go ──")
for label, url, why in (("http", "http://lists.harbor.example/u", "plain https"),
                        ("another port", "https://lists.harbor.example:8443/u", "plain https"),
                        ("userinfo", "https://a:b@lists.harbor.example/u", "plain https"),
                        ("an IP literal", "https://93.184.215.14/u", "bare IP"),
                        ("a name inside the house", "https://lists.kite.example/u", "public internet"),
                        ("a .local name", "https://printer.local/u", "public internet"),
                        ("a name that doesn't resolve", "https://nowhere.example/u", "doesn't resolve")):
    ip, w = U.address_ok(url, resolve)
    check(f"{label}: refused", (ip, why in w), (None, True))
check("a public https name: its checked address", U.address_ok("https://lists.harbor.example/u", resolve),
      ("93.184.215.14", ""))
n = len(POSTS)
DNS["kite2.example"] = "192.168.1.20"   # the name moved inside the house after the card: the tap checks again
plan = U.preview("alex", sender="kite2", **{**KW, "resolve": lambda *a, **k: [(2, 1, 6, "", ("93.184.215.20", 443))]})
U.request(plan["plan_id"], "alex", add_card=add_card)
ok, said = tap("unsub", plan["plan_id"])
check("a name that moved inside the house after the card: the tap refuses and sends nothing",
      (ok, said.startswith("Not sent:"), len(POSTS) - n), (False, True, 0))
DNS["kite2.example"] = "93.184.215.20"
plan = U.preview("alex", sender="kite2", **KW)
U.request(plan["plan_id"], "alex", add_card=add_card)
STATUS["code"] = 410
ok, said = tap("unsub", plan["plan_id"])
check("the site says no: said, not passed off as done", (ok, "HTTP 410" in said), (False, True))
STATUS["code"] = 200
plan = U.preview("alex", sender="kite2", **KW)
check("decline: nothing sent, the address cleared", (tap("decline", plan["plan_id"]), len(POSTS) - n), ((True, "OK, nothing was sent."), 1))

stale = U.preview("alex", sender="kite2", **KW)
U.preview("alex", sender="kite2", **KW, now=__import__("time").time() + U.PLAN_TTL + 1)
c = U._db()
row = dict(c.execute("SELECT state, url FROM mail_unsub_plans WHERE id=?", (stale["plan_id"],)).fetchone())
c.close()
check("a plan nobody answered: expired, its address (a per-recipient token) cleared", row, {"state": "expired", "url": ""})
check("DKIM: header.i's domain, a subdomain, a failure, another domain, SPF only",
      [U.signed_by_sender(a, "news@harbor.example") for a in (
          "mx; dkim=pass header.i=news@mail.harbor.example", "mx; dkim=pass header.d=harbor.example",
          "mx; dkim=fail header.d=harbor.example", "mx; dkim=pass header.d=notharbor.example",
          "mx; spf=pass smtp.mailfrom=harbor.example")], [True, True, False, False, False])
import governance  # noqa: E402
check("the person's own mail switch (governance mail_moves) covers both tools",
      {"mail_unsubscribe_preview", "mail_unsubscribe"} <= governance.CAPABILITIES["mail_moves"].tools, True)

print("── the pinned connection ──")
pc = U._Pinned("lists.harbor.example", "93.184.215.14", 5)
check("connects to the checked IP, with the name for SNI and the certificate", (pc._ip, pc.host, pc.port),
      ("93.184.215.14", "lists.harbor.example", 443))

print("── the tool and the card route ──")
import agent_runtime_lib as lib  # noqa: E402
real_preview, real_request = U.preview, U.request
U.preview = lambda person, **k: real_preview(person, **{**k, **KW})
U.request = lambda plan_id, person: real_request(plan_id, person, add_card=add_card)
lib._TURN_IO.person = {"id": "alex", "name": "Alex"}
lib._TURN_IO.role = "admin"
lib._TURN_IO.capability_tiers = None
out = lib._mail_unsubscribe("mail_unsubscribe_preview", {"sender": "harbor"})
check("preview: the sender and site wrapped as the mail's text, nothing sent",
      (lib._TURN_IO.untrusted_nonce in out, out.index("Harbor") > out.index(lib._TURN_IO.untrusted_nonce),
       "Nothing has been sent" in out), (True, True, True))
pid = out.split(":")[0].split(" ")[1]
check("mail_unsubscribe: asks them to tap", lib._mail_unsubscribe("mail_unsubscribe", {"plan_id": pid}),
      "Asked: a card on their iPhone or Mac to unsubscribe. Nothing is sent until they tap Unsubscribe there.")
lib._TURN_IO.role = "kid"
check("a kid: not theirs to do", lib._mail_unsubscribe("mail_unsubscribe_preview", {"sender": "harbor"}).startswith("Unsubscribing is for"),
      True)
import proactive  # noqa: E402
import tool_descriptors as td  # noqa: E402
check("an approval card: Done on it never sends", "mail-unsub" in proactive.APPROVAL_SOURCES, True)
d = td.lookup("mail_unsubscribe")
check("mail_unsubscribe: a write that always asks, on the owner's device, one web request",
      (d.risk, d.always_ask, "mail_unsubscribe" in td.CONFIRMS_ON_DEVICE, "mail-unsubscribe" in d.egress), ("write", True, True, True))
import egress  # noqa: E402
check("egress declares it: one list of what can leave the house (Coordination, #1721)",
      ("mail-unsubscribe" in egress.KINDS, "sender-chosen public https host" in egress.__doc__), (True, True))
check("both are simulated on an eval turn", {"mail_unsubscribe_preview", "mail_unsubscribe"} <= lib._EVAL_SIMULATED, True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
