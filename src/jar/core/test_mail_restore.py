#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Bringing mail back to the inbox (#1642: unarchive, restore from the Trash), mail_moves' restore_from: the mails
that match in the Archive, the Trash or one of the mailbox's folders, back to the INBOX, on the owner's own tap; Undo
puts them back where they were; nothing is deleted. Against the fake IMAP server, fictional mail only.
Run: python3 scripts/test_mail_restore.py
"""
from __future__ import annotations

import json
import os
import shlex
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailrestore-"))
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


def mail(mid, frm, subject, date="Thu, 01 Oct 2026 09:00:00 -0700"):
    return (f"Message-ID: {mid}\r\nFrom: {frm}\r\nSubject: {subject}\r\nDate: {date}\r\n"
            "Content-Type: text/plain\r\n\r\nbody\r\n").encode()


BOX: dict[str, dict] = {}
LOG: list[str] = []
CAPS = {"move": True}
PASSWORD = "fake-app-pass-LILAC"
NEXT = {"uid": 100}


def reset():
    BOX.clear()
    BOX["INBOX"] = {i: mail(f"<hw-{i}@halewater.example>", "Hale Water <billing@halewater.example>", f"Bill {i}")
                    for i in range(1, 4)}
    BOX["INBOX"][4] = mail("<rb-1@mail.example>", "Robin <robin@mail.example>", "Saturday")
    BOX["Archive"] = {}
    BOX["Trash"] = {}
    BOX["School"] = {}


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
                self.send("* CAPABILITY IMAP4rev1 UIDPLUS" + (" MOVE" if CAPS["move"] else ""))
            elif cmd == "LOGIN":
                if PASSWORD not in rest:
                    self.send(f"{tag} NO bad")
                    continue
            elif cmd == "LIST":
                for flags, name in (("\\HasNoChildren", "INBOX"), ("\\HasNoChildren \\Archive", "Archive"),
                                    ("\\HasNoChildren \\Trash", "Trash"), ("\\HasNoChildren", "School"),
                                    ("\\Noselect \\HasChildren", "Projects")):
                    self.send(f'* LIST ({flags}) "/" "{name}"')
            elif cmd in ("EXAMINE", "SELECT"):
                folder = rest.split(" ", 1)[1].strip('"')
                self.send(f"* {len(BOX[folder])} EXISTS")
                self.send("* OK [UIDVALIDITY 5] ok")
                self.send(f"{tag} OK [{'READ-ONLY' if cmd == 'EXAMINE' else 'READ-WRITE'}] done")
                continue
            elif cmd == "UID SEARCH":
                keys = shlex.split(rest.split(" ", 2)[2])
                hits = [u for u, raw in sorted(BOX[folder].items()) if match(raw, keys)]
                self.send("* SEARCH " + " ".join(map(str, hits)))
            elif cmd == "UID FETCH":
                _, _, uids, what = rest.split(" ", 3)
                want = {int(x) for x in uids.split(",")}
                for n, u in enumerate(sorted(want & set(BOX[folder])), 1):
                    raw = BOX[folder][u].split(b"\r\n\r\n")[0] + b"\r\n\r\n"
                    self.send(f"* {n} FETCH (UID {u} BODY[HEADER.FIELDS ({M.HEADER_FIELDS})] {{{len(raw)}}}\r\n".encode()
                              + raw + b")\r\n")
            elif cmd == "UID MOVE":
                uids, dest = parts[2], rest.split(" ", 3)[3].strip('"')
                for u in [int(x) for x in uids.split(",")]:
                    if u in BOX[folder]:
                        NEXT["uid"] += 1
                        BOX[dest][NEXT["uid"]] = BOX[folder].pop(u)
            elif cmd == "LOGOUT":
                self.send("* BYE")
                self.send(f"{tag} OK bye")
                return
            else:
                self.send(f"{tag} NO not here")
                continue
            self.send(f"{tag} OK done")


def match(raw, keys):
    low, i = raw.decode().lower(), 0
    while i < len(keys):
        k = keys[i].upper()
        if k == "ALL":
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
PORT = srv.server_address[1]
ACCTS = _TMP / "accounts"
ACCTS.mkdir(mode=0o700)
p = ACCTS / "icloud.json"
p.write_text(json.dumps({"owner": "alex", "provider": "imap", "host": "127.0.0.1", "port": PORT,
                         "user": "alex@mail.example", "password": PASSWORD}))
os.chmod(p, 0o600)
ROLES = {"alex": "admin", "robin": "adult", "ava": "kid"}
CARDS: list[dict] = []


def connect(acct):
    import imaplib
    return imaplib.IMAP4(acct["host"], acct["port"], timeout=10)


def add_card(title, detail="", **kw):
    CARDS.append({"title": title, "detail": detail, **kw, "id": f"c{len(CARDS)}"})
    return {"id": f"c{len(CARDS) - 1}"}


KW = dict(connect=connect, directory=ACCTS, role_of=ROLES.get)
IPHONE = {"id": "d-ip", "platform": "ios"}
MAC = {"id": "d-mac", "platform": "mac"}
TV = {"id": "d-tv", "platform": "tvos"}
WATCH = {"id": "d-w", "platform": "watchos"}


def tap(verb, pid, person="alex", role="admin", device=IPHONE):
    return MM.answer(f"{verb}:{pid}", person, role, device, add_card=add_card, **KW)


def moves():
    return [x for x in LOG if x.upper().startswith("UID MOVE")]


reset()
BOX["Archive"] = {201: BOX["INBOX"].pop(1), 202: BOX["INBOX"].pop(2)}
BOX["Trash"] = {301: BOX["INBOX"].pop(3)}
LOG.clear()
print("── unarchive ──")
plan = MM.preview("alex", sender="halewater", restore_from="archive", **KW)
check("two archived Hale Water mails: Archive → INBOX, a restore, nothing moved yet",
      (plan["count"], plan["from"], plan["to"], plan["verb"], moves()), (2, "Archive", "INBOX", "restore", []))
MM.request(plan["plan_id"], "alex", add_card=add_card)
check("the card: Move 2 mails from Archive back to the inbox?, tapped Move to inbox",
      (CARDS[-1]["title"], CARDS[-1]["actions"][0]["label"]), ("Move 2 mails from Archive back to the inbox?", "Move to inbox"))
check("a TV can't tap it", (tap("move", plan["plan_id"], device=TV)[0], len(BOX["Archive"])), (False, 2))
ok, said = tap("move", plan["plan_id"])
check("Alex's iPhone: back in the inbox", (ok, said, len(BOX["Archive"]), len(BOX["INBOX"])),
      (True, "Moved 2 mails back to the inbox from Archive.", 0, 3))
check("…one UID MOVE, never STORE, COPY or EXPUNGE", (len(moves()), [x for x in LOG if x.upper().startswith(("UID STORE", "UID COPY", "EXPUNGE"))]),
      (1, []))
ok, said = MM.undo(plan["plan_id"], "alex", add_card=add_card)
check("undo: asked the same way, the card says back to Archive", (ok, CARDS[-1]["title"]), (True, "Move 2 mails back to Archive?"))
ok, said = tap("move", CARDS[-1]["data"]["plan_id"])
check("…tapped: back in the Archive", (ok, said, len(BOX["Archive"])), (True, "Moved 2 back to Archive.", 2))

print("── restore from the Trash ──")
plan = MM.preview("alex", sender="halewater", restore_from="the trash", **KW)
check("the Trash, by its word: one mail, Trash → INBOX", (plan["count"], plan["from"]), (1, "Trash"))
MM.request(plan["plan_id"], "alex", add_card=add_card)
check("…restored on the tap", (tap("move", plan["plan_id"])[0], len(BOX["Trash"])), (True, 0))

print("── refusals ──")
for label, kw, why in (("a folder that isn't there", {"restore_from": "Taxes"}, "no folder by that name"),
                       ("the inbox itself", {"restore_from": "INBOX"}, "no folder by that name"),
                       ("restore and move at once", {"restore_from": "archive", "folder": "School"}, "one at a time"),
                       ("restore and mark at once", {"restore_from": "archive", "mark": "read"}, "one at a time")):
    try:
        MM.preview("alex", sender="halewater", **kw, **KW)
        check(f"{label}: refused", "planned", "refused")
    except MM.MoveError as exc:
        check(f"{label}: refused, saying why", why in str(exc), True)
check("nothing matching there: no plan", MM.preview("alex", sender="nobody", restore_from="trash", **KW)["count"], 0)
try:
    MM.preview("robin", sender="halewater", restore_from="archive", **KW)
    check("Robin: no mailbox", "planned", "NoMailbox")
except M.NoMailbox:
    check("Robin: no mailbox, nothing planned", True, True)
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
