#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The mail server connector (titan-node#955 step 1) against a fake IMAP server on 127.0.0.1, fictional mail only.
Read-only (EXAMINE, BODY.PEEK, nothing that could change the mailbox); headers and threads from Message-ID,
In-Reply-To and References; the envelope derived from the original body, which is never kept; code mails masked;
UIDVALIDITY changes and removed mail; account files that are 0600 and an adult's; `mail` egress.
Run: python3 scripts/test_mail_server.py
"""
from __future__ import annotations

import imaplib
import json
import os
import re
import shlex
import socketserver
import sys
import tempfile
import time
import threading
from datetime import datetime
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailsrv-"))
os.environ["MAIL_SERVER"] = "1"
os.environ.pop("MAIL_SERVER_KB", None)
os.environ["MAIL_SERVER_DERIVE_DAYS"] = "100000"   # the fixtures' dates never age out; the window has its own test
os.environ["LINKS_DB"] = str(_TMP / "links.db")            # the link index (#951): never the real one
os.environ["LINK_SALT_FILE"] = str(_TMP / "link_salt")
os.environ["KB_LIFECYCLE_BACKUP_DIR"] = str(_TMP / "backups")   # redact-store's copy: never the real folder
os.environ["HOUSEHOLD_DB"] = str(_TMP / "household.db")
sys.path.insert(0, str(Path(__file__).parent))

import egress  # noqa: E402
import mail_server as M  # noqa: E402
REAL_BUSY = M.model_busy
M.model_busy = lambda url=None: False   # never the real model server (the gate runs on titan)

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


# ── fictional mail ──────────────────────────────────────────────────────────────────────────────────────────────────

def mail(mid, frm, subject, body, *, date="Thu, 01 Oct 2026 09:00:00 -0700", irt=None, refs=None, html=False):
    h = [f"Message-ID: {mid}", f"From: {frm}", "To: Alex <alex@mail.example>", f"Subject: {subject}", f"Date: {date}"]
    if irt:
        h.append(f"In-Reply-To: {irt}")
    if refs:
        h.append(f"References: {refs}")
    h += ["MIME-Version: 1.0", f"Content-Type: text/{'html' if html else 'plain'}; charset=utf-8"]
    return ("\r\n".join(h) + "\r\n\r\n" + body + "\r\n").encode()


BOX = {"INBOX": {"validity": 7, "msgs": {
    1: mail("<bill-1@halewater.example>", "Hale Water <billing@halewater.example>", "Your October water bill",
            "Hi Alex, your water bill of $48.20 is due October 20. PLUM-LANTERN-1"),
    2: mail("<pickup-2@mail.example>", "Robin <robin@mail.example>", "Re: Saturday pickup",
            "I can get Leo at 3. PLUM-LANTERN-2", irt="<pickup-1@mail.example>", refs="<pickup-1@mail.example>"),
    3: mail("<pickup-1@mail.example>", "Alex <alex@mail.example>", "Saturday pickup",
            "Can you pick up Leo from practice Saturday? PLUM-LANTERN-3", date="Wed, 30 Sep 2026 18:00:00 -0700"),
    4: mail("<code-1@login.example>", "Login <no-reply@login.example>", "Your verification code",
            "Your verification code is 482913. PLUM-LANTERN-4"),
    5: mail("<paid-1@halewater.example>", "Hale Water <billing@halewater.example>", "Payment received",
            "<html><style>p{color:red}</style><p>Thanks for your payment of $48.20, Alex.</p> PLUM-LANTERN-5</html>",
            html=True),
    6: mail("<pickup-3@mail.example>", "Alex <alex@mail.example>", "Re: Saturday pickup", "Thanks! PLUM-LANTERN-6",
            irt="<pickup-2@mail.example>", refs="<pickup-1@mail.example> <pickup-2@mail.example>"),
}}}
LOG: list[str] = []
LISTED = [("\\HasNoChildren", "INBOX"), ("\\HasNoChildren \\Archive", "Archive"),
          ("\\HasNoChildren \\Sent", "Sent Messages"), ("\\HasNoChildren \\Junk", "Junk"),
          ("\\Noselect \\HasChildren", "Projects")]
PASSWORD = "fake-app-pass-ORCHID"


def _uidset(s):
    out = set()
    for part in s.split(","):
        a, _, b = part.partition(":")
        out |= set(range(int(a), int(b) + 1)) if b else {int(a)}
    return out


class Fake(socketserver.StreamRequestHandler):
    def send(self, line):
        self.wfile.write(line if isinstance(line, bytes) else line.encode() + b"\r\n")

    def handle(self):
        self.send("* OK [CAPABILITY IMAP4rev1] fake ready")
        folder = None
        while True:
            line = self.rfile.readline().decode().rstrip("\r\n")
            if not line:
                return
            tag, _, rest = line.partition(" ")
            cmd = rest.split(" ")[0].upper()
            if cmd == "UID":
                cmd = "UID " + rest.split(" ")[1].upper()
            LOG.append(rest if cmd != "LOGIN" else "LOGIN")
            if cmd == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
            elif cmd == "LOGIN":
                if PASSWORD not in rest:
                    self.send(f"{tag} NO [AUTHENTICATIONFAILED] bad login")
                    continue
            elif cmd == "EXAMINE":
                folder = rest.split(" ", 1)[1].strip('"')
                box = BOX[folder]
                self.send(f"* {len(box['msgs'])} EXISTS")
                self.send(f"* OK [UIDVALIDITY {box['validity']}] ok")
                self.send(f"{tag} OK [READ-ONLY] EXAMINE completed")
                continue
            elif cmd == "UID SEARCH":
                keys = shlex.split(rest.split(" ", 2)[2])
                hits = [u for u, raw in sorted(BOX[folder]["msgs"].items()) if matches(raw, keys)]
                self.send("* SEARCH " + " ".join(str(u) for u in hits))
            elif cmd == "UID FETCH":
                _, _, uids, what = rest.split(" ", 3)
                msgs = BOX[folder]["msgs"]
                for n, uid in enumerate(sorted(_uidset(uids) & set(msgs)), 1):
                    raw = msgs[uid]
                    if "HEADER.FIELDS" in what:
                        hdr, _, _ = raw.partition(b"\r\n\r\n")
                        raw, item = hdr + b"\r\n\r\n", "BODY[HEADER.FIELDS (" + M.HEADER_FIELDS + ")]"
                    elif "<0." in what:
                        raw, item = raw[:int(what.split("<0.")[1].split(">")[0])], "BODY[]<0>"
                    else:
                        item = "BODY[]"
                    self.send(f"* {n} FETCH (UID {uid} {item} {{{len(raw)}}}\r\n".encode() + raw + b")\r\n")
            elif cmd == "LIST":
                for flags, name in LISTED:
                    self.send(f'* LIST ({flags}) "/" "{name}"')
            elif cmd == "LOGOUT":
                self.send("* BYE")
                self.send(f"{tag} OK LOGOUT completed")
                return
            else:
                self.send(f"{tag} NO not here")
                continue
            self.send(f"{tag} OK done")


def matches(raw, keys):
    """IMAP SEARCH as the fake understands it: ALL, TEXT, FROM, SINCE (ANDed)."""
    import email.utils
    low, ok, i = raw.decode().lower(), True, 0
    head = raw.decode().split("\r\n\r\n")[0].lower()
    while i < len(keys):
        k = keys[i].upper()
        if k == "ALL":
            i += 1
            continue
        v = keys[i + 1]
        if k == "TEXT":
            ok &= v.lower() in low
        elif k == "FROM":
            ok &= v.lower() in re.search(r"^from: (.*)$", head, re.M).group(1)
        elif k == "SINCE":
            sent = email.utils.parsedate_to_datetime(re.search(r"^date: (.*)$", head, re.M).group(1))
            ok &= sent.date() >= datetime.strptime(v, "%d-%b-%Y").date()
        i += 2
    return ok


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Fake)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def connect(acct):
    return imaplib.IMAP4(acct["host"], acct["port"], timeout=10)


ACCTS = _TMP / "accounts"
ACCTS.mkdir(mode=0o700)


def account_file(name, owner="alex", mode=0o600, **extra):
    p = ACCTS / f"{name}.json"
    p.write_text(json.dumps({"owner": owner, "provider": "imap", "host": "127.0.0.1", "port": PORT,
                             "user": "alex@mail.example", "password": PASSWORD, **extra}))
    os.chmod(p, mode)
    return p


ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}
role_of = ROLES.get
PROMPTS: list[str] = []


def chat(prompt, url):
    PROMPTS.append(prompt)
    title = re.search(r"TITLE: (.*)", prompt).group(1)
    return {"summary": f"About {title}."}, "fake"


class NoFences:
    def touches(self, text):
        return []

    def subject_in(self, text):
        return None

    def carry(self, *a):
        pass


DB = _TMP / "mail.db"


def run(**kw):
    return M.sync(connect=connect, db=DB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), **kw)


def rows():
    con = M._db(DB)
    try:
        return {r["message_id"]: dict(r) for r in con.execute("SELECT * FROM messages")}
    finally:
        con.close()


print("── account files ──")
p = account_file("icloud", mode=0o644)
try:
    M.load_account(p, role_of)
    check("a file others can read is refused", "accepted", "refused")
except M.AccountError as exc:
    check("a file others can read is refused", "0600" in str(exc) and PASSWORD not in str(exc), True)
os.chmod(p, 0o600)
check("0600 and the admin's: loaded", M.load_account(p, role_of)["owner"], "alex")
for owner, why in (("ava", "kids"), ("nobody", "household member")):
    q = account_file("other", owner=owner)
    try:
        M.load_account(q, role_of)
        check(f"owner {owner}: refused", "accepted", "refused")
    except M.AccountError as exc:
        check(f"owner {owner}: refused ({why})", why in str(exc), True)
    q.unlink()
check("iCloud's server by default", M.PROVIDERS["icloud"], ("imap.mail.me.com", 993))

os.environ["MAIL_SERVER"] = "0"
try:
    run()
    check("MAIL_SERVER off: no connection", "synced", "refused")
except RuntimeError:
    check("MAIL_SERVER off: no connection", LOG, [])
os.environ["MAIL_SERVER"] = "1"

print("\n── the first sync ──")
sink: list[str] = []
prev = egress.begin(sink)
try:
    got = run()
finally:
    egress.begin(prev)
check("the egress ledger says mail", sink, ["mail"])
check("six headers, five envelopes (the code mail gets none)",
      (got["icloud"]["INBOX"]["new"], got["icloud"]["INBOX"]["derived"]), (6, 5))
r = rows()
check("threads from headers: the reply before its original, and a reply to the reply, all one thread",
      {r[m]["thread_id"] for m in ("<pickup-1@mail.example>", "<pickup-2@mail.example>", "<pickup-3@mail.example>")},
      {"<pickup-1@mail.example>"})
check("a mail on its own is its own thread", r["<bill-1@halewater.example>"]["thread_id"], "<bill-1@halewater.example>")
check("the code mail keeps no subject and no envelope",
      (r["<code-1@login.example>"]["subject"], r["<code-1@login.example>"]["envelope"]),
      ("(a sign-in or verification code)", None))
env = json.loads(r["<bill-1@halewater.example>"]["envelope"])
check("the envelope's summary", env["interpretation"]["summary"], "About Your October water bill.")
check("…carries who it's for and where it came from",
      {k: env["record"].get(k) for k in ("owner", "audience", "account", "upstream_id", "observed_at")},
      {"owner": "alex", "audience": "private", "account": "icloud", "upstream_id": "<bill-1@halewater.example>",
       "observed_at": "2026-10-01T16:00:00Z"})
check("derived from the original body (#954), never a summary", any("PLUM-LANTERN-1" in x for x in PROMPTS), True)
check("an HTML-only mail: its text without tags or styles",
      any("Thanks for your payment of $48.20" in x and "<p>" not in x and "color:red" not in x for x in PROMPTS), True)
check("the code mail's body never reached the model", any("482913" in x for x in PROMPTS), False)
blob = DB.read_bytes()
check("no body in the store", [w for w in ("PLUM-LANTERN", "Leo from practice", "482913") if w.encode() in blob], [])
check("no password in the store", PASSWORD.encode() in blob, False)

print("\n── read-only ──")
check("folders opened with EXAMINE only", (sum(x.startswith("EXAMINE") for x in LOG) > 0,
                                           [x for x in LOG if x.split(" ")[0].upper() in ("SELECT", "STORE", "COPY",
                                                                                          "MOVE", "EXPUNGE", "APPEND",
                                                                                          "DELETE", "CREATE")]),
      (True, []))
fetches = [x for x in LOG if x.upper().startswith("UID FETCH")]
check("every fetch is BODY.PEEK", bool(fetches) and all("BODY.PEEK[" in x for x in fetches), True)
conn = M.open_account(M.load_account(p, role_of), connect)
for name, call in (("store", lambda: conn.store("1", "+FLAGS", "\\Seen")), ("expunge", lambda: conn.expunge()),
                   ("a plain fetch", lambda: conn.fetch([1], "BODY[]")), ("RFC822", lambda: conn.fetch([1], "RFC822"))):
    try:
        call()
        check(f"{name}: refused", "sent", "refused")
    except M.ReadOnlyViolation:
        check(f"{name}: refused before the server", True, True)
conn.logout()

print("\n── again, and when the server changes ──")
n_prompts, n_fetch = len(PROMPTS), len(fetches)
got = run()
check("nothing new: no headers, no bodies, no model", (got["icloud"]["INBOX"], len(PROMPTS)),
      ({"new": 0, "moved": 0, "removed": 0, "derived": 0, "hook_read": 0, "hooked": 0}, n_prompts))
del BOX["INBOX"]["msgs"][2]
got = run()
check("a mail gone from the server goes from the store", (got["icloud"]["INBOX"]["removed"],
                                                          "<pickup-2@mail.example>" in rows()), (1, False))
BOX["INBOX"] = {"validity": 9, "msgs": {10 + u: m for u, m in BOX["INBOX"]["msgs"].items()}}
before = {k: v["envelope"] for k, v in rows().items()}
got = run()
after = rows()
check("new UIDVALIDITY: every mail carried over by Message-ID, nothing re-derived",
      (got["icloud"]["INBOX"]["moved"], got["icloud"]["INBOX"]["derived"], len(PROMPTS)), (5, 0, n_prompts))
check("…same envelopes, new addresses", ({k: v["envelope"] for k, v in after.items()} == before,
                                         sorted((v["uidvalidity"], v["uid"]) for v in after.values())),
      (True, [(9, 11), (9, 13), (9, 14), (9, 15), (9, 16)]))

print("\n── the knowledge base, with MAIL_SERVER_KB ──")
BOX["INBOX"]["msgs"][20] = mail("<bill-2@halewater.example>", "Hale Water <billing@halewater.example>",
                                "Your November water bill", "Your bill of $51.10 is due November 19. PLUM-LANTERN-20",
                                date="Sun, 04 Oct 2026 08:00:00 -0700")
WRITES: list[tuple] = []
got = run(kb=True, write=lambda *a: WRITES.append(a), observe=lambda sid, title, text, md: [])
sid, title, text, st, md = WRITES[0]
check("one entry into the KB: its thread's envelope, never the mail's own (#446 v2)",
      (len(WRITES), sid.startswith("cx-mailthread-"), st, md["kind"]), (1, True, "connector:mail", "mail_thread"))
check("what's indexed is the envelope, not the body", ("About Your November water bill." in text,
                                                      "PLUM-LANTERN" in text), (True, False))
check("…owner-only, as mail (so it enters a prompt inside the untrusted markers, #473), with its thread and the "
      "mails it was built from (the references a body is fetched through)",
      (md["owner"], md["audience"], md["source"], md["thread"]["thread_id"], md["thread"]["mails"],
       md["references"] == [rows()["<bill-2@halewater.example>"]["item_id"]]),
      ("alex", "private", "mail", "<bill-2@halewater.example>", 1, True))
check("…participants by display name, never an address", ("Participants: Hale Water" in text, "@" in text), (True, False))
check("the store remembers it's in the KB (through its thread)", rows()["<bill-2@halewater.example>"]["kb"], 1)

print("\n── a thread's envelope: one entry, rewritten as the conversation grows ──")
BOX["INBOX"]["msgs"][21] = mail("<deck-1@mail.example>", "Robin Quarry <robin@mail.example>", "Deck stain colours",
                                "Which stain did we pick for the deck? CEDAR-ORCHID-1", date="Mon, 05 Oct 2026 09:00:00 -0700")
WRITES.clear()
run(kb=True, write=lambda *a: WRITES.append(a), observe=lambda sid, title, text, md: [])
BOX["INBOX"]["msgs"][22] = mail("<deck-2@mail.example>", "Jordan Hale <jordan@hale.example>", "Re: Deck stain colours",
                                "The grey one, Saturday works. CEDAR-ORCHID-2", date="Tue, 06 Oct 2026 10:00:00 -0700",
                                irt="<deck-1@mail.example>", refs="<deck-1@mail.example>")
run(kb=True, write=lambda *a: WRITES.append(a), observe=lambda sid, title, text, md: [])
deck = [w for w in WRITES if w[4]["thread"]["thread_id"] == "<deck-1@mail.example>"]
check("the reply rewrites the same thread entry (one id), now with both mails",
      (len({w[0] for w in deck}), deck[-1][4]["thread"]["mails"]), (1, 2))
_s, _t, ttext, _st, tmd = deck[-1]
check("…subject, participants, dates, then each mail's summary; never a body",
      (ttext.splitlines()[0], "Participants: Robin Quarry, Jordan Hale" in ttext, "2026-10-05 to 2026-10-06, 2 mails" in ttext,
       ttext.count("• "), "CEDAR-ORCHID" in ttext, "@" in ttext),
      ("Re: Deck stain colours", True, True, 2, False, False))
import agent_runtime_lib as _lib  # noqa: E402
_lib._TURN_IO.untrusted_nonce = "abcd1234"
marked = _lib._kb_mark({"metadata": tmd, "source_type": "connector:mail"}, ttext)
check("a thread envelope enters a prompt inside the untrusted markers as mail (#473)",
      (marked.startswith("⟦untrusted source=mail"), ttext in marked), (True, True))
check("claims: a mail's world observations as short lines", M.claims_of([
      {"name": "Hale Water", "attr": "amount_due", "value": "$51.10"}, {"entity": "x", "attr": "", "value": "y"}]),
      ["Hale Water: amount due $51.10"])

print("\n── a mail's body on demand (#446 v2: the server is the store; its row is the reference) ──")
refs = M.references_for("alex", DB)
deck_ref = next(r for r in refs if r["native_id"] == "<deck-2@mail.example>")
check("the person's own server mail as references: their rows, newest first, private, no copy",
      (len(refs), refs[0]["native_id"], deck_ref["owner"], deck_ref["audience"], deck_ref["kind"],
       deck_ref["device_id"].startswith("server:"), "text" in deck_ref), (len(rows()) - 1, "<deck-2@mail.example>", "alex",
                                                                         "private", "mail", True, False))
check("…nobody else's: Robin has none of Alex's", M.references_for("robin", DB), [])
got = M.read_body("alex", deck_ref, connect=connect, directory=ACCTS, role_of=role_of)
check("read now from the server (BODY.PEEK), for this turn only", (got["found"], "CEDAR-ORCHID-2" in got["text"]), (True, True))
check("…capped", len(M.read_body("alex", deck_ref, max_chars=10, connect=connect, directory=ACCTS, role_of=role_of)["text"]), 10)
check("…never for anyone but the owner (no mailbox of theirs holds it)",
      M.read_body("robin", deck_ref, connect=connect, directory=ACCTS, role_of=role_of), {"error": "no_mailbox"})
gone = {**deck_ref, "server": {**deck_ref["server"], "uid": 999}}
check("a mail no longer on the server: found false", M.read_body("alex", gone, connect=connect, directory=ACCTS,
                                                                 role_of=role_of), {"found": False})
check("the store still holds no body after reading", any("CEDAR-ORCHID" in str(v) for r in rows().values() for v in r.values()),
      False)

print("\n── status ──")
s = M.status(DB)[0]
check("counts only", {k: s[k] for k in ("account", "folder", "mails", "envelopes", "codes", "in_kb")},
      {"account": "icloud", "folder": "INBOX", "mails": 8, "envelopes": 7, "codes": 1, "in_kb": 3})
check("no subject or address in it", any(isinstance(v, str) and "@" in v for v in s.values()), False)

print("\n── search_mail on the server (#955 step 4) ──")
LOG.clear()


def find(person="alex", q="", **kw):
    return M.search(person, q, connect=connect, directory=ACCTS, role_of=role_of, **kw)


got = find(q="water")
check("every mail with the word, headers too (IMAP TEXT), newest first",
      ([r["subject"] for r in got["results"]][0], sorted(r["subject"] for r in got["results"])),
      ("Your November water bill", ["Payment received", "Your November water bill", "Your October water bill"]))
check("…each with date, sender, subject and a snippet around the word",
      (lambda r: (r["date"], r["sender"], "water bill of $48.20" in r["snippet"]))(
          next(r for r in got["results"] if r["subject"] == "Your October water bill")),
      ("2026-10-01T16:00:00Z", "Hale Water <billing@halewater.example>", True))
check("every word must be there", [r["subject"] for r in find(q="water November")["results"]],
      ["Your November water bill"])
check("by sender", sorted(r["subject"] for r in find(q="Saturday", sender="robin")["results"]), [])
check("…the sender's own mail", sorted(r["subject"] for r in find(sender="alex@mail.example")["results"]),
      ["Re: Saturday pickup", "Saturday pickup"])
check("how far back", [r["subject"] for r in find(q="water", days=2, now=1_791_216_000.0)["results"]],
      ["Your November water bill"])
check("an HTML mail's snippet is its text", find(q="payment")["results"][0]["snippet"].startswith("Thanks for your payment"),
      True)
check("a verification code mail is never returned", find(q="verification")["results"], [])
check("…not even by its words", find(q="482913")["results"], [])
check("limit", len(find(q="Alex", limit=2)["results"]), 2)
check("read-only: EXAMINE, PEEK, nothing else",
      (all(x.split(" ")[0].upper() in ("CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT") for x in LOG),
       all("PEEK" in x for x in LOG if x.upper().startswith("UID FETCH"))), (True, True))
try:
    find(person="robin", q="water")
    check("someone with no mailbox: none searched", "searched", "NoMailbox")
except M.NoMailbox:
    check("someone with no mailbox here: nothing searched, not Alex's", True, True)
try:
    find(q="")
    check("nothing to look for: refused", "searched", "refused")
except ValueError:
    check("nothing to look for: refused", True, True)
check("the words become SEARCH keys, quoted",
      M.criteria('water "bill"', "Hale", 3, now=1_791_216_000.0),
      ['TEXT', '"water"', 'TEXT', '"bill"', 'FROM', '"Hale"', 'SINCE', '2-Oct-2026'])
sink = []
prev = egress.begin(sink)
try:
    find(q="water")
finally:
    egress.begin(prev)
check("a search notes mail egress", sink, ["mail"])
PASSWORD_SAVED = PASSWORD
try:
    globals()["PASSWORD"] = "changed"
    check("a mailbox that can't be reached is named, not fatal", find(q="water"), {"results": [], "errors": ["icloud"]})
finally:
    globals()["PASSWORD"] = PASSWORD_SAVED

print("\n── the search_mail tool ──")
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
check("a read, offered with MAIL_SERVER on", (lib._TOOL_REGISTRY["search_mail"]["risk"], lib._role_allows("search_mail")),
      ("read", True))
os.environ["MAIL_SERVER"] = "0"
check("…and not offered while it's off (no prompt change)", (lib._role_allows("search_mail"),
      "search_mail" in {d["name"] for d in lib._gemini_function_declarations()}), (False, False))
os.environ["MAIL_SERVER"] = "1"
check("adults and the admin; never a kid or a shared device",
      [hh.role_allows_tool(r, "search_mail") for r in ("admin", "adult", "kid", "household")], [True, True, False, False])
real_search, real_who = M.search, lib._kb_who
M.search = lambda person, q, **kw: real_search(person, q, connect=connect, directory=ACCTS, role_of=role_of, **kw)
try:
    lib._kb_who = lambda: ("alex", "admin")
    out = lib._execute_tool("search_mail", {"query": "water", "limit": "5"})
    check("the result: newest first, as someone else's text", (out.splitlines()[0].startswith("Your mail matching 'water' (3"),
                                                               "untrusted source=mail" in out, out.index("November") < out.index("October")),
          (True, True, True))
    lib._kb_who = lambda: ("robin", "adult")
    check("no mailbox of theirs: says so", "None of your mailboxes" in lib._execute_tool("search_mail", {"query": "water"}), True)
    lib._kb_who = lambda: (None, "household")
    check("a shared device: no mail", lib._execute_tool("search_mail", {"query": "water"}),
          "Mail belongs to a person; a shared device has none.")
finally:
    M.search, lib._kb_who = real_search, real_who
import claim_checks  # noqa: E402
check("a reply citing email after search_mail isn't corrected (#953)",
      claim_checks.check_unread_source("Your email says the bill is $51.10.", [{"tool": "search_mail", "text": ""}]), [])

print("\n── real mail's malformed headers (10-05: the first live sync died on a Message-ID) ──")


def odd(mid_line, subject, body, extra=()):
    head = [mid_line, "From: =?utf-8?q?Hale_B=C3=A4ckerei?= <shop@hale.example>", f"Subject: {subject}", *extra,
            "Content-Type: text/plain; charset=x-unknown-charset"]
    return ("\r\n".join(h for h in head if h) + "\r\n\r\n" + body + "\r\n").encode()


BOX["Odd"] = {"validity": 3, "msgs": {
    1: odd("Message-ID:", "Empty id", "one"),
    2: odd("Message-ID: <>", "Angle brackets only", "two"),
    3: odd("Message-ID: no-brackets@x.example", "No brackets", "three"),
    4: odd("Message-ID: <@x>", "No local part", "four"),
    5: odd("Message-ID: example.com", "A bare domain", "five", ("Date: Thu, 99 Foo 2026 25:61:00 +9999",)),
    6: odd("Message-ID: <<a@b.example>", "Doubled bracket", "six", ("References: <<a@b.example> <> <@x>",
                                                                     "In-Reply-To: <@x>")),
    7: odd("", "No id at all", "seven", ("In-Reply-To: garbage", "References: <ok-1@hale.example> <@>")),
    8: odd("Message-ID: <ok-1@hale.example>", "=?utf-8?b?R3LDvMOfZQ==?=", "eight"),
}}
ODB = _TMP / "odd.db"
oddfile = account_file("odd")
raw_odd = json.loads(oddfile.read_text())
raw_odd["folders"] = ["Odd"]
oddfile.write_text(json.dumps(raw_odd))
os.chmod(oddfile, 0o600)
icloud = ACCTS / "icloud.json"
icloud.rename(_TMP / "icloud.json.off")       # only the odd account this time
got = M.sync(connect=connect, db=ODB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences())
icloud_back = (_TMP / "icloud.json.off").rename(icloud)
check("the sync goes on past every malformed header: all eight read, all eight enveloped",
      (got["odd"]["Odd"]["new"], got["odd"]["Odd"]["derived"]), (8, 8))
con = M._db(ODB)
o = {r["subject"]: dict(r) for r in con.execute("SELECT * FROM messages")}
con.close()
synthetic = {k for k, v in o.items() if v["message_id"].startswith("<uid-3-")}
check("no usable Message-ID: each gets its own synthetic id",
      sorted(synthetic), sorted(["Empty id", "Angle brackets only", "No brackets", "No local part", "A bare domain",
                                 "No id at all"]))
check("…all distinct", len({o[k]["message_id"] for k in synthetic}), 6)
check("a doubled bracket: the id inside it is kept; <> and <@x> are no links",
      (o["Doubled bracket"]["message_id"], o["Doubled bracket"]["in_reply_to"], json.loads(o["Doubled bracket"]["refs"])),
      ("<a@b.example>", None, ["<a@b.example>"]))
check("garbage In-Reply-To is no link; the good reference still threads",
      (o["No id at all"]["in_reply_to"], o["No id at all"]["thread_id"]), (None, "<ok-1@hale.example>"))
check("an impossible Date is no date", o["A bare domain"]["sent_at"], None)
check("encoded words decode (subject and sender)",
      ("Grüße" in o, o["Grüße"]["sender"]), (True, "Hale Bäckerei <shop@hale.example>"))
check("an unknown charset still gives the body to the summariser", any("TITLE: Empty id\nTEXT:\none" in x for x in PROMPTS),
      True)
check("search goes past them too", sorted(r["subject"] for r in M.search(
      "alex", "Hale", connect=connect, directory=ACCTS, role_of=role_of, limit=20)["results"] if r["folder"] == "Odd"),
      sorted(o))
for bad in (b"", b"\xff\xfe\x00garbage", b"Message-ID: <>\r\nReferences: <<>\r\n\r\n"):
    try:
        M.parse_headers(bad), M.body_text(bad)
        check(f"parse never raises: {bad[:12]!r}", True, True)
    except Exception as exc:  # noqa: BLE001
        check(f"parse never raises: {bad[:12]!r}", type(exc).__name__, None)

print("\n── threads whatever order the mail arrives in (10-05 live: 2 replies apart from their parent) ──")
BOX["Chain"] = {"validity": 4, "msgs": {
    1: mail("<c-1@mail.example>", "Alex <alex@mail.example>", "Plans", "first", date="Mon, 28 Sep 2026 09:00:00 -0700"),
    2: mail("<c-2@mail.example>", "Robin <robin@mail.example>", "Re: Plans", "second", irt="<c-1@mail.example>",
            refs="<c-1@mail.example>", date="Tue, 29 Sep 2026 09:00:00 -0700"),
    3: mail("<c-3@mail.example>", "Alex <alex@mail.example>", "Re: Plans", "third (In-Reply-To only)",
            irt="<c-2@mail.example>", date="Wed, 30 Sep 2026 09:00:00 -0700"),
}}
chain = account_file("chain")
raw_c = json.loads(chain.read_text())
raw_c["folders"] = ["Chain"]
chain.write_text(json.dumps(raw_c))
os.chmod(chain, 0o600)
for f in ("icloud", "odd"):
    (ACCTS / f"{f}.json").rename(_TMP / f"{f}.json.off")
M.BATCH, CDB = 1, _TMP / "chain.db"     # one header per fetch, newest first: each reply lands before its parent
got = M.sync(connect=connect, db=CDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences())
M.BATCH = 100
for f in ("icloud", "odd"):
    (_TMP / f"{f}.json.off").rename(ACCTS / f"{f}.json")
con = M._db(CDB)
th = {r["message_id"]: r["thread_id"] for r in con.execute("SELECT message_id, thread_id FROM messages")}
check("a reply stored before its parent still joins the thread (In-Reply-To only, too)", set(th.values()),
      {"<c-1@mail.example>"})
check("…the sync says how many it moved", got["chain"]["Chain"]["rethreaded"] >= 1, True)
con.execute("UPDATE messages SET thread_id=message_id")
check("rethread mends a store that's apart", (M.rethread(con, "chain"), len({r[0] for r in con.execute(
      "SELECT thread_id FROM messages")})), (2, 1))
check("…and again changes nothing", M.rethread(con, "chain"), 0)
con.close()

print("\n── link hooks from the original with its headers (#951) ──")
import sqlite3 as _sq  # noqa: E402


def hooks_of(iid):
    c = _sq.connect(os.environ["LINKS_DB"])
    try:
        return {(k, key) for k, key in c.execute("SELECT kind, key FROM hooks WHERE item_id=?", (iid,))}
    finally:
        c.close()


r = rows()
bill = r["<bill-1@halewater.example>"]
kinds = {k for k, _ in hooks_of(bill["item_id"])}
check("a derived mail has hooks from its headers: sender address and domain, its Message-ID",
      {"party:addr", "party:domain", "id:message"} <= kinds, True)
check("…and from its body (the amount)", ("amount", "48.20") in hooks_of(bill["item_id"]), True)
check("…the domain is the sender's", ("party:domain", "halewater.example") in hooks_of(bill["item_id"]), True)
reply = r["<pickup-3@mail.example>"]
first = r["<pickup-1@mail.example>"]
shared = {k for k in hooks_of(reply["item_id"]) if k[0] == "id:message"} & {k for k in hooks_of(first["item_id"]) if k[0] == "id:message"}
check("a reply shares a message hook with its original (References), so the linker can join them", bool(shared), True)
check("the code mail has no hooks", hooks_of(r["<code-1@login.example>"]["item_id"]), set())
check("the link index holds no body text and no address", [w for w in (b"PLUM-LANTERN", b"billing@halewater")
                                                           if w in Path(os.environ["LINKS_DB"]).read_bytes()], [])
check("with_headers leaves out our synthetic ids", "Message-ID" in M.with_headers(
      {"refs": "[]", "message_id": "<uid-1-2@INBOX>", "sender": "x", "sent_at": "", "in_reply_to": None}, "b"), False)
nov = r["<bill-2@halewater.example>"]
check("the KB path too: after envelope_one, the header hooks are the ones kept",
      ("party:domain", "halewater.example") in hooks_of(nov["item_id"]), True)

print("\n── hooks-backfill: mail derived before the hooks reached this connector ──")
c = _sq.connect(os.environ["LINKS_DB"])
c.execute("DELETE FROM hooks")
c.commit()
c.close()
before = sorted(tuple(x) for x in M._db(DB).execute("SELECT * FROM messages").fetchall())
LOG.clear()
got = M.hooks_backfill(connect=connect, db=DB, directory=ACCTS, role_of=role_of)
check("every derived mail read once and indexed", (got["icloud"]["mails"], got["icloud"]["unread"],
                                                    got["icloud"]["hooks"] > got["icloud"]["mails"]),
      (sum(1 for v in rows().values() if v["derived_at"] and not v["code"]), 0, True))
check("…read-only: EXAMINE and BODY.PEEK only", all(x.split(" ")[0].upper() in ("CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT")
                                                   and ("PEEK" in x or not x.upper().startswith("UID FETCH")) for x in LOG), True)
check("…nothing else in the store changed", sorted(tuple(x) for x in M._db(DB).execute("SELECT * FROM messages").fetchall()),
      before)
check("…the hooks are back", ("party:domain", "halewater.example") in hooks_of(bill["item_id"]), True)

print("\n── hooks-backfill --revector: hooked mail with no topic vector (#951 (e)) ──")
import topics  # noqa: E402
EMBEDDED: list[str] = []
real_embed = topics.embed
topics.embed = lambda texts: (EMBEDDED.extend(texts), [[0.1, 0.2, 0.3] for _ in texts])[1]
try:
    c_ = __import__("sqlite3").connect(os.environ["LINKS_DB"])
    c_.executescript(topics.SCHEMA)
    c_.execute("DELETE FROM topics")
    c_.commit()
    c_.close()
    LOG.clear()
    got = M.hooks_backfill(connect=connect, db=DB, directory=ACCTS, role_of=role_of, revector=True, limit=2)
    check("revector: hooked mail without a vector, at most the limit", (got["icloud"]["mails"], len(EMBEDDED)), (2, 2))
    # (the topic text drops leading header lines in topics.topic_text itself: KB's fix, for every caller)
    check("…read-only (EXAMINE, PEEK)", all(x.split(" ")[0].upper() in ("CAPABILITY", "LOGIN", "EXAMINE", "UID", "LOGOUT")
                                          and ("PEEK" in x or not x.upper().startswith("UID FETCH")) for x in LOG), True)
    rest = M.hooks_backfill(connect=connect, db=DB, directory=ACCTS, role_of=role_of, revector=True, limit=1000)
    again = M.hooks_backfill(connect=connect, db=DB, directory=ACCTS, role_of=role_of, revector=True, limit=1000)
    check("…the rest next time, then nothing left", (rest["icloud"]["mails"] > 0, again["icloud"]["mails"]), (True, 0))
    check("…and no body in the link index", b"PLUM-LANTERN" in Path(os.environ["LINKS_DB"]).read_bytes(), False)
finally:
    topics.embed = real_embed


print("\n── the 30-minute timer: headers always, envelopes only when the local model is free ──")
BOX["INBOX"]["msgs"][30] = mail("<busy-1@hale.example>", "Hale Water <billing@halewater.example>", "December bill",
                                "Your bill of $50.00 is due December 18.", date="Mon, 05 Oct 2026 08:00:00 -0700")
n = len(PROMPTS)
got = run(busy=lambda: True)
check("busy: the new header is stored, no envelope, and the run says why",
      (got["icloud"]["INBOX"]["new"], got["icloud"]["INBOX"]["derived"], "busy" in got["icloud"]["skipped"], len(PROMPTS)),
      (1, 0, True, n))
got = run(busy=lambda: False)
check("free again: the waiting envelope is made", (got["icloud"]["INBOX"]["derived"],
                                                   sum("TITLE: December bill" in x for x in PROMPTS[n:])), (1, 1))


class Slots:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


import urllib.request as _ur  # noqa: E402
real_open = _ur.urlopen
try:
    busy_fn = REAL_BUSY
    for body, want in ((b'[{"id":0,"is_processing":true}]', True), (b'[{"id":0,"is_processing":false}]', False),
                       (b'[{"id":0,"is_processing":true},{"id":1,"is_processing":false}]', False), (b"{}", False)):
        _ur.urlopen = lambda *a, **k: Slots(body)
        check(f"model_busy reads /slots: {body.decode()[:40]} → {want}", busy_fn("http://127.0.0.1:1"), want)
    _ur.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("refused"))
    check("model_busy: no answer counts as free", busy_fn("http://127.0.0.1:1"), False)
finally:
    _ur.urlopen = real_open

units = Path(M.__file__).parent.parent / "server-setup"
svc = (units / "titan-mail-server.service").read_text()
check("the 30-minute unit syncs with MAIL_SERVER on and the KB off, in the unit only",
      ("mail_server.py sync" in svc, "Environment=MAIL_SERVER=1" in svc, "Environment=MAIL_SERVER_KB=0" in svc,
       "OnUnitActiveSec=30min" in (units / "titan-mail-server.timer").read_text()), (True, True, True, True))

print("\n── search_mail ranks what it read; folders \"auto\" (10-05: \"trade-in\" brought back trade digests) ──")
BOX["INBOX"] = {"validity": 21, "msgs": {
    1: mail("<coin-1@digest.example>", "Coin Digest <news@digest.example>", "Your weekly trade summary",
            "You made 3 trades this week. Trade volume is up.", date="Sat, 03 Oct 2026 09:00:00 -0700"),
    2: mail("<post-1@post.example>", "Post <news@post.example>", "Informed delivery",
            "Mail arriving today. Your package is in transit.", date="Sun, 04 Oct 2026 09:00:00 -0700"),
}}
BOX["Archive"] = {"validity": 22, "msgs": {
    5: mail("<car-1@carrier.example>", "Hale Wireless <care@carrier.example>", "Your device return",
            "We received your phone. Your trade-in credit of $120.00 posts in 2 bills.", date="Thu, 01 Oct 2026 09:00:00 -0700"),
    6: mail("<car-2@carrier.example>", "Hale Wireless <care@carrier.example>", "Trade-in offer confirmed",
            "Thanks for choosing us.", date="Fri, 25 Sep 2026 09:00:00 -0700"),
}}
BOX["Sent Messages"] = {"validity": 23, "msgs": {
    7: mail("<me-1@mail.example>", "Alex <alex@mail.example>", "Question", "Is the trade in value final?",
            date="Fri, 02 Oct 2026 09:00:00 -0700"),
}}
BOX["Junk"] = {"validity": 24, "msgs": {9: mail("<junk-1@x.example>", "Spam <s@x.example>", "trade-in now", "trade-in!!!")}}
check("relevance: the phrase in the subject, in the text, every word, some, none",
      [M.relevance("trade-in", h, t) for h, t in (("Trade-in offer", ""), ("Return", "your trade-in credit"),
                                                     ("x", "trade in"), ("x", "trade"), ("x", "tradein"))],
      [3, 2, -1, -1, -1])
check("…several words", [M.relevance("water bill", h, t) for h, t in (("Your water bill", ""), ("x", "the water bill is"),
                                                                       ("x", "bill for water"), ("x", "water"), ("x", "x"))],
      [3, 2, 1, 0, -1])
autofile = account_file("auto")
raw_a = json.loads(autofile.read_text())
raw_a["folders"] = "auto"
autofile.write_text(json.dumps(raw_a))
os.chmod(autofile, 0o600)
for f in ("icloud", "odd", "chain"):
    if (ACCTS / f"{f}.json").exists():
        (ACCTS / f"{f}.json").rename(_TMP / f"{f}.json.off")
check("folders auto: INBOX, the \\Archive and \\Sent folders; never Junk or a Noselect",
      M._folders(M.open_account(M.load_account(autofile, role_of), connect), M.load_account(autofile, role_of)),
      ["INBOX", "Archive", "Sent Messages"])
got = find(q="trade-in")["results"]
check("\"trade-in\": the carrier's mails first, phrase in the subject before the text; no trade digest",
      [(r["subject"], r["folder"]) for r in got],
      [("Trade-in offer confirmed", "Archive"), ("Your device return", "Archive")])
check("…the snippet is around the phrase", "trade-in credit of $120.00" in got[1]["snippet"], True)
got = find(q="trade in")["results"]
check("\"trade in\" (two words): the phrase first, then every word, then some; newest first within",
      [r["subject"] for r in got], ["Question", "Your device return", "Trade-in offer confirmed", "Your weekly trade summary"])
check("…the sent mail with the phrase leads", got[0]["subject"], "Question")
check("a sender-only search isn't filtered", [r["subject"] for r in find(sender="hale")["results"]],
      ["Your device return", "Trade-in offer confirmed"])
for f in ("icloud", "odd", "chain"):
    if (_TMP / f"{f}.json.off").exists():
        (_TMP / f"{f}.json.off").rename(ACCTS / f"{f}.json")
autofile.unlink()

print("\n── serials and IMEIs never leave the connector (#979: a reply showed a trade-in Watch's IMEI) ──")
IMEI, SERIAL = "356938035643809", "F2LXK9P3Q1M7"
BOX["Carrier"] = {"validity": 31, "msgs": {
    1: mail("<tradein-1@carrier.example>", "Hale Wireless <care@carrier.example>", f"Trade-in received, serial {SERIAL}",
            f"Serial number: {SERIAL} " + "x" * 50 + f" trade-in received. Device IMEI: {IMEI}. Credit $120.00 in 2 bills.",
            date="Sat, 03 Oct 2026 09:00:00 -0700"),
}}
carrier = account_file("carrier")
raw_c = json.loads(carrier.read_text())
raw_c["folders"] = ["Carrier"]
carrier.write_text(json.dumps(raw_c))
os.chmod(carrier, 0o600)
parked = [f for f in ACCTS.glob("*.json") if f.stem != "carrier"]
for f in parked:
    f.rename(_TMP / (f.name + ".off"))
CDB2, n = _TMP / "carrier.db", len(PROMPTS)
M.sync(connect=connect, db=CDB2, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences())
check("the store holds neither (the subject is masked when it's read)",
      [w for w in (IMEI, SERIAL) if w.encode() in CDB2.read_bytes()], [])
check("the summariser never sees them", [w for w in (IMEI, SERIAL) if any(w in x for x in PROMPTS[n:])], [])
check("…but it saw the mail", any("Credit $120.00" in x for x in PROMPTS[n:]), True)
got = find(q="trade-in")["results"]
shown = json.dumps(got)
check("search_mail: neither in the subject or the snippet", (len(got), [w for w in (IMEI, SERIAL) if w in shown]), (1, []))
check("…even when the snippet's cut falls inside the serial's label", "F2LXK" in got[0]["snippet"], False)
c_ = __import__("sqlite3").connect(os.environ["LINKS_DB"])
iid = M._db(CDB2).execute("SELECT item_id FROM messages").fetchone()[0]
check("the link index still has the IMEI hook (a keyed hash, from the original)",
      bool(c_.execute("SELECT 1 FROM hooks WHERE item_id=? AND kind='id:imei'", (iid,)).fetchone()), True)
check("…never the number itself", IMEI.encode() in Path(os.environ["LINKS_DB"]).read_bytes(), False)
c_.close()
for f in parked:
    (_TMP / (f.name + ".off")).rename(f)
carrier.unlink()

print("\n── redact-store: what was stored before #979 is masked in place, after a 0600 backup ──")
RDB = _TMP / "redact.db"
con = M._db(RDB)
env = {"record": {"title": "Return label for serial F2LXK9P3Q1M7"}, "interpretation": {"summary": "A return label."}}
for uid, subj, env_ in ((1, "Return label for serial F2LXK9P3Q1M7", env), (2, "Your October water bill", None)):
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, sender, "
                "subject, envelope) VALUES ('icloud','INBOX',1,?,'alex','x',?,?,'Hale <care@carrier.example>',?,?)",
                (uid, f"<r{uid}@x.example>", f"<r{uid}@x.example>", subj, json.dumps(env_) if env_ else None))
con.commit()
con.close()
got = M.redact_store(db=RDB, now=1_791_216_000.0)
bdir = Path(os.environ["KB_LIFECYCLE_BACKUP_DIR"])
backup = bdir / got["backup"]
check("counts only: the one subject and the one envelope", {k: got[k] for k in ("mails", "subjects", "senders", "envelopes")},
      {"mails": 2, "subjects": 1, "senders": 0, "envelopes": 1})
check("…the store no longer holds the serial", b"F2LXK9P3Q1M7" in RDB.read_bytes(), False)
check("…the other mail is untouched", M._db(RDB).execute("SELECT subject FROM messages WHERE uid=2").fetchone()[0],
      "Your October water bill")
check("the backup is the whole store before, 0600, named for retention",
      (got["backup"].startswith("mail-server-") and got["backup"].endswith(".db"), oct(backup.stat().st_mode & 0o777),
       b"F2LXK9P3Q1M7" in backup.read_bytes()), (True, "0o600", True))
again = M.redact_store(db=RDB, now=1_791_216_100.0)
check("again: nothing left to change", (again["subjects"], again["envelopes"]), (0, 0))
import backup_retention as BR  # noqa: E402
check("backup_retention covers it, in the backups folder", "mail-server-*.db" in BR.LIFECYCLE_DIR_PATTERNS, True)
check("…kept under 30 days, shredded after",
      (BR.prune(bdir, "mail-server-*.db", now=1_791_216_000.0 + 86400 * 29, apply=False),
       sorted(BR.prune(bdir, "mail-server-*.db", now=time.time() + 86400 * 31, apply=True)),
       list(bdir.glob("mail-server-*.db"))), ([], sorted([got["backup"], again["backup"]]), []))

print("\n── envelopes for the last 90 days only; older mail gets hooks, no model (Coordination, 10-05) ──")
BOX["Window"] = {"validity": 41, "msgs": {
    1: mail("<old-1@hale.example>", "Hale Water <billing@halewater.example>", "May water bill",
            "Your bill of $41.00 is due May 20. Account ref HW-2291.", date="Fri, 01 May 2026 09:00:00 -0700"),
    2: mail("<old-2@login.example>", "Login <no-reply@login.example>", "Hello",
            "Your verification code is 771204.", date="Sat, 02 May 2026 09:00:00 -0700"),
    3: mail("<new-1@hale.example>", "Hale Water <billing@halewater.example>", "September water bill",
            "Your bill of $44.10 is due September 21.", date="Tue, 15 Sep 2026 09:00:00 -0700"),
    4: odd("", "Undated note", "no date here"),
}}
win = account_file("window")
raw_w = json.loads(win.read_text())
raw_w["folders"] = ["Window"]
win.write_text(json.dumps(raw_w))
os.chmod(win, 0o600)
parked = [f for f in ACCTS.glob("*.json") if f.stem != "window"]
for f in parked:
    f.rename(_TMP / (f.name + ".off"))
WDB, n = _TMP / "window.db", len(PROMPTS)
ON = 1_791_216_000.0    # 2026-10-05: May is outside the 90 days, September inside
got = M.sync(connect=connect, db=WDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), now=ON,
             derive_days=90)
w = {r["subject"]: dict(r) for r in M._db(WDB).execute("SELECT * FROM messages")}
check("inside the window: an envelope (and undated mail counts as recent)",
      (w["September water bill"]["envelope"] is not None, w["Undated note"]["envelope"] is not None), (True, True))
check("outside it: no envelope, the model never saw it", (w["May water bill"]["envelope"], any("May water bill" in x for x in PROMPTS[n:])),
      (None, False))
check("…but its link hooks are indexed (no model)", (w["May water bill"]["hooked_at"] is not None,
                                                    ("amount", "41.00") in hooks_of(w["May water bill"]["item_id"])), (True, True))
check("an old code mail found by its body keeps nothing, hooks included",
      (w.get("(a sign-in or verification code)", {}).get("code"), hooks_of(w.get("(a sign-in or verification code)", {}).get("item_id", "x"))),
      (1, set()))
check("the run's counts", (got["window"]["Window"]["derived"], got["window"]["Window"]["hooked"]), (2, 1))
again = M.sync(connect=connect, db=WDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), now=ON, derive_days=90)
check("again: nothing re-read", again["window"]["Window"], {"new": 0, "moved": 0, "removed": 0, "derived": 0, "hook_read": 0, "hooked": 0})
for f in parked:
    (_TMP / (f.name + ".off")).rename(f)
win.unlink()

print("\n── newest first across folders, one budget (#1410: a thread's Sent reply with its INBOX side) ──")
BOX["NInbox"] = {"validity": 41, "msgs": {
    1: mail("<nf-i1@x.example>", "Robin <robin@mail.example>", "Inbox oldest", "a", date="Mon, 07 Sep 2026 09:00:00 -0700"),
    2: mail("<nf-i2@x.example>", "Robin <robin@mail.example>", "Inbox newest", "b", date="Thu, 08 Oct 2026 09:00:00 -0700"),
}}
BOX["NArchive"] = {"validity": 42, "msgs": {   # the highest UIDs, the oldest mail: uid order would take these first
    50: mail("<nf-a1@x.example>", "Jordan <jordan@mail.example>", "Archive old", "c", date="Tue, 01 Sep 2026 09:00:00 -0700"),
    51: mail("<nf-a2@x.example>", "Jordan <jordan@mail.example>", "Archive middle", "d", date="Tue, 22 Sep 2026 09:00:00 -0700"),
    52: odd("", "Archive undated", "no date here"),
}}
BOX["NSent"] = {"validity": 43, "msgs": {
    3: mail("<nf-s1@x.example>", "Alex <alex@mail.example>", "Sent this week", "e", date="Fri, 09 Oct 2026 09:00:00 -0700",
            irt="<nf-i2@x.example>", refs="<nf-i2@x.example>"),
    4: mail("<nf-s2@x.example>", "Alex <alex@mail.example>", "Sent last month", "f", date="Wed, 16 Sep 2026 09:00:00 -0700"),
}}
nff = account_file("newest")
raw_n = json.loads(nff.read_text())
raw_n["folders"] = ["NInbox", "NArchive", "NSent"]
nff.write_text(json.dumps(raw_n))
os.chmod(nff, 0o600)
parked = [f for f in ACCTS.glob("*.json") if f.stem != "newest"]
for f in parked:
    f.rename(_TMP / (f.name + ".off"))
NDB = _TMP / "newest.db"
derived = lambda: [r[0] for r in M._db(NDB).execute(  # noqa: E731
    "SELECT subject FROM messages WHERE derived_at IS NOT NULL ORDER BY derived_at, folder")]
got = M.sync(connect=connect, db=NDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), derive_max=3)
check("a budget of 3 takes the 3 newest, whatever their folder (never the Archive's high UIDs first)",
      sorted(derived()), sorted(["Inbox newest", "Sent this week", "Archive middle"]))
check("…every folder's headers are read all the same", M._db(NDB).execute("SELECT COUNT(*) FROM messages").fetchone()[0], 7)
check("…the counts per folder", {f: got["newest"][f]["derived"] for f in ("NInbox", "NArchive", "NSent")},
      {"NInbox": 1, "NArchive": 1, "NSent": 1})
first = set(derived())
M.sync(connect=connect, db=NDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), derive_max=3)
check("the next run: the next 3 newest, the undated one still waiting (dated mail first)",
      sorted(set(derived()) - first), sorted(["Sent last month", "Inbox oldest", "Archive old"]))
M.sync(connect=connect, db=NDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), derive_max=3)
check("…then the undated one (in the window, last)", derived()[-1], "Archive undated")
BOX["NArchive"]["validity"] = 44   # renumbered between runs: headers read again under the new numbering
BOX["NArchive"]["msgs"][53] = mail("<nf-a3@x.example>", "Jordan <jordan@mail.example>", "Archive renumbered new", "g",
                                   date="Sat, 10 Oct 2026 09:00:00 -0700")
M.sync(connect=connect, db=NDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), derive_max=1)
check("after a renumbering, its newest mail is derived under the new numbering",
      "Archive renumbered new" in derived(), True)
for f in parked:
    (_TMP / (f.name + ".off")).rename(f)
nff.unlink()

print("\n── older mail drains a run at a time: hook_read is what was fetched, never the same mail twice (10-10) ──")
BOX["Backlog"] = {"validity": 51, "msgs": {
    i: mail(f"<bk-{i}@hale.example>", "Hale Water <billing@halewater.example>", f"Old bill {i}",
            f"Your bill of ${40 + i}.00 is due May {i}.", date=f"Fri, 0{i} May 2026 09:00:00 -0700") for i in range(1, 6)}}
bkf = account_file("backlog")
raw_b = json.loads(bkf.read_text())
raw_b["folders"] = ["Backlog"]
bkf.write_text(json.dumps(raw_b))
os.chmod(bkf, 0o600)
parked = [f for f in ACCTS.glob("*.json") if f.stem != "backlog"]
for f in parked:
    f.rename(_TMP / (f.name + ".off"))
BDB = _TMP / "backlog.db"
read, marked = [], []
for _ in range(4):
    st = M.sync(connect=connect, db=BDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), now=ON,
                derive_days=90, hooks_max=2)["backlog"]["Backlog"]
    read.append(st["hook_read"])
    marked.append(M._db(BDB).execute("SELECT COUNT(*) FROM messages WHERE hooked_at IS NOT NULL").fetchone()[0])
check("a backlog of 5 at 2 a run: fetched 2, 2, 1, then 0", read, [2, 2, 1, 0])
check("…each run marks new mail (2, 4, 5, 5): none fetched twice", marked, [2, 4, 5, 5])
check("…hooked counts the indexed ones and never exceeds hook_read", st["hooked"] <= st["hook_read"], True)
for f in parked:
    (_TMP / (f.name + ".off")).rename(f)
bkf.unlink()

print("\n── a standing keep rule (#955, 10-10): a phrase's threads, any age, newest first, the owner's only ──")
BOX["Keep"] = {"validity": 61, "msgs": {
    1: mail("<kp-1@hale.example>", "Jordan <jordan@hale.example>", "Harbor Project kickoff", "Kickoff is set.",
            date="Mon, 03 Mar 2025 09:00:00 -0800"),
    2: mail("<kp-2@hale.example>", "Jordan <jordan@hale.example>", "Re: next steps", "Agreed on next steps.",
            date="Tue, 04 Mar 2025 09:00:00 -0800", irt="<kp-1@hale.example>", refs="<kp-1@hale.example>"),
    3: mail("<kp-3@hale.example>", "Jordan <jordan@hale.example>", "Lunch?", "Lunch Thursday?", date="Wed, 05 Mar 2025 09:00:00 -0800"),
    4: mail("<kp-4@hale.example>", "Leo <leo@other.example>", "Harbor projections", "Not the project.",
            date="Thu, 06 Mar 2025 09:00:00 -0800"),
    5: mail("<kp-5@ci.example>", "Builds <no-reply@ci.example>", "Harbor Project build passed", "Build 41 passed.",
            date="Fri, 07 Mar 2025 09:00:00 -0800"),
}}
kpf = account_file("keep")
raw_k = json.loads(kpf.read_text())
raw_k["folders"] = ["Keep"]
kpf.write_text(json.dumps(raw_k))
os.chmod(kpf, 0o600)
parked = [f for f in ACCTS.glob("*.json") if f.stem != "keep"]
for f in parked:
    f.rename(_TMP / (f.name + ".off"))
KDB = _TMP / "keep.db"
keepfile = _TMP / "mail_keep.json"
owner = raw_k.get("owner") or "alex"
keepfile.write_text(json.dumps({owner: {"phrases": ["harbor project"]}, "robin": {"phrases": ["lunch"]}}))
os.chmod(keepfile, 0o600)
M.KEEP_FILE = keepfile
got = M.sync(connect=connect, db=KDB, directory=ACCTS, role_of=role_of, chat=chat, fences=NoFences(), now=ON, derive_days=90)
k = {r["subject"]: dict(r) for r in M._db(KDB).execute("SELECT * FROM messages")}
check("a 19-month-old mail with the phrase: derived (past the 90-day window), recorded as kept",
      (k["Harbor Project kickoff"]["derived_at"] is not None, k["Harbor Project kickoff"]["passes"]), (True, "keep"))
check("…its reply that lost the phrase: derived through its thread", k["Re: next steps"]["derived_at"] is not None, True)
check("an old mail from the same sender without the phrase: not derived", k["Lunch?"]["derived_at"], None)
check("an old build ping with the phrase (a no-reply sender): enveloped locally as keep-bulk, never the KB",
      (k["Harbor Project build passed"]["passes"], k["Harbor Project build passed"]["kb"]), ("keep-bulk", 0))
check("automated(): bulk headers, or a no-reply / notifications address; a person's address isn't",
      [M.automated(a, b) for a, b in (("Jordan <jordan@hale.example>", False), ("x <no-reply@x.example>", False),
                                      ("x <notifications@x.example>", False), ("Jordan <jordan@hale.example>", True))],
      [False, True, True, True])
check("a word that only starts like the phrase (\"Harbor projections\"): not kept", k["Harbor projections"]["derived_at"], None)
check("another person's rule (robin's \"lunch\") never reaches this owner's mailbox", k["Lunch?"]["passes"], None)
os.chmod(keepfile, 0o644)
check("a keep file that isn't 0600 is ignored entirely", M.load_keep(keepfile), {})
os.chmod(keepfile, 0o600)
check("phrases are read lowercased; shorter than 3 characters ignored",
      (keepfile.write_text(json.dumps({"x": {"phrases": ["Harbor Project", "ab"]}})), M.load_keep(keepfile)["x"][0]["words"])[1],
      {"harbor project"})
keepfile.write_text(json.dumps({"x": [{"phrases": ["harbor project"], "project": "Harbor", "facet": "Work"},
                                      {"phrases": ["dock"], "project": "y" * 81, "facet": "household"}]}))
r = M.load_keep(keepfile)["x"]
check("rules as a list, each with its checked scope (a facet lowercased; a too-long name or a non-person facet dropped)",
      [x["scope"] for x in r], [{"project": "Harbor", "facet": "work"}, {}])
M.KEEP_FILE = _TMP / "no-keep.json"
for f in parked:
    (_TMP / (f.name + ".off")).rename(f)
kpf.unlink()

print("\n── rederive: the window derived again at cut-over, envelopes kept until replaced ──")
RDB2 = _TMP / "rederive.db"
con = M._db(RDB2)
for uid, sent, code in ((1, "2026-09-20T09:00:00Z", 0), (2, "2026-05-01T09:00:00Z", 0), (3, "2026-09-21T09:00:00Z", 1),
                        (4, None, 0)):
    con.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, sent_at, "
                "code, envelope, derived_at) VALUES ('icloud','INBOX',1,?,'alex','x',?,?,?,?,?,?)",
                (uid, f"<rd{uid}@x.example>", f"<rd{uid}@x.example>", sent, code, json.dumps({"kind": "item_envelope"}), 1.0))
con.commit()
con.close()
got = M.rederive(90, db=RDB2, now=1_791_216_000.0)
rows_ = {r["uid"]: dict(r) for r in M._db(RDB2).execute("SELECT * FROM messages")}
check("only recent (and undated) non-code mail is marked", (got["marked"], sorted(u for u, r in rows_.items() if r["derived_at"] is None)),
      (2, [1, 4]))
check("…each keeps its envelope until the new one replaces it", all(r["envelope"] for r in rows_.values() if not r["code"]), True)
check("…a second run marks nothing new", M.rederive(90, db=RDB2, now=1_791_216_000.0)["marked"], 0)

print("── a KB source already gone is done, not a failed sync (10-13: one 404 stopped every sync) ──")
import urllib.error  # noqa: E402
import agent_runtime_lib as _lib  # noqa: E402
_real = _lib._runtime_request
def _raises(code):
    def f(*a, **k):
        raise urllib.error.HTTPError("http://127.0.0.1:9100/v1/retrieval/sources/x", code, "x", {}, None)
    return f
try:
    _lib._runtime_request = _raises(404)
    try:
        M._kb_delete("cx-mailsrv-alex-1")
        ok404 = True
    except Exception:
        ok404 = False
    check("a 404 on delete is treated as already deleted", ok404, True)
    _lib._runtime_request = _raises(500)
    try:
        M._kb_delete("cx-mailsrv-alex-1")
        raised500 = False
    except urllib.error.HTTPError:
        raised500 = True
    check("…any other error still fails the sync", raised500, True)
finally:
    _lib._runtime_request = _real

srv.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
