#!/usr/bin/env python3
# gate: hermetic   (a fake IMAP server, fictional mail; touches nothing live)
"""Marking mail on the server (#1642, mail_moves' marks): read, unread, flag, unflag, where the mail is. A plan holds
only the mails whose flag would change, the owner's own iPhone or Mac taps, one UID STORE of \\Seen or \\Flagged (never
\\Deleted, never EXPUNGE, nothing moves), and Undo puts back exactly those. The writer refuses any other mark. The tool
says what it'll do. Fictional people (Alex the admin). Run: python3 scripts/test_mail_marks.py"""
from __future__ import annotations

import json
import os
import shlex
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailmarks-"))
os.environ.update({"MAIL_SERVER": "1", "MAIL_SERVER_WRITES": "1", "HOUSEHOLD_DB": str(_TMP / "household.db"),
                   "SPACES_DB": str(_TMP / "spaces.db"), "CAPABILITY_SWITCHES": str(_TMP / "switches.json"),
                   "LINKS_DB": str(_TMP / "links.db"), "LINK_SALT_FILE": str(_TMP / "link_salt"),
                   "MAIL_SERVER_DB": str(_TMP / "mail.db"), "PROACTIVE_DB": str(_TMP / "proactive.db")})
os.environ.pop("MAIL_SERVER_KB", None)
sys.path.insert(0, str(Path(__file__).parent))
import mail_moves as MM  # noqa: E402
import mail_server as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


def mail(mid, frm, subject):
    return (f"Message-ID: {mid}\r\nFrom: {frm}\r\nSubject: {subject}\r\nDate: Thu, 01 Oct 2026 09:00:00 -0700\r\n"
            "Content-Type: text/plain\r\n\r\nbody\r\n").encode()


BOX = {"INBOX": {i: mail(f"<hw-{i}@halewater.example>", "Hale Water <billing@halewater.example>", f"Bill {i}")
                 for i in range(1, 4)}, "Archive": {}, "Trash": {}}
BOX["INBOX"][4] = mail("<rb-1@mail.example>", "Robin <robin@mail.example>", "Saturday")
FLAGS: dict[int, set] = {1: set(), 2: {"\\Seen"}, 3: set(), 4: set()}   # Bill 2 is read already
LOG: list[str] = []
PASSWORD = "fake-app-pass-WREN"


class Fake(socketserver.StreamRequestHandler):
    def send(self, line):
        self.wfile.write(line if isinstance(line, bytes) else line.encode() + b"\r\n")

    def handle(self):
        self.send("* OK [CAPABILITY IMAP4rev1] fake")
        folder = None
        while True:
            line = self.rfile.readline().decode().rstrip("\r\n")
            if not line:
                return
            tag, _, rest = line.partition(" ")
            parts = rest.split(" ")
            cmd = parts[0].upper() + (" " + parts[1].upper() if parts[0].upper() == "UID" else "")
            LOG.append("LOGIN" if cmd == "LOGIN" else rest)
            if cmd == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 UIDPLUS MOVE")
            elif cmd == "LOGIN":
                if PASSWORD not in rest:
                    self.send(f"{tag} NO bad")
                    continue
            elif cmd == "LIST":
                for flags, name in (("\\HasNoChildren", "INBOX"), ("\\HasNoChildren \\Archive", "Archive"),
                                    ("\\HasNoChildren \\Trash", "Trash")):
                    self.send(f'* LIST ({flags}) "/" "{name}"')
            elif cmd in ("EXAMINE", "SELECT"):
                folder = rest.split(" ", 1)[1].strip('"')
                self.send(f"* {len(BOX[folder])} EXISTS")
                self.send(f"{tag} OK [{'READ-ONLY' if cmd == 'EXAMINE' else 'READ-WRITE'}] done")
                continue
            elif cmd == "UID SEARCH":
                keys = shlex.split(rest.split(" ", 2)[2])
                self.send("* SEARCH " + " ".join(str(u) for u, raw in sorted(BOX[folder].items()) if match(u, raw, keys)))
            elif cmd == "UID FETCH":
                _, _, uids, _what = rest.split(" ", 3)
                for n, u in enumerate(sorted({int(x) for x in uids.split(",")} & set(BOX[folder])), 1):
                    raw = BOX[folder][u].split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                    self.send(f"* {n} FETCH (UID {u} BODY[HEADER.FIELDS ({M.HEADER_FIELDS})] {{{len(raw)}}}\r\n".encode()
                              + raw + b")\r\n")
            elif cmd == "UID STORE":
                _, _, uids, op, flag = rest.split(" ", 4)
                for u in (int(x) for x in uids.split(",")):
                    (FLAGS[u].add if op.startswith("+") else FLAGS[u].discard)(flag.strip("()"))
            elif cmd == "LOGOUT":
                self.send("* BYE")
                self.send(f"{tag} OK bye")
                return
            else:
                self.send(f"{tag} NO not here")
                continue
            self.send(f"{tag} OK done")


def match(uid, raw, keys):
    low, i = raw.decode().lower(), 0
    state = {"SEEN": "\\Seen" in FLAGS[uid], "UNSEEN": "\\Seen" not in FLAGS[uid],
             "FLAGGED": "\\Flagged" in FLAGS[uid], "UNFLAGGED": "\\Flagged" not in FLAGS[uid]}
    while i < len(keys):
        k = keys[i].upper()
        if k in state:
            if not state[k]:
                return False
            i += 1
            continue
        if k == "HEADER":
            if f"{keys[i + 1].lower()}: {keys[i + 2].lower()}" not in low:
                return False
            i += 3
            continue
        v = keys[i + 1].lower()
        if (k == "TEXT" and v not in low) or (k == "FROM" and v not in low.split("from: ")[1].split("\r\n")[0]):
            return False
        i += 2
    return True


srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Fake)
srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
ACCTS = _TMP / "accounts"
ACCTS.mkdir(mode=0o700)
(ACCTS / "icloud.json").write_text(json.dumps({"owner": "alex", "provider": "imap", "host": "127.0.0.1",
                                               "port": srv.server_address[1], "user": "alex@mail.example", "password": PASSWORD}))
os.chmod(ACCTS / "icloud.json", 0o600)
CARDS: list[dict] = []


def connect(acct):
    import imaplib
    return imaplib.IMAP4(acct["host"], acct["port"], timeout=10)


def add_card(title, detail="", **kw):
    CARDS.append({"title": title, "detail": detail, **kw})
    return {"id": f"c{len(CARDS) - 1}"}


KW = dict(connect=connect, directory=ACCTS, role_of={"alex": "admin"}.get)
IPHONE = {"id": "d-ip", "platform": "ios"}


def tap(verb, pid, device=IPHONE):
    return MM.answer(f"{verb}:{pid}", "alex", "admin", device, add_card=add_card, **KW)


def changes():
    return [x for x in LOG if x.upper().startswith(("UID STORE", "UID MOVE", "UID COPY", "UID EXPUNGE", "EXPUNGE", "STORE"))]


print("── mark read: only the unread ones ──")
LOG.clear()
plan = MM.preview("alex", sender="halewater", mark="read", **KW)
check("Hale Water's two unread bills (Bill 2 is read already): where they are, nothing changed yet",
      (plan["count"], plan["verb"], plan["from"], plan["to"], changes()), (2, "read", "INBOX", "INBOX", []))
MM.request(plan["plan_id"], "alex", add_card=add_card)
check("a card: Mark 2 mails read?, tapped Mark read, undo for 7 days",
      (CARDS[-1]["title"], CARDS[-1]["actions"][0]["label"], "undo it for 7 days" in CARDS[-1]["detail"]),
      ("Mark 2 mails read?", "Mark read", True))
check("a TV can't tap it", (tap("move", plan["plan_id"], {"id": "tv", "platform": "tvos"})[0], changes()), (False, []))
LOG.clear()
ok, said = tap("move", plan["plan_id"])
check("Alex's iPhone: marked read", (ok, said, sorted(u for u in (1, 2, 3) if "\\Seen" in FLAGS[u])),
      (True, "Marked 2 mails read.", [1, 2, 3]))
check("…one silent UID STORE of \\Seen; nothing moved, nothing expunged",
      (len(changes()), changes()[0].split(" ")[3:], len(BOX["INBOX"])), (1, ["+FLAGS.SILENT", "(\\Seen)"], 4))
check("…the result card offers Undo", CARDS[-1]["actions"][0]["value"].split(":")[0], "undo")
ok, said = MM.undo(plan["plan_id"], "alex", add_card=add_card)
check("undo: asked the same way", (ok, said), (True, "Confirm on your iPhone or Mac to put 2 back as they were."))
back = CARDS[-1]["data"]["plan_id"]
check("…the card says Mark 2 mails unread?", CARDS[-1]["title"], "Mark 2 mails unread?")
ok, said = tap("move", back)
check("…tapped: exactly those two unread again, Bill 2 still read", (ok, sorted(u for u in (1, 2, 3) if "\\Seen" in FLAGS[u])),
      (True, [2]))

print("── flag and unflag ──")
plan = MM.preview("alex", query="Saturday", mark="flag", **KW)
MM.request(plan["plan_id"], "alex", add_card=add_card)
check("Flag 1 mail?", CARDS[-1]["title"], "Flag 1 mail?")
tap("move", plan["plan_id"])
check("flagged", "\\Flagged" in FLAGS[4], True)
again = MM.preview("alex", query="Saturday", mark="flag", **KW)
check("flagging it again: nothing to do (already flagged)", again["count"], 0)
unf = MM.preview("alex", query="Saturday", mark="unflag", **KW)
MM.request(unf["plan_id"], "alex", add_card=add_card)
check("unflag: Unflag 1 mail?, done", (CARDS[-1]["title"], tap("move", unf["plan_id"])[1], "\\Flagged" in FLAGS[4]),
      ("Unflag 1 mail?", "Unflagged 1 mail.", False))

print("── refusals ──")
for label, kw, why in (("an unknown mark", {"mark": "delete"}, "read, unread, flag or unflag"),
                       ("a mark and a folder", {"mark": "read", "folder": "Archive"}, "one at a time")):
    try:
        MM.preview("alex", sender="halewater", **kw, **KW)
        check(f"{label}: refused", "planned", "refused")
    except MM.MoveError as exc:
        check(f"{label}: refused, saying why", why in str(exc), True)


class Spy:
    def __init__(self):
        self.calls = []

    def uid(self, *a):
        self.calls.append(a)
        return "OK", [b""]


spy = Spy()
w = M._Mover(spy)
for how in ("deleted", "\\Deleted", "seen", ""):
    try:
        w.mark([1], how)
        check(f"the writer, mark {how!r}: refused", "stored", "refused")
    except M.ReadOnlyViolation:
        check(f"the writer, mark {how!r}: refused, nothing sent", spy.calls, [])
try:
    w.store("1", "+FLAGS", "(\\Deleted)")
    check("the writer has no raw store", "stored", "refused")
except M.ReadOnlyViolation:
    check("the writer has no raw store or expunge", spy.calls, [])

print("── the tool ──")
import agent_runtime_lib as lib  # noqa: E402
real_preview = MM.preview
MM.preview = lambda person, **k: real_preview(person, **{**k, **KW})
lib._TURN_IO.person = {"id": "alex", "name": "Alex"}
lib._TURN_IO.role = "admin"
lib._TURN_IO.capability_tiers = None
out = lib._mail_move("mail_move_preview", {"sender": "halewater", "mark": "unread"})
check("preview with mark: says it marks them where they are, nothing changed",
      ("marked unread where they are" in out, "Nothing has moved" in out), (True, True))
pid = out.split(":")[0].split(" ")[1]
real_request = MM.request
MM.request = lambda plan_id, person: real_request(plan_id, person, add_card=add_card)
check("mail_move: asks them to tap Mark unread (the one read bill: the others are unread already)",
      lib._mail_move("mail_move", {"plan_id": pid}),
      "Asked: a card on their iPhone or Mac to mark 1 mail unread. Nothing has moved until they tap Mark unread there.")
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
