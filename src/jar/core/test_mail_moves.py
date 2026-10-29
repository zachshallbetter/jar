#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""Archiving mail on the server (titan-node#955 step 5) against a fake IMAP server, fictional mail only. The floor:
nothing moves without a tap on the owner's own iPhone or Mac, every time; a changed set asks again; at most 500;
Archive only, UID MOVE only, never a delete; undo by Message-ID for 7 days, asked the same way; the audit has no
subject. On for each person until they turn it off (#1627: the mail_moves capability, for the mailbox's owner); off for
the house with MAIL_SERVER_WRITES=0. Run: python3 scripts/test_mail_moves.py
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

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailmove-"))
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
print("── the plan: read-only, exact, the owner's own inbox ──")
LOG.clear()
plan = MM.preview("alex", sender="halewater", **KW)
check("three Hale Water mails, INBOX → Archive, nothing moved",
      (plan["count"], plan["from"], plan["to"], len(BOX["INBOX"]), moves()), (3, "INBOX", "Archive", 4, []))
check("…planning only reads (EXAMINE and PEEK)", [x.split(" ")[0] for x in LOG if x.split(" ")[0].upper() in ("SELECT", "STORE", "EXPUNGE")], [])
try:
    MM.preview("robin", sender="halewater", **KW)
    check("Robin: no mailbox", "planned", "NoMailbox")
except M.NoMailbox:
    check("Robin: no mailbox, nothing planned", True, True)
cap = MM.CAP
MM.CAP = 2
try:
    MM.preview("alex", sender="halewater", **KW)
    check("more than the cap: refused", "planned", "refused")
except MM.MoveError as exc:
    check("more than the cap: refused with the count", "3 mails" in str(exc), True)
MM.CAP = cap

print("\n── asking: a card, and only the owner's own iPhone or Mac moves anything ──")
MM.request(plan["plan_id"], "alex", add_card=add_card)
card = CARDS[-1]
check("a card to Alex only, with the count, senders and undo note",
      (card["to"], card["title"], "Hale Water" in card["detail"], "undo it for 7 days" in card["detail"],
       [a["value"].split(":")[0] for a in card["actions"]]), (["alex"], "Archive 3 mails?", True, True, ["move", "decline"]))
check("still nothing moved", (len(BOX["Archive"]), moves()), (0, []))
for who, args in (("Robin's tap", ("robin", "adult", IPHONE)), ("Alex on a TV", ("alex", "admin", TV)),
                  ("Alex on a Watch", ("alex", "admin", WATCH)), ("Alex with no device (master token)", ("alex", "admin", None))):
    ok, said = tap("move", plan["plan_id"], *args)
    check(f"{who}: refused, nothing moved", (ok, len(BOX["Archive"])), (False, 0))
os.environ["MAIL_SERVER_WRITES"] = "0"
check("writes off: the tap does nothing", (tap("move", plan["plan_id"])[0], len(BOX["Archive"])), (False, 0))
os.environ["MAIL_SERVER_WRITES"] = "1"
LOG.clear()
ok, said = tap("move", plan["plan_id"], device=MAC)
check("Alex's own Mac: archived 3", (ok, said, len(BOX["INBOX"]), len(BOX["Archive"]), len(BOX["Trash"])),
      (True, "Archived 3 mails.", 1, 3, 0))
check("…with one UID MOVE to Archive; never STORE, COPY or EXPUNGE",
      (len(moves()), moves()[0].endswith('"Archive"'), [x for x in LOG if x.split(" ")[0].upper() in ("STORE", "COPY", "EXPUNGE")]
       + [x for x in LOG if x.upper().startswith(("UID STORE", "UID COPY", "UID EXPUNGE"))]), (1, True, []))
check("a second tap: already answered", tap("move", plan["plan_id"]), (False, "That's been answered already."))
done_card = CARDS[-1]
check("the result card offers Undo for 7 days", ([a["value"].split(":")[0] for a in done_card["actions"]], done_card["expires_in"]),
      (["undo"], MM.UNDO_S))
con = MM._db()
row = dict(con.execute("SELECT * FROM mail_plans WHERE id=?", (plan["plan_id"],)).fetchone())
con.close()
check("the audit: who, which device, how many; Message-IDs; no subject",
      (row["state"], row["confirmed_by"], row["confirmed_device"], row["moved"], "Bill 1" in json.dumps(row),
       "<hw-1@halewater.example>" in row["mids"]), ("done", "alex", "d-mac", 3, False, True))

print("\n── undo: by Message-ID (the Archive numbered them anew), asked the same way ──")
ok, said = tap("undo", plan["plan_id"])
check("undo asks: a card, nothing moved yet", (ok, CARDS[-1]["title"], len(BOX["Archive"])),
      (True, "Move 3 mails back to the inbox?", 3))
back = CARDS[-1]["data"]["plan_id"]
check("…another person can't confirm it", tap("move", back, "robin", "adult")[0], False)
ok, said = tap("move", back)
check("…the owner's tap moves them back", (ok, said, len(BOX["INBOX"]), len(BOX["Archive"])),
      (True, "Moved 3 back to the inbox.", 4, 0))
check("an undo can't be undone again from its own plan", MM.undo(back, "alex")[0], False)

print("\n── a set that changed between the card and the tap asks again ──")
reset()
plan = MM.preview("alex", sender="halewater", **KW)
MM.request(plan["plan_id"], "alex", add_card=add_card)
BOX["Archive"][999] = BOX["INBOX"].pop(2)      # moved by hand on another device meanwhile
n = len(CARDS)
ok, said = tap("move", plan["plan_id"])
check("nothing moved, and a new card asks for the two still there",
      (ok, len(BOX["INBOX"]), len(CARDS) - n, CARDS[-1]["title"]), (False, 3, 1, "Archive 2 mails?"))

print("\n── a server without MOVE: refused, never copy-and-delete ──")
reset()
plan = MM.preview("alex", sender="halewater", **KW)
CAPS["move"] = False
LOG.clear()
ok, said = tap("move", plan["plan_id"])
check("no move, nothing copied or deleted", (ok, len(BOX["INBOX"]), [x for x in LOG if "COPY" in x.upper() or "EXPUNGE" in x.upper()]),
      (False, 4, []))
CAPS["move"] = True

print("\n── the write connection does only what a move needs ──")
w = M.open_writer(M.load_account(p, ROLES.get), connect)
for name, call in (("store", lambda: w.store("1", "+FLAGS", "\\Deleted")), ("expunge", lambda: w.expunge()),
                   ("copy", lambda: w.copy("1", "Trash")), ("append", lambda: w.append("INBOX", None, None, b"x"))):
    try:
        call()
        check(f"{name}: refused", "sent", "refused")
    except M.ReadOnlyViolation:
        check(f"{name}: refused before the server", True, True)
w.logout()
try:
    M.open_writer({"provider": "gmail", "name": "g"})
    check("Gmail: not yet", "opened", "refused")
except M.ReadOnlyViolation:
    check("Gmail: not yet (gmail.modify is step 5b)", True, True)

print("\n── the agent's tools ──")
import agent_runtime_lib as lib  # noqa: E402
import household_identity as hh  # noqa: E402
check("offered with writes on; never to a kid or a shared device",
      ([hh.role_allows_tool(r, "mail_move") for r in ("admin", "adult", "kid", "household")], lib._role_allows("mail_move")),
      ([True, True, False, False], True))
os.environ["MAIL_SERVER_WRITES"] = "0"
check("…not offered while writes are off", lib._role_allows("mail_move_preview"), False)
os.environ["MAIL_SERVER_WRITES"] = "1"
import governance  # noqa: E402
os.environ.pop("MAIL_SERVER_WRITES")
check("#1627: the old flag unset is on (it's a kill switch now, not the on-switch)",
      (MM.enabled("alex"), lib._role_allows("mail_move")), (True, True))
governance.set_tier("robin", "mail_moves", "never")
check("Robin turned it off: Robin's own refusal says so, Alex's still on",
      (MM._off("robin"), MM._off("alex")), (MM.YOU_OFF, None))
try:
    MM.preview("robin", query="water", **KW)
    check("…Robin's preview is refused", "planned", "refused")
except MM.MoveError as exc:
    check("…Robin's preview is refused", str(exc), MM.YOU_OFF)
lib._TURN_IO.person, lib._TURN_IO.role, lib._TURN_IO.capability_tiers = {"id": "robin"}, "adult", None
check("…and the tool isn't offered to Robin", lib._role_allows("mail_move"), False)
lib._TURN_IO.person = None
lib._TURN_IO.role = None
lib._TURN_IO.capability_tiers = None
governance.set_tier("robin", "mail_moves", "always")
governance.set_tier("alex", "mail", "never")
check("Alex's Never for all of mail covers moving it", MM._off("alex"), MM.YOU_OFF)
governance.set_tier("alex", "mail", "always")
os.environ["MAIL_SERVER_WRITES"] = "1"
reset()
real = (MM.preview, MM.request, lib._kb_who)
MM.preview = lambda person, **kw: real[0](person, **{**kw, **KW})
MM.request = lambda pid, person, **kw: real[1](pid, person, add_card=add_card)
lib._kb_who = lambda: ("alex", "admin")
try:
    out = lib._execute_tool("mail_move_preview", {"sender": "halewater"})
    pid = out.split(" ")[1].rstrip(":")
    check("preview: the count and senders; says nothing has moved", ("3 inbox mails" in out, "Nothing has moved" in out), (True, True))
    out = lib._execute_tool("mail_move", {"plan_id": pid})
    check("mail_move only asks", (out.startswith("Asked:"), len(BOX["Archive"])), (True, 0))
    import clear_prefs as _CP
    _CP.put("alex", {"kinds": {"mail": "trash"}})
    out = lib._execute_tool("mail_move_preview", {"sender": "halewater"})
    pid = out.split(" ")[1].rstrip(":")
    check("with Trash set, the model is told it's the Trash, not an archive",
          ("(to the Trash, their clearing setting)" in out, "to the Archive" in out), (True, False))
    out = lib._execute_tool("mail_move", {"plan_id": pid})
    check("…and that the card asks to move them to the Trash",
          (out, len(BOX["Trash"])), ("Asked: a card on their iPhone or Mac to move 3 mails to the Trash. Nothing has moved "
                                     "until they tap Move to Trash there.", 0))
    _CP.put("alex", {"kinds": {"mail": None}})
    check("no tool text claims archive is the only place mail goes",
          "Archive is the only place" in lib._TOOL_REGISTRY["mail_move"]["description"], False)
    lib._kb_who = lambda: (None, "household")
    check("a shared device: refused", lib._execute_tool("mail_move_preview", {"sender": "x"}).startswith("Moving mail is for"), True)
finally:
    MM.preview, MM.request, lib._kb_who = real

print("\n── clearing to the Trash, when that's Alex's setting (#1646) ──")
import clear_prefs as CP  # noqa: E402
reset()
CP.put("alex", {"kinds": {"mail": "trash"}})
LOG.clear()
plan = MM.preview("alex", sender="halewater", **KW)
check("the plan goes to the Trash, nothing moved", (plan["to"], plan["verb"], len(BOX["INBOX"]), moves()), ("Trash", "trash", 4, []))
MM.request(plan["plan_id"], "alex", add_card=add_card)
card = CARDS[-1]
check("the card says Trash, its button too, and that undo needs the server to keep it",
      (card["title"], next(a["label"] for a in card["actions"] if a["id"] == "move"), "while the server keeps it in the Trash"
       in card["detail"]), ("Move 3 mails to the Trash?", "Move to Trash", True))
LOG.clear()
ok, said = tap("move", plan["plan_id"], device=MAC)
check("the owner's tap: moved to the Trash, one UID MOVE, never STORE or EXPUNGE",
      (ok, said, len(BOX["Trash"]), len(BOX["Archive"]), len(moves()), moves()[0].endswith('"Trash"'),
       [x for x in LOG if "EXPUNGE" in x.upper() or "STORE" in x.upper()]), (True, "Moved 3 mails to the Trash.", 3, 0, 1, True, []))
check("the result card says how long undo holds", "while the server keeps it in the Trash" in CARDS[-1]["detail"], True)
ok, said = tap("undo", plan["plan_id"])
back = CARDS[-1]["data"]["plan_id"]
check("undo from the Trash, asked the same way, moves them back",
      (ok, CARDS[-1]["title"], tap("move", back)[1], len(BOX["INBOX"]), len(BOX["Trash"])),
      (True, "Move 3 mails back to the inbox?", "Moved 3 back to the inbox.", 4, 0))
CP.put("alex", {"kinds": {"mail": None}})
check("back to the default: archive", MM.preview("alex", sender="halewater", **KW)["to"], "Archive")
old = MM._db()
old.execute("UPDATE mail_plans SET verb=NULL")
old.commit()
check("a plan from before #1646 (no verb) reads as an archive",
      MM._card_text(old.execute("SELECT * FROM mail_plans WHERE undo_of IS NULL ORDER BY created_at DESC").fetchone())[0],
      "Archive 3 mails?")
old.close()

print("\n── the Trash folder, and the edges of clearing to it (#1646 review) ──")


class Listed:
    def __init__(self, rows):
        self.rows = rows

    def folders(self):
        return self.rows


check("trash_folder: the \\Trash flag first, never a \\Noselect one; else its name; never a folder called Bin; a nested "
      "name isn't guessed",
      (M.trash_folder(Listed([({"\\hasnochildren"}, "Bin"), ({"\\trash"}, "Deleted")])),
       M.trash_folder(Listed([({"\\trash", "\\noselect"}, "[Mail]"), ({"\\hasnochildren"}, "Trash")])),
       M.trash_folder(Listed([({"\\hasnochildren"}, "Deleted Messages")])),
       M.trash_folder(Listed([({"\\hasnochildren"}, "Bin")])),
       M.trash_folder(Listed([({"\\hasnochildren"}, "INBOX.Trash")]))),
      ("Deleted", "Trash", "Deleted Messages", None, None))
reset()
CP.put("alex", {"kinds": {"mail": "trash"}})
plan = MM.preview("alex", sender="halewater", **KW)
MM.request(plan["plan_id"], "alex", add_card=add_card)
CP.put("alex", {"kinds": {"mail": "archive"}})
ok, said = tap("move", plan["plan_id"], device=MAC)
check("the setting changed after the card went out: the move is the one the card asked (Trash)",
      (ok, len(BOX["Trash"]), len(BOX["Archive"])), (True, 3, 0))
BOX["Trash"].clear()                     # the server emptied its Trash
ok, said = tap("undo", plan["plan_id"])
back = CARDS[-1]["data"]["plan_id"]
ok, said = tap("move", back)
check("undo after the server emptied the Trash says so, and moves nothing",
      (ok, said, len(BOX["INBOX"])), (False, "Those mails aren't in the Trash any more (the server may have emptied it); "
                                          "nothing was moved.", 1))
con = MM._db()
check("an undo plan is recorded as one (verb back), not as an archive",
      con.execute("SELECT verb FROM mail_plans WHERE id=?", (back,)).fetchone()[0], "back")
con.close()
CP.put("alex", {"kinds": {"mail": None}})

import sqlite3 as _sq  # noqa: E402
olddb = Path(os.environ["MAIL_SERVER_DB"]).with_name("old-plans.db") if os.environ.get("MAIL_SERVER_DB") else _TMP / "old-plans.db"
o = _sq.connect(olddb)
o.execute("""CREATE TABLE mail_plans (id TEXT PRIMARY KEY, owner TEXT NOT NULL, account TEXT NOT NULL,
    from_folder TEXT NOT NULL, to_folder TEXT NOT NULL, mids TEXT NOT NULL, count INTEGER NOT NULL,
    senders TEXT NOT NULL, first_at TEXT, last_at TEXT, created_at REAL NOT NULL, expires_at REAL NOT NULL,
    state TEXT NOT NULL, card_id TEXT, undo_of TEXT, confirmed_by TEXT, confirmed_device TEXT, done_at REAL,
    moved INTEGER)""")
o.execute("INSERT INTO mail_plans VALUES ('old1','alex','a','INBOX','Archive','[]',2,'[[\"Hale\", 2]]',NULL,NULL,0,1e12,"
          "'pending',NULL,NULL,NULL,NULL,NULL,NULL)")
o.commit()
o.close()
con = MM._db(olddb)
row = con.execute("SELECT * FROM mail_plans WHERE id='old1'").fetchone()
check("a table from before #1646 gains the column, and its plan reads as an archive",
      ("verb" in row.keys(), MM._card_text(row)[0]), (True, "Archive 2 mails?"))
con.close()
con = MM._db(olddb)                      # a second open (another process got there first) is fine
con.close()

print("\n── filing into one of the mailbox's own folders (#1646) ──")
reset()
plan = MM.preview("alex", sender="halewater", folder="school", **KW)
check("a plan to that folder (any case), nothing moved", (plan["to"], plan["verb"], len(BOX["School"]), len(BOX["INBOX"])),
      ("School", "move", 0, 4))
MM.request(plan["plan_id"], "alex", add_card=add_card)
card = CARDS[-1]
check("the card names the folder, its button says Move, undo for 7 days",
      (card["title"], next(a["label"] for a in card["actions"] if a["id"] == "move"), "undo it for 7 days" in card["detail"]),
      ("Move 3 mails to School?", "Move", True))
LOG.clear()
ok, said = tap("move", plan["plan_id"], device=MAC)
check("the owner's tap files them: one UID MOVE, never STORE or EXPUNGE",
      (ok, said, len(BOX["School"]), [x for x in LOG if "EXPUNGE" in x.upper() or "STORE" in x.upper()]),
      (True, "Moved 3 mails to School.", 3, []))
ok, said = tap("undo", plan["plan_id"])
back = CARDS[-1]["data"]["plan_id"]
check("undo files them back, asked the same way", (ok, tap("move", back)[1], len(BOX["INBOX"]), len(BOX["School"])),
      (True, "Moved 3 back to the inbox.", 4, 0))
for label, name, want in (("a folder that isn't there", "Taxes", "no folder by that name"),
                          ("the inbox itself", "INBOX", "no folder by that name"),
                          ("a folder that can't hold mail (\\Noselect)", "Projects", "no folder by that name")):
    try:
        MM.preview("alex", sender="halewater", folder=name, **KW)
        check(f"{label}: refused", "planned", "refused")
    except MM.MoveError as exc:
        check(f"{label}: refused, saying which folders there are", (want in str(exc), "School" in str(exc),
                                                                  "Projects" in str(exc)), (True, True, False))
real = (MM.preview, MM.request, lib._kb_who)
MM.preview = lambda person, **kw: real[0](person, **{**kw, **KW})
MM.request = lambda pid, person, **kw: real[1](pid, person, add_card=add_card)
lib._kb_who = lambda: ("alex", "admin")
try:
    out = lib._execute_tool("mail_move_preview", {"sender": "halewater", "folder": "School"})
    check("the model is told they'd be filed into that folder", "(filed into that folder)" in out, True)
    out = lib._execute_tool("mail_move", {"plan_id": out.split(" ")[1].rstrip(":")})
    check("…and that the card asks to move them to School", out,
          "Asked: a card on their iPhone or Mac to move 3 mails to School. Nothing has moved until they tap Move there.")
finally:
    MM.preview, MM.request, lib._kb_who = real

print("\n── where they usually file a sender's mail (#1644) ──")
for _ in range(3):
    reset()
    pl = MM.preview("alex", sender="halewater", folder="School", **KW)
    MM.request(pl["plan_id"], "alex", add_card=add_card)
    tap("move", pl["plan_id"], device=MAC)
reset()
plan = MM.preview("alex", sender="halewater", **KW)
check("after three confirmed filings into School, an archive plan says where they usually put these senders' mail",
      (plan["to"], plan.get("usual_folder")), ("Archive", "School"))
check("…a filing plan doesn't (it already says where)", "usual_folder" in MM.preview("alex", sender="halewater", folder="School", **KW),
      False)
check("…only their own filings count: another person has none", MM.usual_folder("robin", ["Hale Water <billing@halewater.example>"]),
      None)
con = MM._db()
school_n = con.execute("SELECT n FROM filing_habits WHERE owner='alex' AND folder='School'").fetchone()[0]
con.execute("INSERT INTO filing_habits (owner, sender, folder, n, last_at) VALUES "
            "('alex','hale water <billing@halewater.example>','Archive',?,0)", (school_n,))
con.commit()
check("a tie between two folders: nothing said", MM.usual_folder("alex", ["Hale Water <billing@halewater.example>"]), None)
con.execute("UPDATE filing_habits SET n=1 WHERE folder='Archive'")
con.execute("UPDATE filing_habits SET folder='Gone' WHERE folder='School'")
con.commit()
con.close()
check("a usual folder that's no longer in the mailbox: nothing said", MM.preview("alex", sender="halewater", **KW).get("usual_folder"),
      None)
con = MM._db()
con.execute("UPDATE filing_habits SET folder='School' WHERE folder='Gone'")
con.commit()
con.close()
real = (MM.preview, lib._kb_who)
MM.preview = lambda person, **kw: real[0](person, **{**kw, **KW})
lib._kb_who = lambda: ("alex", "admin")
try:
    out = lib._execute_tool("mail_move_preview", {"sender": "halewater"})
    check("the model is told to offer School, never to move it there unasked",
          ("usually filed these senders' mail into School" in out, "never move it there unasked" in out), (True, True))
finally:
    MM.preview, lib._kb_who = real
import me_delete  # noqa: E402
check("delete-my-data covers where they file their mail",
      ("mail", "filing_habits", "owner") in [(p_, t, c) for p_, t, _o, c in me_delete.Parts(None)._stores()], True)

print("\n── the audit goes after 30 days ──")
check("pruned", (MM.prune(now=__import__("time").time() + MM.KEEP_S + 60) > 0, MM._db().execute("SELECT COUNT(*) FROM mail_plans").fetchone()[0]),
      (True, 0))

srv.shutdown()
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
