#!/usr/bin/env python3
# gate: hermetic   (run by the deploy gate's discovery pass; touches nothing live)
"""The parallel week's count comparison (titan-node#955 step 6): Mac sync vs the server connector, matched by
Message-ID, per day, counts only; INBOX-scoped since 10-06: the Mac-only mails are looked up read-only on the IMAP
account and only those in INBOX (or nowhere) hold the run sheet back. Temp stores, a fake IMAP server, fictional ids.
Run: python3 scripts/test_mail_compare.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="titan-mailcmp-"))
os.environ["CONNECTORS_LEDGER"] = str(_TMP / "ledger.db")
os.environ["MAIL_SERVER_DB"] = str(_TMP / "mail_server.db")
os.environ["MAIL_ACCOUNTS_DIR"] = str(_TMP / "no-accounts")   # never the real accounts: the gate runs on titan
sys.path.insert(0, str(Path(__file__).parent))

import os as _os_acl, tempfile as _tf_acl  # noqa: E402
_os_acl.environ.setdefault("KB_ACL_DB", _os_acl.path.join(_tf_acl.mkdtemp(prefix="titan-acl-"), "kb_acl.db"))  # #1273: ingest writes sharing rows; never titan's own
import connectors_runner as R  # noqa: E402
import mail_compare as C  # noqa: E402
import mail_server as M  # noqa: E402

passed = failed = 0


def check(label, got, expected):
    global passed, failed
    ok = got == expected
    passed, failed = passed + ok, failed + (not ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f": got={got!r:.300} expected={expected!r:.300}"))


L = R._db(Path(os.environ["CONNECTORS_LEDGER"]))
for sid, acct, at, gone, owner in (
        ("a1@hale.example", "iCloud", "2026-09-17T10:00:00Z", None, "zach"),
        ("a2@hale.example", "iCloud", "2026-09-17T11:00:00Z", 1.0, "zach"),     # tombstoned by the cleanup, still seen
        ("a3@hale.example", "iCloud", "2026-09-18T09:00:00Z", None, "zach"),     # the server connector misses it
        ("g1@gmail.example", "Gmail", "2026-09-18T09:00:00Z", None, "zach"),     # another provider: left out
        ("r1@hale.example", "iCloud", "2026-09-18T09:00:00Z", None, "p_robin"),  # someone else's: left out
        ("old@hale.example", "iCloud", "2026-09-01T09:00:00Z", None, "zach")):   # before the server's window
    L.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, event_id, account, observed_at, "
              "synced_at, deleted_at, owner, upstream_id) VALUES ('mail',?,?,?,0,0,NULL,?,?,0,?,?,?)",
              (sid, "h", "cx-" + sid, acct, at, gone, owner, sid))
L.commit()
L.close()
S = M._db(Path(os.environ["MAIL_SERVER_DB"]))
for uid, mid, at, code in ((1, "<A1@hale.example>", "2026-09-17T10:00:00Z", 0), (2, "<a2@hale.example>", "2026-09-17T11:00:00Z", 0),
                           (3, "<s1@hale.example>", "2026-09-18T12:00:00Z", 0), (4, "<c1@login.example>", "2026-09-18T13:00:00Z", 1),
                           (5, "<s0@hale.example>", "2026-09-10T08:00:00Z", 0)):
    S.execute("INSERT INTO messages (account, folder, uidvalidity, uid, owner, item_id, message_id, thread_id, sent_at, code) "
              "VALUES ('icloud','INBOX',1,?,'zach','x',?,?,?,?)", (uid, mid, mid, at, code))
S.commit()
S.close()

got = C.compare()
check("the window starts where both have mail", got["since"], "2026-09-10")
check("matched by Message-ID, brackets and case aside; per day",
      got["days"], {"2026-09-10": {"both": 0, "server_only": 1, "mac_only": 0, "mac_only_inbox": 0, "mac_only_missed": 0, "mac_only_absent": 0,
                                   "mac_only_filed": 0, "mac_tombstoned": 0, "codes": 0},
                    "2026-09-17": {"both": 2, "server_only": 0, "mac_only": 0, "mac_only_inbox": 0, "mac_only_missed": 0,
                                   "mac_only_absent": 0, "mac_only_filed": 0, "mac_tombstoned": 1, "codes": 0},
                    "2026-09-18": {"both": 0, "server_only": 2, "mac_only": 1, "mac_only_inbox": 1, "mac_only_missed": 1,
                                   "mac_only_absent": 0, "mac_only_filed": 0, "mac_tombstoned": 0, "codes": 1}})
check("not looked up: the Mac-only mail counts as an INBOX miss", (got["located"], got["mac_only_where"]), (False, {"inbox": 1}))
check("a Mac mail the server lacks: not a match", got["match"], False)
check("the window can be set", sorted(C.compare(since="2026-09-17")["days"]), ["2026-09-17", "2026-09-18"])
check("another provider's prefix", C.compare(account_prefix="gma")["total"]["mac_only"], 1)
check("another owner sees only their own", C.compare(owner="p_robin")["total"],
      {"both": 0, "server_only": 0, "mac_only": 1, "mac_only_inbox": 1, "mac_only_missed": 1, "mac_only_absent": 0,
       "mac_only_filed": 0, "mac_tombstoned": 0, "codes": 0})
log = _TMP / "cmp.jsonl"
out = subprocess.run([sys.executable, str(Path(C.__file__)), "--append", str(log)], capture_output=True, text=True,
                     env=dict(os.environ)).stdout
check("the CLI prints counts", out.splitlines()[0].startswith("since 2026-09-10: both 2, server_only 3, mac_only 1"), True)
check("…never an id or an address", "@" in out or "hale" in out, False)
line = json.loads(log.read_text().splitlines()[0])
check("…and appends one JSON line, 0600", (line["total"]["both"], oct(log.stat().st_mode & 0o777)), (2, "0o600"))
check("read-only: the stores are unchanged", (sqlite3.connect(os.environ["CONNECTORS_LEDGER"]).execute(
      "SELECT COUNT(*) FROM items").fetchone()[0], sqlite3.connect(os.environ["MAIL_SERVER_DB"]).execute(
      "SELECT COUNT(*) FROM messages").fetchone()[0]), (6, 5))

print("\n── where the Mac-only mails are (read-only IMAP, folder kinds only) ──")


class FakeIMAP:
    """LIST / EXAMINE / UID SEARCH HEADER Message-ID over fixed folders; anything else is an AttributeError."""
    FOLDERS = {"INBOX": ["in1@hale.example"], "Sent Messages": ["snt@hale.example"], "Archive": ["arc@hale.example"],
               "Junk": []}
    FLAGS = {"INBOX": "\\HasNoChildren", "Sent Messages": "\\HasNoChildren \\Sent", "Archive": "\\Archive",
             "Junk": "\\Junk"}
    calls: list = []

    def __init__(self, acct):
        self.sel = None

    def login(self, u, p):
        pass

    def list(self):
        return "OK", [f'({self.FLAGS[n]}) "/" "{n}"'.encode() for n in self.FOLDERS]

    def select(self, folder, readonly=False):
        FakeIMAP.calls.append(("select", readonly))
        self.sel = folder.strip('"')
        return "OK", [b"1"]

    def response(self, code):
        return code, [b"7"]

    def uid(self, cmd, *args):
        FakeIMAP.calls.append(("uid", cmd))
        mid = args[-1].strip('"')
        return "OK", [b"3" if mid in self.FOLDERS[self.sel] else b""]

    def logout(self):
        pass


ACCT = [{"owner": "zach", "name": "icloud", "user": "alex@hale.example", "password": "x", "provider": None},
        {"owner": "p_robin", "name": "robin", "user": "r", "password": "x", "provider": None}]
where = C.locate(["snt@hale.example", "arc@hale.example", "in1@hale.example", "gone@hale.example"], "zach", ACCT,
                 connect=FakeIMAP)
check("locate: each id by its folder's kind, absent when nowhere", where,
      {"in1@hale.example": "inbox", "snt@hale.example": "sent", "arc@hale.example": "archive", "gone@hale.example": "absent"})
check("…read-only: every select is readonly (EXAMINE), only UID SEARCH runs",
      ({c[1] for c in FakeIMAP.calls if c[0] == "select"}, {c[1] for c in FakeIMAP.calls if c[0] == "uid"}), ({True}, {"SEARCH"}))
check("folder kinds by name when there's no flag", [C.folder_kind(set(), n) for n in ("Sent Items", "Deleted Messages", "Receipts")],
      ["sent", "trash", "other"])
# the Mac has three more mails the server's INBOX lacks: one in Sent, one in Archive, one nowhere
L = R._db(Path(os.environ["CONNECTORS_LEDGER"]))
for sid in ("snt@hale.example", "arc@hale.example", "gone@hale.example"):
    L.execute("INSERT INTO items (source, source_id, content_hash, item_id, kb, node, event_id, account, observed_at, "
              "synced_at, deleted_at, owner, upstream_id) VALUES ('mail',?,?,?,0,0,NULL,'iCloud','2026-09-19T09:00:00Z',0,NULL,"
              "'zach',?)", (sid, "h", "cx-" + sid, sid))
L.commit()
L.close()
g2 = C.compare(locate_fn=lambda mids: C.locate(mids, "zach", ACCT, connect=FakeIMAP))
check("compare: Sent and Archive don't count against the INBOX read; absent and a3 (not on this fake) do",
      (g2["located"], g2["total"]["mac_only"], g2["total"]["mac_only_inbox"], g2["mac_only_where"]),
      (True, 4, 2, {"sent": 1, "archive": 1, "absent": 2}))


def boom(mids):
    raise OSError("no route")


g3 = C.compare(locate_fn=boom)
check("a lookup that fails counts every Mac-only mail as a miss", (g3["located"], g3["total"]["mac_only_inbox"]), (False, 4))
check("summary gates on mac_only_inbox, shows all mailboxes beside it; old lines fall back to mac_only",
      [(r["mac_only"], r["mac_only_all"]) for r in C.summary([
          json.dumps({"at": "2026-10-06T14:11:00Z", "total": {"mac_only": 3, "mac_only_inbox": 0}}),
          json.dumps({"at": "2026-10-05T14:11:00Z", "total": {"mac_only": 1}})], days=2)["days"]],
      [(1, 1), (0, 3)])

print("\n── the run sheet, per sent-day of the parallel week (10-07) ──")
check("compare splits Mac-only by where it is: missed (INBOX), deleted upstream, filed elsewhere",
      {k: g2["total"][k] for k in ("mac_only_missed", "mac_only_absent", "mac_only_filed")},
      {"mac_only_missed": 0, "mac_only_absent": 2, "mac_only_filed": 2})


def run(day, per):
    return json.dumps({"at": f"{day}T14:11:00Z", "located": True, "total": {}, "days": per})


week = [f"2026-10-{d:02d}" for d in range(6, 13)]
clean = {d: {"both": 40, "server_only": 0, "mac_only_missed": 0, "mac_only_absent": 0, "mac_only_filed": 0} for d in week}
gone = {**clean, "2026-10-05": {"mac_only_missed": 0, "mac_only_absent": 1}}   # the real 10-05 mail: deleted upstream
rs = C.run_sheet([run(d, gone) for d in week], "2026-10-06")
check("one mail deleted upstream before the week (the real 10-05 case) never blocks GO", (rs["go"], rs["clean"]), (True, True))
rs = C.run_sheet([run(d, {**clean, "2026-10-09": {"mac_only_missed": 1, "mac_only_absent": 2}}) for d in week], "2026-10-06")
check("a mail still in the server's INBOX that the connector missed: NOT YET, on its day",
      (rs["go"], [r["day"] for r in rs["days"] if r["missed"]], [r["absent"] for r in rs["days"] if r["day"] == "2026-10-09"]),
      (False, ["2026-10-09"], [2]))
rs = C.run_sheet([run(d, clean) for d in week if d != "2026-10-08"], "2026-10-06")
check("a day with no run logged: NOT YET", (rs["go"], rs["logged"]), (False, False))
old_line = json.dumps({"at": "2026-10-12T14:11:00Z", "total": {}, "days": {"2026-10-07": {"mac_only_inbox": 1}}})
rs = C.run_sheet([run(d, clean) for d in week[:-1]] + [old_line], "2026-10-06")
check("a line from before the split counts its mac_only_inbox as missed", [r["missed"] for r in rs["days"] if r["day"] == "2026-10-07"], [1])
rlog = _TMP / "week.jsonl"
rlog.write_text("\n".join(run(d, gone) for d in week) + "\n")
p_ = subprocess.run([sys.executable, str(Path(C.__file__)), "--run-sheet", str(rlog), "--week-start", "2026-10-06"],
                    capture_output=True, text=True, env=dict(os.environ))
check("the one command: GO, exit 0, one line per day, counts only",
      (p_.returncode, p_.stdout.strip().splitlines()[-1], p_.stdout.count("  2026-10-"), "@" in p_.stdout),
      (0, "GO: every day of the week logged and measured, no INBOX mail missed", 7, False))
print("── a day neither side saw mail on is unmeasured, never GO (10-10: the reset took the Mac's side away) ──")
reset = {**clean, **{d: {"both": 0, "server_only": 0, "mac_only_missed": 0, "mac_only_absent": 0, "mac_only_filed": 0}
                     for d in ("2026-10-10", "2026-10-11", "2026-10-12")}}
rs = C.run_sheet([run(d, reset) for d in week], "2026-10-06")
check("\"both 0\" days: logged and clean, but NOT YET (unmeasured)", (rs["logged"], rs["clean"], rs["measured"], rs["go"]),
      (True, True, False, False))
check("…the unmeasured days are named", [r["day"] for r in rs["days"] if not r["measured"]], ["2026-10-10", "2026-10-11", "2026-10-12"])
quiet = {**clean, "2026-10-11": {"both": 0, "server_only": 3}}
check("a day only the server saw mail on is measured (server_only counts)",
      [r["measured"] for r in C.run_sheet([run(d, quiet) for d in week], "2026-10-06")["days"] if r["day"] == "2026-10-11"], [True])
rlog.write_text("\n".join(run(d, reset) for d in week) + "\n")
p_ = subprocess.run([sys.executable, str(Path(C.__file__)), "--run-sheet", str(rlog), "--week-start", "2026-10-06"],
                    capture_output=True, text=True, env=dict(os.environ))
check("the command: NOT YET with the reason, exit 1, each unmeasured day marked",
      (p_.returncode, "a day measured nothing" in p_.stdout.splitlines()[-1], p_.stdout.count("(unmeasured: neither side saw mail)")),
      (1, True, 3))

unit = (Path(C.__file__).parent.parent / "server-setup" / "titan-mail-compare.service").read_text()
check("the daily unit appends to the week's log", "mail_compare.py --append /ai/datasets/connectors/mail-compare.jsonl" in unit,
      True)
check("…on a daily timer", "OnCalendar=*-*-* 07:10:00 America/Los_Angeles" in (
      Path(C.__file__).parent.parent / "server-setup" / "titan-mail-compare.timer").read_text(), True)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
